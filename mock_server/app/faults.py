"""U3-a 故障场景注入与拓扑反向 BFS 传播

传播模型（KTD-5）：被依赖者故障 → 调用方劣化。沿 EDGES 的**反向**边做 BFS，
每跳效应 × PROPAGATION_DECAY（0.6），最多 PROPAGATION_MAX_HOPS（3）跳。
例：rds-mysql-order 故障 → order-service（1 跳，×0.6）→ api-gateway（2 跳，×0.36）
    → web-frontend（3 跳，×0.216）→ nginx-ingress 超出跳数不再传播。

每个场景声明：
- target        : 故障源实体
- direct        : 对故障源本身的效应（指标 → add/mul）
- propagate     : 传播到上游服务时施加的效应模板（会被逐跳衰减）
- world_mutation: 对世界可变状态的修改（如追加慢查询表、下线实例）
- recover_actions: 该故障对应的治理动作类型（供 backend 生成 check 列表时参考）
- expected_rules : 该故障【应当】触发的规则（评测的 ground truth）

评测相关字段（eval/ 下的量化评测会读）：
- expected_instance     : 真正的根因实体，用于判定"是否定位到了正确实例"
- must_not_flag_instance: 同型的另一个实例；它被一起告警即为串报
- expected_table        : 精确到表的根因（慢查询类场景）
- difficulty            : easy/medium/hard，用于分层统计准确率
- confusable_with       : 现象相似但根因不同的场景，成对评测才能看出是否在套模板
- expect_no_new_findings: 负样本标记。expected_rules 为空【且】此标记为真时，
                          表示"故意不该报出任何新增风险"，与"漏写真值"区分开
"""
from __future__ import annotations

import time
from collections import deque
from typing import Dict, List

from . import config, world_def as W

SCENARIOS: Dict[str, dict] = {
    "rds_conn_spike": {
        "title": "RDS 连接数打满",
        "target": "rds-mysql-order",
        "description": "订单库连接池被瞬时打满，连接使用率飙至 92%，内存同步升高；"
                       "上游 order/payment/inventory 服务获取连接失败，错误率与时延双升。",
        "direct": {
            "conn_pct": {"add": 37.0},      # 稳态 55 → 92（阈值 80）
            "mem_pct": {"add": 21.0},       # 稳态 70 → 91（阈值 85）
            "latency_ms": {"add": 700.0},   # 使上游接口 P99 突破 1s（API-001 阈值）
            "error_rate": {"add": 0.035},
        },
        "propagate": {
            "latency_ms": {"add": 800.0},
            "error_rate": {"add": 0.030},
            "cpu_pct": {"add": 9.0},
        },
        "recover_actions": ["upgrade_rds", "create_db_index"],
        "expected_rules": ["DB-001", "CAP-004", "API-001"],
        "expected_instance": "rds-mysql-order",
        "must_not_flag_instance": "rds-mysql-core",
        "difficulty": "easy",
        "confusable_with": "core_db_conn_exhausted",
    },
    "slow_query_storm": {
        "title": "慢查询风暴（orders 表全表扫描）",
        "target": "rds-mysql-order",
        "description": "orders 表缺失 (status, created_at) 复合索引，三条 SQL 全表扫描，"
                       "单次扫描 100 万+ 行；连接被长期占用，下单接口 P99 破 1.5s。",
        "direct": {
            "conn_pct": {"add": 28.0},
            "cpu_pct": {"add": 22.0},
            "mem_pct": {"add": 18.0},
            "error_rate": {"add": 0.020},
        },
        "propagate": {
            "latency_ms": {"add": 400.0},
            "error_rate": {"add": 0.022},
        },
        "world_mutation": {"slow_query_tables_add": ("rds-mysql-order", "orders")},
        "recover_actions": ["create_db_index"],
        # CAP-004 是量化评测反过来发现的漏标：本场景 direct 里 mem_pct +18，
        # 稳态 70% → 88%，必然越过 CAP-004 的 85% 阈值。
        # 原真值只写了三条，评测便把这条正确的告警算成误报（P 从 100% 掉到 75%）。
        # 教训：真值也要复核 —— 拿错的真值去判对错，会把系统的正确行为定成缺陷。
        "expected_rules": ["DB-002", "DB-001", "API-001", "CAP-004"],
        "expected_instance": "rds-mysql-order",
        "expected_table": "orders",
        "difficulty": "easy",
        "confusable_with": "inventory_slow_query",
    },
    "pod_oom_crash": {
        "title": "Pod 内存溢出崩溃重启",
        "target": "payment-service",
        "description": "支付服务缺失内存 limit（CAP-002），内存持续增长触发 OOMKilled，"
                       "实例反复重启；存活实例分摊双倍流量，CPU 与时延升高。",
        "direct": {
            "mem_pct": {"add": 44.0},
            "cpu_pct": {"add": 26.0},
            "latency_ms": {"add": 900.0},   # 使 payment 类接口 P99 突破 1s
            "error_rate": {"add": 0.045},
        },
        "propagate": {
            "latency_ms": {"add": 700.0},
            "error_rate": {"add": 0.030},
        },
        "world_mutation": {"kill_one_instance": "payment-service", "restart_loop": True},
        "recover_actions": ["patch_resources", "restart_pod", "scale_out"],
        "expected_rules": ["API-001"],
        "expected_instance": "payment-service",
        "difficulty": "medium",
    },
    "instance_down": {
        "title": "服务实例整体下线",
        "target": "order-service",
        "description": "订单服务唯一副本（HA-002 单副本缺陷）被驱逐后无法调度，"
                       "所有下单相关接口 100% 失败，拓扑边全红。"
                       "注意：本场景是**快速失败**（connection refused），P99 不会破 1s，"
                       "靠 100% 错误率触发 API-001（规则为 P99 或 错误率越阈的或逻辑）。",
        "direct": {
            "error_rate": {"add": 1.0},
            "latency_ms": {"add": 60.0},
        },
        "propagate": {
            "error_rate": {"add": 0.35},
            "latency_ms": {"add": 120.0},
        },
        "world_mutation": {"down_all_instances": "order-service"},
        "recover_actions": ["scale_out", "restart_pod"],
        "expected_rules": ["API-001", "HA-002"],
        "expected_instance": "order-service",
        "difficulty": "easy",
        "confusable_with": "dependency_timeout",
    },
    # ══════════════════════════════════════════════════════════════════
    # 以下 8 个场景为【评测扩充】。原有 4 个场景只覆盖 5 类规则、且都集中在
    # order 库与 order/payment 服务上，做量化评测样本量不足、也缺少
    # 「现象相似但根因不同」的样本 —— 那恰恰是最能检验 Agent 是否在推理
    # 而不是套模板的一类。扩充遵循三条：
    #   ① 同型不同实例（测实例定位精度、不串报）；
    #   ② 易混淆对（测能否区分表层现象与真实根因）；
    #   ③ 负样本（有异常但未越阈，测会不会误报）。
    #
    # 每个场景的数值都是按稳态基线算过的，确保真的越阈（或真的不越阈）。
    # ══════════════════════════════════════════════════════════════════

    "core_db_conn_exhausted": {
        "title": "core 库连接耗尽（与 order 库同型故障）",
        "target": "rds-mysql-core",
        "description": "用户库/商品库连接池耗尽，连接使用率 41%→87%、内存 62%→88%；"
                       "user/product 服务取连接失败，登录与商品列表接口 P99 破 1s。"
                       "本场景与 rds_conn_spike 症状同型但实例不同，"
                       "用于检验风险定位是否精确到实例（另一台 order 库不应告警）。",
        "direct": {
            "conn_pct": {"add": 46.0},      # 41 → 87（阈值 80）
            "mem_pct": {"add": 26.0},       # 62 → 88（阈值 85）
            "cpu_pct": {"add": 30.0},
            "latency_ms": {"add": 800.0},   # 经 db_calls 放大到 user/product 接口
            "error_rate": {"add": 0.030},
        },
        "propagate": {
            "latency_ms": {"add": 900.0},
            "error_rate": {"add": 0.030},
        },
        "recover_actions": ["upgrade_rds", "create_db_index"],
        "expected_rules": ["DB-001", "CAP-004", "API-001"],
        "expected_instance": "rds-mysql-core",
        "must_not_flag_instance": "rds-mysql-order",   # 串报即为误报
        "difficulty": "easy",
        "confusable_with": "rds_conn_spike",
    },
    "inventory_slow_query": {
        "title": "库存表慢查询（与 orders 表慢查询同库不同表）",
        "target": "rds-mysql-order",
        "description": "inventory 表缺索引导致扣减库存的 SQL 全表扫描，"
                       "/api/inventory 与 /api/inventory/deduct P99 破 1s；"
                       "连接被占用推高 order 库连接率。与 slow_query_storm 同库但表不同，"
                       "用于检验根因是否精确到【表】而不是笼统说“数据库慢”。",
        "direct": {
            "conn_pct": {"add": 28.0},      # 55 → 83（阈值 80）
            "cpu_pct": {"add": 18.0},
            "error_rate": {"add": 0.015},
        },
        "propagate": {
            "latency_ms": {"add": 350.0},
            "error_rate": {"add": 0.015},
        },
        "world_mutation": {"slow_query_tables_add": ("rds-mysql-order", "inventory")},
        "recover_actions": ["create_db_index"],
        "expected_rules": ["DB-002", "DB-001", "API-001"],
        "expected_instance": "rds-mysql-order",
        "expected_table": "inventory",
        "difficulty": "medium",
        "confusable_with": "slow_query_storm",
    },
    "redis_cache_avalanche": {
        "title": "Redis 缓存雪崩（击穿到数据库）",
        "target": "kvstore-redis-01",
        "description": "缓存集中过期导致请求全部击穿到后端，Redis CPU 16%→78%、内存 42%→86%；"
                       "user/product/payment 服务因缓存未命中而变慢，多个接口 P99 破 1s。"
                       "※ 表层现象与数据库故障几乎一样（接口慢 + 后端负载升高），"
                       "但根因在缓存层 —— 用于检验是否会误判成数据库问题。",
        "direct": {
            "cpu_pct": {"add": 62.0},       # 16 → 78
            "mem_pct": {"add": 44.0},       # 42 → 86
            "latency_ms": {"add": 40.0},
            "error_rate": {"add": 0.040},
        },
        "propagate": {
            "latency_ms": {"add": 1500.0},  # ×0.6 → +900，user/product 接口破 1s
            "error_rate": {"add": 0.045},
            "cpu_pct": {"add": 22.0},
        },
        # 实测修正：scale_out 对缓存实例直接返回 "service not found: kvstore-redis-01"
        # （它只认 service_state 里的 K8s 服务），原来把它列进 recover_actions 是错的声明。
        # upgrade_rds 才真正被接受（实测 cpu 76.3→14.7、mem 87.0→41.1 全部回稳态）。
        "recover_actions": ["upgrade_rds"],
        # CACHE-001/002 是本场景暴露缓存层无规则覆盖后新增的：
        # 雪崩后 Redis mem 42→86%、cpu 16→78%，分别越过 80% / 70% 阈值。
        # 但注意：新规则让根因在**规则层**浮现了，并不意味着这个场景变简单 ——
        # 它仍然要求 Agent 能在“接口慢 + 后端负载升高”的表象下不把根因误定到数据库。
        "expected_rules": ["API-001", "CACHE-001", "CACHE-002"],
        "expected_instance": "kvstore-redis-01",
        "difficulty": "hard",
        "confusable_with": "core_db_conn_exhausted",
    },
    "dependency_timeout": {
        "title": "第三方支付网关超时（慢失败）",
        "target": "payment-service",
        "description": "外部支付网关响应劣化，支付服务同步等待导致 P99 从 320ms 涨到 1.6s，"
                       "错误率升至 6%。与 instance_down 形成对照：那个是快失败"
                       "（连接直接拒绝、P99 不涨），这个是慢失败（P99 暴涨）。"
                       "用于检验是否会把两类完全不同的故障给出同一套结论。",
        "direct": {
            "latency_ms": {"add": 1300.0},  # 320 → 1620
            "error_rate": {"add": 0.060},
            "cpu_pct": {"add": 14.0},
        },
        "propagate": {
            "latency_ms": {"add": 700.0},
            "error_rate": {"add": 0.030},
        },
        "recover_actions": ["scale_out", "restart_pod"],
        "expected_rules": ["API-001"],
        "expected_instance": "payment-service",
        "difficulty": "medium",
        "confusable_with": "instance_down",
    },
    "product_cpu_saturation": {
        "title": "商品服务 CPU 饱和（计算瓶颈，非数据库）",
        "target": "product-service",
        "description": "商品详情页计算逻辑劣化，CPU 33%→87%，商品接口 P99 从 90ms 涨到 1.1s。"
                       "数据库侧一切正常 —— 用于检验是否会习惯性把接口慢都归到数据库。",
        "direct": {
            "cpu_pct": {"add": 54.0},       # 33 → 87
            "latency_ms": {"add": 1000.0},  # 90/120 → 1090/1120
            "error_rate": {"add": 0.020},
        },
        "propagate": {
            "latency_ms": {"add": 400.0},
        },
        "recover_actions": ["scale_out", "patch_resources"],
        "expected_rules": ["API-001"],
        "expected_instance": "product-service",
        "difficulty": "medium",
    },
    "traffic_surge": {
        "title": "流量突增（容量不足，不是故障）",
        "target": "order-service",
        "description": "营销活动导致下单 QPS 涨到 3.6 倍，单副本的 order-service 扛不住，"
                       "CPU 42%→84%、下单接口 P99 破 1s。"
                       "⚠️ 这不是“坏了”，而是容量不够 —— 正确结论应指向扩容，"
                       "而不是去查代码缺陷或数据库。用于检验能否区分容量问题与故障。",
        "direct": {
            "qps": {"mul": 3.6},
            "cpu_pct": {"add": 42.0},       # 42 → 84
            "latency_ms": {"add": 800.0},   # 260/340 → 1060/1140
            "error_rate": {"add": 0.012},
        },
        "propagate": {
            "latency_ms": {"add": 300.0},
        },
        "recover_actions": ["scale_out"],
        "expected_rules": ["API-001"],
        "expected_instance": "order-service",
        "difficulty": "hard",
    },
    "frontend_bandwidth_saturation": {
        "title": "前端服务带宽打满",
        "target": "web-frontend",
        "description": "静态资源未走 CDN，web-frontend 出口带宽逼近 500Mbps 上限，"
                       "热门商品接口 P99 从 60ms 涨到 1s 以上。"
                       "考察维度是网络，而非 CPU/内存/数据库。",
        "direct": {
            "bandwidth_mbps": {"add": 360.0},   # 120 → 480 / 上限 500
            "latency_ms": {"add": 1000.0},
            "error_rate": {"add": 0.030},
        },
        "propagate": {
            "latency_ms": {"add": 300.0},
        },
        "recover_actions": ["scale_out"],
        "expected_rules": ["API-001"],
        "expected_instance": "web-frontend",
        "difficulty": "medium",
    },
    "memory_creep_no_breach": {
        "title": "内存缓慢上涨但未越阈（负样本）",
        "target": "inventory-service",
        "description": "库存服务内存从 38% 涨到 68%，确实在上升，但【没有越过任何阈值】，"
                       "接口时延与错误率均正常。"
                       "⚠️ 这是负样本：正确行为是【不产生任何新增风险】。"
                       "如果扫描报出了新风险，或 Agent 声称发现故障，就是误报。"
                       "运维场景里误报的代价极高 —— 报多了人就不看了。",
        "direct": {
            "mem_pct": {"add": 30.0},       # 38 → 68，阈值内
            "cpu_pct": {"add": 10.0},       # 26 → 36，阈值内
        },
        "recover_actions": ["restart_pod", "patch_resources"],
        # 显式声明"故意为空"，与"漏写"区分开（校验用例据此放行空列表）
        "expected_rules": [],
        "expect_no_new_findings": True,
        "expected_instance": "inventory-service",
        "difficulty": "hard",
    },
}


# 评测用的可选字段：一并通过 HTTP 暴露，这样评测端不必再 import 本模块、
# 也就不会出现"评测里抄了一份真值、场景改了却没同步"的漂移。
_EVAL_FIELDS = ("expected_instance", "must_not_flag_instance", "expected_table",
                "difficulty", "confusable_with", "expect_no_new_findings")


def list_scenarios() -> List[dict]:
    out = []
    for sid, s in SCENARIOS.items():
        item = {"id": sid, "title": s["title"], "target": s["target"],
                "description": s["description"],
                "recover_actions": s["recover_actions"],
                "expected_rules": s["expected_rules"]}
        for f in _EVAL_FIELDS:
            if f in s:
                item[f] = s[f]
        out.append(item)
    return out


def _upstream_bfs(start: str) -> List[tuple]:
    """沿调用边反向 BFS，返回 [(service, hop)]，hop 从 1 起，最多 MAX_HOPS。"""
    out, seen = [], {start}
    q = deque([(start, 0)])
    while q:
        node, hop = q.popleft()
        if hop >= config.PROPAGATION_MAX_HOPS:
            continue
        for up in W.UPSTREAM.get(node, []):
            if up in seen:
                continue
            seen.add(up)
            out.append((up, hop + 1))
            q.append((up, hop + 1))
    return out


def inject(engine, scenario_id: str) -> dict:
    """整段持锁：这是「读状态 → 改 effects/service_state → 记录事件」的复合操作，
    中途被 tick 插入会让世界状态与 effects 不一致。"""
    with engine.lock:
        return _inject_locked(engine, scenario_id)


def _inject_locked(engine, scenario_id: str) -> dict:
    if scenario_id not in SCENARIOS:
        return {"error": f"unknown scenario: {scenario_id}"}
    sc = SCENARIOS[scenario_id]
    fault_id = f"F-{time.strftime('%Y%m%d')}-{len(engine.active_faults) + 1:03d}"
    source = f"fault:{fault_id}"
    target = sc["target"]
    affected = [target]

    # 1. 故障源直接效应
    for key, eff in sc["direct"].items():
        engine.set_effect(target, key, add=eff.get("add", 0.0), mul=eff.get("mul", 1.0),
                          source=source)

    # 2. 沿反向边逐跳衰减传播
    for svc, hop in _upstream_bfs(target):
        factor = config.PROPAGATION_DECAY ** hop
        for key, eff in sc.get("propagate", {}).items():
            engine.set_effect(svc, key,
                              add=eff.get("add", 0.0) * factor,
                              mul=1.0 + (eff.get("mul", 1.0) - 1.0) * factor,
                              source=source)
        affected.append(svc)

    # 3. 世界状态变更（慢查询表 / 实例下线 / 重启）
    mut = sc.get("world_mutation", {})
    if "slow_query_tables_add" in mut:
        ds, table = mut["slow_query_tables_add"]
        st = engine.datastore_state[ds]
        if table not in st["slow_query_tables"]:
            st["slow_query_tables"].append(table)
        if table in st["indexed_tables"]:
            st["indexed_tables"].remove(table)
    if "kill_one_instance" in mut:
        svc = mut["kill_one_instance"]
        pods = [i for i in engine.instances.values()
                if i["service"] == svc and i["kind"] == "pod" and i.get("healthy", True)]
        if len(pods) > 1:
            victim = pods[0]
            victim["healthy"] = False
            victim["restart_count"] += 1
            engine.service_state[svc]["healthy_replicas"] -= 1
            engine.emit_k8s_event("Warning", "OOMKilling",
                                  f"Memory cgroup out of memory: Killed process in {victim['instance']}",
                                  "Pod", victim["instance"])
            engine.emit_k8s_event("Warning", "BackOff",
                                  f"Back-off restarting failed container in pod {victim['instance']}",
                                  "Pod", victim["instance"])
    if "down_all_instances" in mut:
        svc = mut["down_all_instances"]
        for inst in engine.instances.values():
            if inst["service"] == svc and inst["kind"] == "pod":
                inst["healthy"] = False
                inst["status"] = "faulty"
        engine.service_state[svc]["healthy_replicas"] = 0
        engine.emit_k8s_event("Warning", "FailedScheduling",
                              f"0/6 nodes are available: insufficient resources for {svc}",
                              "Deployment", svc)

    fault = {"fault_id": fault_id, "scenario_id": scenario_id, "title": sc["title"],
             "target": target, "affected": affected, "status": "active",
             "since_tick": engine.tick_no, "since_ts": engine.tick_ts_ms,
             "expected_rules": sc["expected_rules"],
             "recover_actions": sc["recover_actions"]}
    engine.active_faults.append(fault)
    engine.record_audit("fault_inject", f"{fault_id} {scenario_id} target={target} "
                                        f"affected={len(affected)}")
    return {"fault_id": fault_id, "status": "active", "affected": affected,
            "world_version": engine.world_version,
            "expected_rules": sc["expected_rules"]}


def recover(engine, fault_id: str) -> dict:
    """整段持锁：这是「读状态 → 改 effects/service_state → 记录事件」的复合操作，
    中途被 tick 插入会让世界状态与 effects 不一致。"""
    with engine.lock:
        return _recover_locked(engine, fault_id)


def _recover_locked(engine, fault_id: str) -> dict:
    fault = next((f for f in engine.active_faults if f["fault_id"] == fault_id), None)
    if not fault:
        return {"error": f"fault not found or already recovered: {fault_id}"}
    sc = SCENARIOS[fault["scenario_id"]]

    # 效应转入指数衰减（而非立刻归零），指标平滑回落更真实
    for entity in list(engine.effects):
        for key, e in engine.effects[entity].items():
            if e.get("source") == f"fault:{fault_id}":
                e["decay_from_tick"] = engine.tick_no

    # 回滚世界状态变更
    mut = sc.get("world_mutation", {})
    if "slow_query_tables_add" in mut:
        ds, table = mut["slow_query_tables_add"]
        st = engine.datastore_state[ds]
        if table in st["slow_query_tables"]:
            st["slow_query_tables"].remove(table)
    for key in ("kill_one_instance", "down_all_instances"):
        if key in mut:
            svc = mut[key]
            for inst in engine.instances.values():
                if inst["service"] == svc and inst["kind"] == "pod":
                    inst["healthy"] = True
                    inst["status"] = "normal"
            engine.service_state[svc]["healthy_replicas"] = engine.service_state[svc]["replicas"]
            engine.emit_k8s_event("Normal", "Started",
                                  f"Started container {svc} after recovery", "Pod", svc)

    fault["status"] = "recovering"
    fault["recovered_tick"] = engine.tick_no
    engine.active_faults = [f for f in engine.active_faults if f["fault_id"] != fault_id]
    engine.record_audit("fault_recover", f"{fault_id} {fault['scenario_id']} → recovering")
    return {"fault_id": fault_id, "status": "recovering", "world_version": engine.world_version}
