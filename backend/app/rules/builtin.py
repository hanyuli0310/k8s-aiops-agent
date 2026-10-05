"""固定规则引擎：13 条内置风险规则（对齐 ground truth，但按通用逻辑实现，不写死服务名）。

⚠️ 关于「规则」这套机制本身的边界（这段是给读代码的人和写提示词的人看的）：
下面每一条规则都是**人已经知道的故障模式**的编码。它们判定快、可复现、零成本，
所以值得写死；但它们天然只覆盖「有人想到过并且愿意写下来」的那部分问题。
量化评测实测过一次典型的漏网：Redis 缓存雪崩场景下，11 条规则里没有任何一条
涉及缓存层，规则侧只能报出下游的 API-001（接口变慢），根因完全报不出来 ——
补上 CACHE-001/002 解决的是这一个具体缺口，**并没有解决「规则有限」这件事本身**。
真正的兜底在 Agent 侧：见 base.py 提示词与 risk_governance Skill 的
「规则之外」一节 —— 规则命中是线索起点，不是排查范围的边界。

每条规则是一个纯函数：读数据库 → 返回 finding 列表（可为空）。

U9.5（spec v1.1）双模式语义：
- static（默认）：全表聚合 + governance 标记判定 resolved —— 旧静态 demo 基准不变；
- live：数据类规则加 RULE_WINDOW_MINUTES 滑动时间窗（append 模式下否则被基线稀释永不越阈），
  禁用 governance 标记（纯靠窗口数据自然恢复，避免"动作一执行立刻 resolved"与
  二次注入被旧标记误判）。
- 指标类聚合一律按实例（dims）分组逐个判定：新世界 2×RDS，不分组会把单实例 95%
  稀释成双实例均值 67%；旧世界单实例下分组结果与全局聚合一致，回归不受影响。
"""
from __future__ import annotations

import logging
import math
import time

from .. import config, db

logger = logging.getLogger(__name__)

CPU_OVERSALE_THRESHOLD = 150.0   # %
RDS_MEM_THRESHOLD = 85.0         # %
RDS_CONN_THRESHOLD = 80.0        # %
SLOW_ROWS_EXAMINED = 1_000_000
API_P99_THRESHOLD_S = 1.0
API_ERR_THRESHOLD = 0.01
CACHE_MEM_THRESHOLD = 80.0        # %
CACHE_CPU_THRESHOLD = 70.0        # %


def _deployments():
    rows = db.fetch_all("SELECT namespace, name, spec_json FROM k8s_resources WHERE kind='Deployment'")
    return [(r["namespace"], r["name"], db.json_load(r["spec_json"])) for r in rows]


def _pods_by_app():
    rows = db.fetch_all("SELECT spec_json FROM k8s_resources WHERE kind='Pod'")
    out = {}
    for r in rows:
        spec = db.json_load(r["spec_json"])
        app = spec["metadata"].get("labels", {}).get("app")
        out.setdefault(app, []).append(spec)
    return out


def _node_zones():
    rows = db.fetch_all("SELECT spec_json FROM k8s_resources WHERE kind='Node'")
    return {db.json_load(r["spec_json"])["metadata"]["name"]:
            db.json_load(r["spec_json"])["metadata"]["labels"].get("topology.kubernetes.io/zone")
            for r in rows}


def _has_governance(key: str) -> bool:
    return db.fetch_one(
        "SELECT id FROM agent_memory WHERE scope='governance' AND mem_key=:k", {"k": key}) is not None


def _has_governance_prefix(prefix: str) -> bool:
    # U9.5：live 模式禁用标记判定，纯靠窗口数据回落自然消除
    if config.is_live():
        return False
    return db.fetch_one(
        "SELECT id FROM agent_memory WHERE scope='governance' AND mem_key LIKE :k",
        {"k": f"{prefix}%"}) is not None


def _window_ms() -> int:
    """live 模式的窗口起点（毫秒）；static 返回 0 即全表。"""
    if not config.is_live():
        return 0
    return int((time.time() - config.RULE_WINDOW_MINUTES * 60) * 1000)


def _window_s() -> int:
    """live 模式的窗口起点（秒）；static 返回 0 即全表。"""
    if not config.is_live():
        return 0
    return int(time.time() - config.RULE_WINDOW_MINUTES * 60)


def parse_cpu_m(v) -> float:
    """K8s CPU 两种写法统一换算为毫核：'4' -> 4000，'3900m' -> 3900。"""
    if v is None:
        return 0.0
    s = str(v)
    return float(s[:-1]) if s.endswith("m") else float(s) * 1000


def _metric_avg(namespace: str, metric: str) -> float:
    row = db.fetch_one(
        "SELECT AVG(avg) AS v FROM metrics WHERE namespace=:ns AND metric_name=:mn AND ts >= :w",
        {"ns": namespace, "mn": metric, "w": _window_ms()})
    return row["v"] if row and row["v"] is not None else 0.0


def _metric_avg_by_instance(namespace: str, metric: str) -> dict:
    """按 instanceId 分组的窗口均值：{instance_id: avg}。

    U9.5 核心：多实例下逐个判定，单实例（旧世界）下结果与全局聚合一致。
    dims_json 的存储形态因库而异（MySQL dict / SQLite 字符串），在 Python 侧解析分组。

    ⚠️ 窗口内零样本必须留痕（实测发现的隐性失效）：
    指标采集是 **60 秒一批**的。当 RULE_WINDOW_MINUTES 也是 1 分钟时，窗口与采集
    同频，窗口内样本数会在 0 和 1 之间持续振荡（逐拍实测：
    0/0 → 1/1 → 1/1 → 0/0 …）。落在 0 样本相位上的那次扫描，所有指标类规则
    （CAP-004 / DB-001 / CACHE-001 / CACHE-002）都会返回空 ——
    **与“真的没有风险”完全无法区分**。Redis 雪崩首次评测就是这么丢的：
    手查数据库 CPU 80.8%、内存 86.8% 早已越阈，而扫描结果是“无新增风险”。

    默认配置（5 分钟窗口）下窗口内稳定有 4~5 个点，不会触发；但只要有人为了
    “更快发现问题”把窗口调到 ≤ 采集周期，就会遇上。这里不自作主张改写窗口
    （那是部署方显式配的值），但必须把“没数据”和“没超阈”在日志里分开。
    """
    rows = db.fetch_all(
        "SELECT dims_json, avg FROM metrics WHERE namespace=:ns AND metric_name=:mn AND ts >= :w",
        {"ns": namespace, "mn": metric, "w": _window_ms()})
    if not rows and config.is_live():
        logger.warning(
            "指标 %s/%s 在 %d 分钟窗口内零样本，依赖它的规则本次未做判定（不等于无风险）。"
            "指标采集为 60 秒一批，RULE_WINDOW_MINUTES 建议 ≥ 2 以免与采集同频振荡",
            namespace, metric, config.RULE_WINDOW_MINUTES)
    groups: dict = {}
    for r in rows:
        dims = db.json_load(r["dims_json"]) or {}
        inst = dims.get("instanceId") or dims.get("instance") or "unknown"
        if r["avg"] is not None:
            groups.setdefault(inst, []).append(r["avg"])
    return {inst: sum(vs) / len(vs) for inst, vs in groups.items() if vs}


# --- HA 高可用类 ---

def check_ha_001():
    """多副本 Deployment 的 Pod 全部落在同一可用区。"""
    zones = _node_zones()
    pods = _pods_by_app()
    findings = []
    for ns, name, spec in _deployments():
        replicas = spec["spec"]["replicas"]
        if replicas < 2:
            continue
        pod_zones = sorted({zones.get(p["spec"].get("nodeName")) for p in pods.get(name, [])})
        if len(pod_zones) == 1:
            findings.append({
                "rule_id": "HA-001", "severity": "P1",
                "title": f"{name} 单可用区部署（{replicas} 副本全在 {pod_zones[0]}）",
                "resource_ref": f"{ns}/{name}",
                "evidence": {"replicas": replicas, "zones": pod_zones,
                             "node_affinity": bool(spec["spec"]["template"]["spec"].get("affinity"))},
                "suggestion": f"移除钉死单区的 nodeAffinity，使用 topologySpreadConstraints 将 {name} 副本分散到 >=2 个可用区",
            })
    return findings


def check_ha_002():
    """单副本 Deployment。"""
    findings = []
    for ns, name, spec in _deployments():
        if spec["spec"]["replicas"] == 1:
            findings.append({
                "rule_id": "HA-002", "severity": "P1",
                "title": f"{name} 单副本运行",
                "resource_ref": f"{ns}/{name}",
                "evidence": {"replicas": 1},
                "suggestion": f"将 {name} 的 replicas 调整为 >=2，避免 Pod 重建期间服务完全不可用",
            })
    return findings


def check_ha_003():
    """缺失存活/就绪探针。"""
    findings = []
    for ns, name, spec in _deployments():
        c = spec["spec"]["template"]["spec"]["containers"][0]
        missing = [p for p in ("livenessProbe", "readinessProbe") if p not in c]
        if missing:
            findings.append({
                "rule_id": "HA-003", "severity": "P2",
                "title": f"{name} 缺失{'/'.join(missing)}",
                "resource_ref": f"{ns}/{name}",
                "evidence": {"missing_probes": missing},
                "suggestion": f"为 {name} 容器补全 livenessProbe 与 readinessProbe",
            })
    return findings


def check_ha_004():
    """Deployment 无 PodDisruptionBudget 保护。"""
    pdb_apps = set()
    for r in db.fetch_all("SELECT spec_json FROM k8s_resources WHERE kind='PodDisruptionBudget'"):
        spec = db.json_load(r["spec_json"])
        pdb_apps.add((spec["metadata"]["namespace"], spec["spec"]["selector"]["matchLabels"].get("app")))
    findings = []
    for ns, name, spec in _deployments():
        app = spec["spec"]["template"]["metadata"].get("labels", {}).get("app", name)
        if (ns, app) not in pdb_apps:
            findings.append({
                "rule_id": "HA-004", "severity": "P2",
                "title": f"{name} 缺失 PodDisruptionBudget",
                "resource_ref": f"{ns}/{name}",
                "evidence": {"pdb_covered_apps": sorted(a for n, a in pdb_apps if n == ns)},
                "suggestion": f"为 {name} 创建 minAvailable>=1 的 PDB，防止节点维护时全部副本同时被驱逐",
            })
    return findings


# --- CAP 容量类 ---

def check_cap_001():
    """容器缺失 CPU request（键缺失而非值为 0）。"""
    findings = []
    for ns, name, spec in _deployments():
        res = spec["spec"]["template"]["spec"]["containers"][0].get("resources", {})
        if "cpu" not in res.get("requests", {}):
            findings.append({
                "rule_id": "CAP-001", "severity": "P1",
                "title": f"{name} 缺失 CPU request",
                "resource_ref": f"{ns}/{name}",
                "evidence": {"requests_keys": sorted(res.get("requests", {}))},
                "suggestion": f"为 {name} 容器补充 resources.requests.cpu，保证调度器为其预留算力",
            })
    return findings


def check_cap_002():
    """容器缺失内存 limit。"""
    findings = []
    for ns, name, spec in _deployments():
        res = spec["spec"]["template"]["spec"]["containers"][0].get("resources", {})
        if "memory" not in res.get("limits", {}):
            findings.append({
                "rule_id": "CAP-002", "severity": "P2",
                "title": f"{name} 缺失内存 limit",
                "resource_ref": f"{ns}/{name}",
                "evidence": {"limits_keys": sorted(res.get("limits", {}))},
                "suggestion": f"为 {name} 容器补充 resources.limits.memory，防止内存失控拖垮节点",
            })
    return findings


def check_cap_003():
    """命名空间 CPU 超卖率超阈值：从快照实时复算 Σ(Pod cpu limit)/Σ(节点 allocatable)，
    CMS 指标作为双源印证——快照复算保证治理（下调 limit）后能真实闭环。"""
    alloc_m = 0.0
    for r in db.fetch_all("SELECT spec_json FROM k8s_resources WHERE kind='Node'"):
        spec = db.json_load(r["spec_json"])
        alloc_m += parse_cpu_m(spec["status"]["allocatable"]["cpu"])
    limits_by_ns = {}
    for r in db.fetch_all("SELECT spec_json FROM k8s_resources WHERE kind='Pod'"):
        spec = db.json_load(r["spec_json"])
        ns = spec["metadata"].get("namespace", "default")
        for c in spec["spec"]["containers"]:
            cpu_limit = c.get("resources", {}).get("limits", {}).get("cpu")
            limits_by_ns[ns] = limits_by_ns.get(ns, 0.0) + parse_cpu_m(cpu_limit)
    cms_avg = _metric_avg("acs_k8s", "namespace.cpu.oversale_rate")
    findings = []
    for ns, limit_m in limits_by_ns.items():
        rate = limit_m / alloc_m * 100 if alloc_m else 0
        if rate > CPU_OVERSALE_THRESHOLD:
            findings.append({
                "rule_id": "CAP-003", "severity": "P1",
                "title": f"{ns} 命名空间 CPU 超卖率 {rate:.1f}%（阈值 {CPU_OVERSALE_THRESHOLD:.0f}%）",
                "resource_ref": f"namespace/{ns}",
                "evidence": {"snapshot_oversale_pct": round(rate, 2),
                             "cms_metric_avg_pct": round(cms_avg, 2),
                             "sum_pod_cpu_limit_m": round(limit_m),
                             "sum_node_allocatable_m": round(alloc_m),
                             "threshold_pct": CPU_OVERSALE_THRESHOLD},
                "suggestion": "下调各 Deployment 的 CPU limit（patch_deployment set_cpu_limit）或扩容节点，将超卖率压回 150% 以内",
            })
    return findings


def check_cap_004():
    """RDS 内存使用率高水位（按实例分组，live 窗口内）。"""
    findings = []
    for inst, v in _metric_avg_by_instance("acs_rds_dashboard", "MemoryUsage").items():
        if v > RDS_MEM_THRESHOLD:
            findings.append({
                "rule_id": "CAP-004", "severity": "P2",
                "title": f"{inst} 内存使用率常态 {round(v, 1)}%（阈值 {RDS_MEM_THRESHOLD:.0f}%）",
                "resource_ref": inst,
                "evidence": {"memory_usage_avg_pct": round(v, 2), "threshold_pct": RDS_MEM_THRESHOLD,
                             "window_minutes": config.RULE_WINDOW_MINUTES if config.is_live() else None},
                "suggestion": f"升配 RDS 实例 {inst}（upgrade_rds_instance）或优化 buffer pool 占用，当前无扩展余量",
                "resolved_by_governance": _has_governance_prefix("rds_upgrade:"),
            })
    return findings


# --- DB 数据库类 ---

def check_db_001():
    """RDS 连接使用率高水位（按实例分组；指标 + 应用日志双源印证）。"""
    findings = []
    err_cnt = db.fetch_one(
        "SELECT COUNT(*) AS c FROM app_logs WHERE message LIKE '%Too many connections%' AND ts >= :w",
        {"w": _window_s()})["c"]
    for inst, v in _metric_avg_by_instance("acs_rds_dashboard", "ConnectionUsage").items():
        if v <= RDS_CONN_THRESHOLD:
            continue
        findings.append({
            "rule_id": "DB-001", "severity": "P1",
            "title": f"{inst} 连接使用率常态 {round(v, 1)}%（阈值 {RDS_CONN_THRESHOLD:.0f}%）",
            "resource_ref": inst,
            "evidence": {"connection_usage_avg_pct": round(v, 2),
                         "too_many_connections_errors": err_cnt,
                         "window_minutes": config.RULE_WINDOW_MINUTES if config.is_live() else None},
            "suggestion": "治理慢查询释放连接占用（根因），必要时上调 max_connections（upgrade_rds_instance）或接入连接池中间件",
            "resolved_by_governance": _has_governance_prefix("db_index:") or _has_governance_prefix("rds_upgrade:"),
        })
    return findings


def check_db_002():
    """无索引慢查询：rows_examined 百万级且 rows_sent 个位数（按实例分组，live 窗口内）。"""
    rows = db.fetch_all(
        f"""SELECT instance_id, sql_text, COUNT(*) AS cnt,
                   MIN(rows_examined) AS min_examined, MAX(rows_sent) AS max_sent
            FROM slow_logs
            WHERE governed=0 AND rows_examined >= {SLOW_ROWS_EXAMINED} AND rows_sent < 10
              AND ts >= :w
            GROUP BY instance_id, sql_text""", {"w": _window_s()})
    if not rows:
        return []
    findings = []
    by_inst: dict = {}
    for r in rows:
        by_inst.setdefault(r["instance_id"], []).append(r)
    for inst, group in by_inst.items():
        total = sum(r["cnt"] for r in group)
        findings.append({
            "rule_id": "DB-002", "severity": "P2",
            "title": f"{inst} 存在无索引慢查询（{len(group)} 种 SQL 共 {total} 条，全表扫描）",
            "resource_ref": inst,
            "evidence": {"slow_sql_kinds": len(group), "slow_log_count": total,
                         "min_rows_examined": min(r["min_examined"] for r in group),
                         "max_rows_sent": max(r["max_sent"] for r in group),
                         "sample_sql": group[0]["sql_text"][:120],
                         "window_minutes": config.RULE_WINDOW_MINUTES if config.is_live() else None},
            "suggestion": "为 orders 表创建复合索引 orders(status, created_at)（create_db_index），消除全表扫描",
        })
    return findings


# --- API 接口质量类 ---

def _min_samples() -> int:
    """接口分位数判定所需的最小样本数（随窗口时长缩放）。

    原来硬编码为 20。在默认的 5 分钟窗口下这个值是合理的 ——
    低流量接口窗口内几十个样本，1 个偶发错误就超 1%，会造成二项抖动误报。

    但它**不随 RULE_WINDOW_MINUTES 缩放**，于是窗口调小就会静默失效：
    实测把窗口从 5 分钟改成 1 分钟后，15 个接口里有 9 个（60%）样本数掉到
    20 以下、被直接跳过 —— API-001 对这些接口彻底不生效，
    而日志里没有任何提示。运维为了"更快发现问题"调小窗口，
    反而丢掉了大半覆盖面，这是最难查的一类问题。

    改为按窗口线性缩放（每分钟 4 个样本），5 分钟仍得 20 ——
    **默认行为完全不变**，只修正窗口被调小时的静默丢失。
    """
    if not config.is_live():
        return 0
    return max(5, 4 * config.RULE_WINDOW_MINUTES)


def check_api_001():
    """接口 P99/错误率劣化（live 窗口内；样本过少的接口跳过防抖动误报）。"""
    w = _window_s()
    groups = db.fetch_all(
        "SELECT method, url, COUNT(*) AS cnt FROM ingress_logs WHERE ts >= :w GROUP BY method, url",
        {"w": w})
    findings = []
    min_samples = _min_samples()
    skipped = []
    for g in groups:
        if g["cnt"] < min_samples:          # 窗口内样本过少，分位数不稳定
            skipped.append(f"{g['method']} {g['url']}({g['cnt']})")
            continue
        rows = db.fetch_all(
            "SELECT request_time, status FROM ingress_logs WHERE method=:m AND url=:u AND ts >= :w",
            {"m": g["method"], "u": g["url"], "w": w})
        ts = sorted(r["request_time"] for r in rows)
        p99 = ts[math.ceil(0.99 * len(ts)) - 1]
        err_cnt = sum(1 for r in rows if r["status"] >= 500)
        err = err_cnt / len(rows)
        # 错误率判定附加最小错误数：低流量接口窗口内几十个样本，1 个偶发错误就超 1%，
        # 二项抖动会造成稳态误报（live 实测：60 样本 1 错 = 1.7%）
        err_hit = err >= API_ERR_THRESHOLD and err_cnt >= 3
        if p99 >= API_P99_THRESHOLD_S or err_hit:
            api = f"{g['method']} {g['url']}"
            findings.append({
                "rule_id": "API-001", "severity": "P1",
                "title": f"{api} P99 达 {p99:.3f}s 且错误率 {err * 100:.2f}%",
                "resource_ref": api,
                "evidence": {"requests": g["cnt"], "p99_s": round(p99, 3),
                             "error_rate_pct": round(err * 100, 2),
                             "thresholds": {"p99_s": API_P99_THRESHOLD_S, "error_rate_pct": API_ERR_THRESHOLD * 100},
                             "window_minutes": config.RULE_WINDOW_MINUTES if config.is_live() else None},
                "suggestion": "结合 Trace 下钻该接口调用链定位瓶颈环节，治理根因后观察恢复",
                "resolved_by_governance": _has_governance_prefix("db_index:"),
            })
    if skipped:
        # 被跳过的接口必须留痕：否则"规则没报"与"规则没跑"分不开
        logger.info("API-001 跳过 %d 个低流量接口（样本 < %d）：%s",
                    len(skipped), min_samples, ", ".join(skipped[:6]))
    return findings


# --- CACHE 缓存类 ---
#
# 这两条是量化评测发现缓存层完全无规则覆盖后补的。
# 为什么没有第三条（连接使用率）：Redis 稳态连接使用率 18%、max_connections 10000，
# 现有任何注入场景都不会把它推到阈值以上 —— 写了就是一条永远不会被触发、
# 也无法被评测验证的死规则，只能把「规则条数」这个数字刷好看。宁可只上两条。

def check_cache_001():
    """缓存实例内存使用率高水位（按实例分组，live 窗口内）。

    阈值 80% 比 RDS 的 85%（CAP-004）更保守，因为两者越阈后的后果不同量级：
    RDS 内存高主要是性能劣化；缓存实例内存接近 maxmemory 会开始成批驱逐 key
    （或写入直接失败），命中率塔式下跌，请求全量击穿到后端 ——
    缓存是多个服务共享的依赖，一个实例失效会同时放大成多条链路的故障，所以定 P1。
    """
    findings = []
    for inst, v in _metric_avg_by_instance("acs_kvstore", "MemoryUsage").items():
        if v > CACHE_MEM_THRESHOLD:
            findings.append({
                "rule_id": "CACHE-001", "severity": "P1",
                "title": f"{inst} 内存使用率 {round(v, 1)}%（阈值 {CACHE_MEM_THRESHOLD:.0f}%）",
                "resource_ref": inst,
                "evidence": {"memory_usage_avg_pct": round(v, 2), "threshold_pct": CACHE_MEM_THRESHOLD,
                             "window_minutes": config.RULE_WINDOW_MINUTES if config.is_live() else None},
                "suggestion": f"确认 {inst} 的 maxmemory-policy 与大 key 分布；内存压力持续则升配实例。"
                              f"若同时伴随下游数据库负载上升，优先排查缓存命中率而不是直接升配数据库",
            })
    return findings


def check_cache_002():
    """缓存实例 CPU 使用率高水位（按实例分组，live 窗口内）。

    阈值取 70%而不是 85%：Redis 命令执行是单线程的，CPU 到 70% 已经接近拐点
    （主线程排队、所有命令 RT 齐涨），等到 85% 时下游已经大面积超时了。
    与 CACHE-001 拆成两条而不是合并，是因为治理手段不同：
    内存高 → 看 key 规模/驱逐策略；CPU 高 → 看慢命令、热 key、连接风暴。
    合并成一条会让 suggestion 只能写成模糊的「看看缓存」。
    """
    findings = []
    for inst, v in _metric_avg_by_instance("acs_kvstore", "CpuUsage").items():
        if v > CACHE_CPU_THRESHOLD:
            findings.append({
                "rule_id": "CACHE-002", "severity": "P1",
                "title": f"{inst} CPU 使用率 {round(v, 1)}%（阈值 {CACHE_CPU_THRESHOLD:.0f}%，单线程模型）",
                "resource_ref": inst,
                "evidence": {"cpu_usage_avg_pct": round(v, 2), "threshold_pct": CACHE_CPU_THRESHOLD,
                             "window_minutes": config.RULE_WINDOW_MINUTES if config.is_live() else None},
                "suggestion": f"排查 {inst} 是否存在慢命令（KEYS/大范围 SCAN）、热 key 或瞬时请求风暴；"
                              f"缓存命中率下跌引起的反复回源也会把 CPU 抬高，需与内存水位一并看",
            })
    return findings


BUILTIN_CHECKS = [
    check_ha_001, check_ha_002, check_ha_003, check_ha_004,
    check_cap_001, check_cap_002, check_cap_003, check_cap_004,
    check_db_001, check_db_002, check_api_001,
    check_cache_001, check_cache_002,
]

BUILTIN_RULE_META = [
    ("HA-001", "P1", "多副本 Deployment 单可用区部署", "check_ha_001"),
    ("HA-002", "P1", "Deployment 单副本运行", "check_ha_002"),
    ("HA-003", "P2", "缺失存活/就绪探针", "check_ha_003"),
    ("HA-004", "P2", "缺失 PodDisruptionBudget", "check_ha_004"),
    ("CAP-001", "P1", "缺失 CPU request", "check_cap_001"),
    ("CAP-002", "P2", "缺失内存 limit", "check_cap_002"),
    ("CAP-003", "P1", "命名空间 CPU 超卖率 >150%", "check_cap_003"),
    ("CAP-004", "P2", "RDS 内存使用率 >85%", "check_cap_004"),
    ("DB-001", "P1", "RDS 连接使用率 >80%", "check_db_001"),
    ("DB-002", "P2", "无索引慢查询（全表扫描）", "check_db_002"),
    ("API-001", "P1", "接口 P99>1s 或错误率>1%", "check_api_001"),
    ("CACHE-001", "P1", "缓存实例内存使用率 >80%", "check_cache_001"),
    ("CACHE-002", "P1", "缓存实例 CPU 使用率 >70%", "check_cache_002"),
]


def seed_builtin_rules():
    """把内置规则元信息写入 risk_rules 表（幂等）。"""
    for rule_id, severity, title, func_name in BUILTIN_RULE_META:
        if not db.fetch_one("SELECT id FROM risk_rules WHERE rule_id=:r", {"r": rule_id}):
            db.execute(
                """INSERT INTO risk_rules (rule_id, source, severity, title, check_type, check_ref, enabled)
                   VALUES (:r, 'builtin', :s, :t, 'python', :f, 1)""",
                {"r": rule_id, "s": severity, "t": title, "f": func_name})


def run_builtin_checks() -> list:
    """执行全部启用的内置规则，返回 finding 列表。"""
    enabled = {r["check_ref"] for r in db.fetch_all(
        "SELECT check_ref FROM risk_rules WHERE source='builtin' AND enabled=1")}
    findings = []
    for fn in BUILTIN_CHECKS:
        if not enabled or fn.__name__ in enabled:
            findings.extend(fn())
    return findings
