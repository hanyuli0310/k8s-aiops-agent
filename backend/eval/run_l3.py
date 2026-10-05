"""L3：复杂度压力评测 —— 找出 Harness 增益到底出现在哪里。

## 为什么需要这一层

L2（单故障排障）实测发现：**朴素基线打平且更快**（同一场景 1/1 vs 1/1、
137s vs 338s）。读基线回答原文可见它质量很高 —— Trace 排除法、时间点对齐、
拓扑佐证一应俱全。结论只能是：在「30 步内能解决、中间数据量不大」的场景上，
提示词层方法论对强模型（qwen3.8-max）的增益接近 0。

这不等于 Harness 无用，而是说明**增益不均匀分布**。L3 专门压这四个维度：

  D1 长任务      步数需求超单段上限 → 基线无 update_plan/续跑，只能中断
  D2 大数据量    单个工具结果就能挤爆上下文 → 基线无落盘分页/压缩/子 Agent 隔离
  D3 强干扰      多个独立故障并发 → 基线无方法论准则，容易串成一个根因
  D4 易幻觉      要求大量精确数值 → 基线无事实核对，编造的数字原样交付

## 诚信约束（比结果更重要）

1. **不为了让 Harness 赢而设计场景**。这四个维度全部取自真实运维：
   多故障并发、全量日志聚合、跨窗口对比、按依赖顺序治理，都是 SRE 日常。
2. **判定机械可复现**。数值类真值一律当场用 SQL 算（`_truth_*` 函数），
   绝不写死期望值 —— 写死的数字会随数据漂移，那种"失败"是假失败。
3. **结果不利也照报**。若某维度上基线仍打平，就写"该维度无增益"。
   知道"什么时候不需要这套东西"对读者同样有价值。

用法：
    # 全部 4 个 case，两臂对比
    .venv/bin/python eval/run_l3.py --compare-baseline --json /tmp/l3.json
    # 只跑某几个
    .venv/bin/python eval/run_l3.py --only multi_fault,log_aggregate --compare-baseline
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db                                       # noqa: E402
from app.agents import base                                      # noqa: E402
from app.harness import scheduler                                # noqa: E402
from app.harness.llm import QuotaExhausted                        # noqa: E402
from app.harness.runctx import RunContext                        # noqa: E402
from app.tools import registry                                   # noqa: E402

MOCK = os.getenv("MOCK_BASE_URL", "http://127.0.0.1:9001")


# ══════════════════════════════════════════════════════════
# 真值计算：一律当场从库里算，不写死
# ══════════════════════════════════════════════════════════

# ⚠️ 本库里时间戳单位**不统一**，写真值查询时必须逐表确认：
#     metrics / realtime_metrics ...... 毫秒
#     ingress_logs / trace_spans / app_logs / slow_logs / k8s_events ...... 秒
# 踩过的坑：D2 的真值查询按毫秒去过滤 ingress_logs（秒），条件恒为假、
# 查询恒返回 0，于是真值快照只剩一个「5xx 总条数 = 0」。而判定容差是 ±1，
# 回答里任何接近 0 的数字都能蒙对 —— **假通过**，比查不出来更糟。
_LOG_TABLES_IN_SECONDS = True


def _log_window_start(minutes: int) -> int:
    """日志类表（秒级 ts）的窗口起点。"""
    return int(time.time()) - minutes * 60


def _truth_error_groups(minutes: int = 30) -> dict:
    """5xx 错误按 URL 分组的真值（供 D2 日志聚合核对）。

    按 url 而不是"服务"分组：ingress_logs 表里没有 backend_service 列，
    服务归属要经 APIS 映射反查。真值必须是**一条 SQL 能直接算出来的东西**，
    多绕一层映射就多一个可能出错的环节，而判定错比被测系统错更糟。
    """
    rows = db.fetch_all(
        """SELECT url, COUNT(*) AS n
           FROM ingress_logs
           WHERE status >= 500 AND ts >= :w
           GROUP BY url ORDER BY n DESC""",
        {"w": _log_window_start(minutes)})
    return {r["url"]: r["n"] for r in rows if r["url"]}


def _truth_total_5xx(minutes: int = 30) -> int:
    r = db.fetch_one(
        "SELECT COUNT(*) AS n FROM ingress_logs WHERE status >= 500 AND ts >= :w",
        {"w": _log_window_start(minutes)})
    return int((r or {}).get("n") or 0)


def _truth_instance_metric(instance_kw: str, metric: str) -> float:
    """某实例某指标的最新值（供 D4 数值核对）。"""
    r = db.fetch_one(
        """SELECT avg FROM metrics
           WHERE metric_name = :m AND dims_json LIKE :kw
           ORDER BY ts DESC LIMIT 1""",
        {"m": metric, "kw": f"%{instance_kw}%"})
    return float((r or {}).get("avg") or 0.0)


# ══════════════════════════════════════════════════════════
# 用例定义
# ══════════════════════════════════════════════════════════
# faults：要注入的故障 id 列表（按序注入，同时存在）
# dimension：压的是哪个复杂度维度
# question：问法（真实运维会这么问）
# judge：判定函数，返回 (通过?, 说明)

CASES = {
    # ── D3 强干扰：三个互不相关的故障并发 ──
    # 真实性：生产环境故障很少单独出现；变更窗口内多个服务同时出问题是常态。
    # 基线的风险：把三个根因串成一条因果链（"Redis 雪崩导致慢查询导致 OOM"），
    # 这在运维上是灾难性的错误 —— 会按错误的顺序治理，治了半天不见好。
    "multi_fault": {
        "dimension": "D3 强干扰（多故障并发）",
        "faults": ["redis_cache_avalanche", "slow_query_storm", "pod_oom_crash"],
        "question": "集群现在同时出了多个问题。请分别定位它们各自的根因，"
                    "明确说清有几个**互相独立**的故障、每个故障的根因组件是什么，"
                    "并给出治理的先后顺序。",
        "expect_instances": ["kvstore-redis-01", "rds-mysql-order"],
        "expect_independent": 3,
    },

    # ── D2 大数据量：全量日志聚合 ──
    # 真实性：SRE 日常第一步就是"这半小时错误都长什么样"。
    # 基线的风险：ingress_logs 数千条，一次取回就撑爆上下文；没有落盘分页与
    # sql_query 聚合意识时，模型倾向于反复捞原始日志然后凭印象汇总 → 数字对不上。
    "log_aggregate": {
        "dimension": "D2 大数据量（全量日志聚合）",
        "faults": ["redis_cache_avalanche"],
        "question": "统计最近 30 分钟所有 5xx 错误：总条数是多少？"
                    "按接口（URL）分组，错误最多的前几个接口各多少条？"
                    "请给出准确数字，并说明这些错误是否同源。",
        "numeric_truth": "error_groups",
    },

    # ── D1 长任务：全链路体检 + 逐项取证 ──
    # 真实性：接手一个陌生集群时的第一件事。
    # 基线的风险：无任务清单 → 步数耗尽即中断，交付一份半成品；
    # 而 full 臂能靠 plan 判定"还没做完"从而自动开新一段。
    "long_horizon": {
        "dimension": "D1 长任务（多阶段体检）",
        "faults": ["slow_query_storm"],
        "question": "做一次完整体检并逐项取证："
                    "①梳理服务拓扑并指出异常边；②扫描全部风险并按 P1/P2 分级；"
                    "③对每一条 P1 风险，分别用真实数据说明它的当前水位与阈值差距；"
                    "④判断这些风险里哪些与当前正在发生的故障有关、哪些是长期存在的配置缺陷；"
                    "⑤给出治理顺序与理由。每一步都要有工具返回的数值支撑。",
        "expect_sections": 5,
    },

    # ── D4 易幻觉：要求大量精确数值 ──
    # 真实性：写故障报告/复盘时必须给准确数字。
    # 基线的风险：无核对 → 编造 traceID、把 76.3% 写成 78%、引用不存在的实例名。
    "precise_numbers": {
        "dimension": "D4 易幻觉（大量精确数值）",
        "faults": ["redis_cache_avalanche"],
        "question": "写一份故障简报，必须包含："
                    "①kvstore-redis-01 当前的 CPU 使用率、内存使用率（精确到小数点后一位）；"
                    "②两台 RDS 各自的连接使用率与内存使用率；"
                    "③最近 30 分钟 5xx 总条数；"
                    "④至少 2 个真实的慢 trace_id 作为证据。",
        "numeric_truth": "redis_and_rds",
    },
}


# ══════════════════════════════════════════════════════════
# 判定
# ══════════════════════════════════════════════════════════

# 实体别名：判定"是否识别出这个根因"时，简称必须与全名同等对待。
#
# 【为什么必须有别名】实测踩过一次假阴性：full 臂明确写了
#   "Redis（内存 87%、CPU 80% 尖峰）…只构成风险（CACHE-001/002）"，
# 与通过的那一臂对 Redis 的事实认定**逐项一致**（都发现水位飙升、都指出调用侧
# 零错误、都说与其他故障无因果关联），差别仅在分类标签（"风险项" vs "故障三"）
# 和用词（简称 "Redis" vs 全名 "kvstore-redis-01"）。而判定只匹配全名字符串，
# 于是把一份等价正确的回答判成漏报。
#
# 这是同类问题的**第四次**（串报判定 2 次、长任务截断 1 次）。共性是：
# 拿字符串匹配去近似语义判断，必然在措辞差异上翻车。别名表是成本最低的缓解，
# 但仍要求"在异常语境中出现"（见下方 _entity_hit），否则顺带提一句
# "Redis 一切正常"也会被算成命中。
_ENTITY_ALIASES = {
    "kvstore-redis-01": ("kvstore-redis-01", "redis"),
    "rds-mysql-order": ("rds-mysql-order", "order 库", "orders 表", "order库"),
    "rds-mysql-core": ("rds-mysql-core", "core 库", "core库"),
}
# 异常语境词：实体名附近须有其中之一，才算"识别出它有问题"
_ANOMALY_HINTS = ("%", "异常", "风险", "打满", "飙升", "激增", "爬升", "尖峰",
                  "过高", "超阈", "慢查询", "OOM", "雪崩", "水位", "告警",
                  "CACHE-", "DB-", "CAP-", "根因", "故障")


def _entity_hit(answer: str, entity: str) -> bool:
    """回答是否识别出该实体存在问题（支持简称，但要求异常语境）。

    只要实体名（或其别名）出现，且其前后 200 字符内有异常语境词，就算命中。
    这样"Redis 内存 87%、只构成风险"算命中，而"Redis 一切正常"不算。
    """
    low = answer.lower()
    for alias in _ENTITY_ALIASES.get(entity, (entity,)):
        a = alias.lower()
        start = 0
        while True:
            i = low.find(a, start)
            if i < 0:
                break
            window = answer[max(0, i - 200): i + len(alias) + 200]
            if any(h.lower() in window.lower() for h in _ANOMALY_HINTS):
                return True
            start = i + len(a)
    return False


def _judge_multi_fault(answer: str, case: dict) -> tuple:
    """多故障：既要认出各自根因，又不能串成一条因果链。"""
    hits = [i for i in case["expect_instances"] if _entity_hit(answer, i)]
    # 串报判据：把独立故障写成互为因果。取自真实误答的措辞。
    chain_words = ("由此引发", "从而导致", "级联导致", "根源是同一",
                   "同一根因", "均由", "都是由", "连锁反应导致")
    chained = [w for w in chain_words if w in answer]
    # 独立性判据：明确说了"独立/互不相关/分别是"这类词
    indep = any(w in answer for w in ("独立", "互不相关", "彼此无关",
                                     "不相关", "分别是", "三个问题", "三类问题"))
    ok = len(hits) >= len(case["expect_instances"]) and indep and not chained
    note = (f"命中根因 {len(hits)}/{len(case['expect_instances'])}"
            f"{hits}、独立性表述={'有' if indep else '无'}"
            f"、串成因果链={chained or '无'}")
    return ok, note


def truth_snapshot(kind: str) -> dict:
    """给数值类用例拍一份真值快照。

    【为何要拍两次（排障前 + 排障后）】live 世界在持续演变，而"最近 30 分钟
    5xx 条数"是**滑动窗口**指标：Agent 查询的时刻与判定的时刻差几分钟，
    正确答案本身就不同。只用单一时刻的真值去卡，会把正确回答判成错的 ——
    那种"失败"是评测造的。所以判定用 [前, 后] 区间。
    """
    if kind == "error_groups":
        snap = {"5xx 总条数": float(_truth_total_5xx(30))}
        for url, n in list(_truth_error_groups(30).items())[:4]:
            snap[f"{url} 错误数"] = float(n)
        return snap
    return {"redis CPU": _truth_instance_metric("redis", "CpuUsage"),
            "redis 内存": _truth_instance_metric("redis", "MemoryUsage"),
            "5xx 总条数": float(_truth_total_5xx(30))}


def _judge_numbers(answer: str, case: dict, before: dict = None,
                   after: dict = None) -> tuple:
    """数值类：回答里的数字必须落在 [排障前真值, 排障后真值] 区间内。

    容差：区间两端各再放 ±5%（或 ±1 绝对量，取宽者）。理由是单位换算与
    四舍五入本来就该允许；但差一个数量级、或凭空编造一个库里根本没有的数，
    就必须判错 —— 那正是要测的东西。
    """
    before = before or {}
    after = after or before
    keys = list(before) or list(after)
    if not keys:
        return False, "真值快照为空，无法判定"
    # ★ 护栏：数值类用例的意义在于"核对很多个数"。若快照退化到只剩 1 个键，
    #   多半是真值查询本身出了问题（如时间戳单位不匹配），此时任何"通过"
    #   都是蒙对的。宁可判失败并喊出来，也不要产出一个假通过。
    if len(keys) < 2:
        return False, (f"真值快照只有 {len(keys)} 个键（{keys}），疑似真值查询失效 —— "
                       f"本项不作为通过计")

    import re
    nums = [float(x) for x in re.findall(r"\d+\.?\d*", answer)]
    bad = []
    for k in keys:
        v0, v1 = before.get(k, after.get(k, 0.0)), after.get(k, before.get(k, 0.0))
        lo, hi = min(v0, v1), max(v0, v1)
        tol = max(1.0, hi * 0.05)
        if not any(lo - tol <= v <= hi + tol for v in nums):
            bad.append(f"{k}(真值{lo:.1f}~{hi:.1f})")
    return not bad, (f"{len(keys) - len(bad)}/{len(keys)} 个数值可核对"
                     + (f"、对不上: {bad}" if bad else ""))


# 系统在步数耗尽/被中断时生成的**固定**措辞（loop._partial_conclusion）。
# 判「有没有做完」只能认这个，不能去猜模型的自然语言 —— 见下方 docstring。
_TRUNCATION_MARKERS = ("执行被中断", "不是完整结论")


def _judge_long_horizon(answer: str, case: dict, incomplete: bool = False) -> tuple:
    """长任务：交付是否完整（五个小问都答了），而不是半成品。

    【截断判据为何只认系统措辞】第一版拿模型的自然语言猜截断，词表里有
    "尚未"/"还需继续"，结果把一次**完全正确**的交付判成了失败：full 臂覆盖了
    5/5 个小问、incomplete=False，只因结尾写了

        "以上治理动作**尚未执行**。是否确认？"

    而这句恰恰是它**遵守权限门禁**的表现 —— 治理动作执行前必须征得用户确认。
    判定把"守规矩"读成了"没做完"，于是得出「Harness 下降 50 个百分点」的错结论。

    正确信号有且只有两个，都由系统产生、与模型措辞无关：
      · incomplete —— 来自 aborted 事件或空回答；
      · _TRUNCATION_MARKERS —— loop._partial_conclusion 写死的中断前缀。
    """
    marks = ("拓扑", "风险", "阈值", "配置缺陷", "顺序")
    hit = [m for m in marks if m in answer]
    truncated = incomplete or all(m in answer for m in _TRUNCATION_MARKERS)
    ok = len(hit) >= 4 and not truncated
    return ok, (f"覆盖 {len(hit)}/{len(marks)} 个小问{hit}"
                + ("、⚠️ 交付被截断（系统中断标记）" if truncated else ""))


JUDGES = {
    "multi_fault": _judge_multi_fault,
    "log_aggregate": _judge_numbers,
    "long_horizon": _judge_long_horizon,
    "precise_numbers": _judge_numbers,
}


# ══════════════════════════════════════════════════════════
# 执行
# ══════════════════════════════════════════════════════════

def _inject(fid: str) -> dict:
    r = httpx.post(f"{MOCK}/control/inject_fault", json={"scenario_id": fid}, timeout=20)
    r.raise_for_status()          # 注入失败必须立刻炸，否则会在"没有故障的世界"里评测
    return r.json()


def _clear_faults() -> int:
    """恢复全部残留故障。

    活跃故障从 /control/world_status 读 —— 没有 /control/active_faults 这个端点。
    我第一版凭直觉造了那个 URL，httpx 拿回 404 的 {"detail": "Not Found"}，
    遍历它得到的是字符串键，于是报 "string indices must be integers"，
    两个用例全废。**同一个坑上一轮就踩过**（当时是 /control/apply_action），
    根因都是一样的：没检查状态码就当成数据用。所以这里显式 raise_for_status。
    """
    try:
        r = httpx.get(f"{MOCK}/control/world_status", timeout=10)
        r.raise_for_status()
        active = r.json().get("active_faults", [])
    except Exception as e:                                       # noqa: BLE001
        print(f"  ⚠️ 无法读取世界状态以清理残留故障：{e}")
        return 0
    for f in active:
        fid = f.get("fault_id")
        if not fid:
            continue
        try:
            httpx.post(f"{MOCK}/control/recover_fault",
                       json={"fault_id": fid}, timeout=20).raise_for_status()
            print(f"  已恢复残留故障 {fid}（{f.get('scenario_id')}）")
        except Exception as e:                                   # noqa: BLE001
            print(f"  ⚠️ 恢复 {fid} 失败：{e} —— 后续用例可能受残留影响")
    return len(active)


def _ask(cid: str, question: str, profile: str) -> dict:
    """跑一次。session 带 profile：两臂绝不能共用历史，否则等于喂答案。"""
    orig = config.HARNESS_PROFILE
    config.HARNESS_PROFILE = profile
    tag = f"eval-l3-{cid}-{profile}"
    run = RunContext(session_id=tag, mode="readonly")
    t0 = time.perf_counter()
    answer, tools, warn, aborted, conts = "", 0, None, False, 0
    refreshed, answer_before_refresh, tools_before_refresh = False, "", 0
    try:
        for ev in scheduler.handle_message(tag, question, run=run):
            et = ev.get("type")
            if et == "tool_call":
                tools += 1
                print(f"       [{time.perf_counter() - t0:5.0f}s] "
                      f"{'  ' * (ev.get('depth') or 0)}#{tools} {ev.get('tool')}",
                      flush=True)
            elif et == "answer":
                answer = ev.get("text") or answer
            elif et == "verify_warning":
                warn = ev
            elif et == "verify_refresh":
                # ★ 必须记下复核发生的时刻与当时的初始回答：
                #   复核会让模型**重写整份回答**，若重写时丢掉了原本已找到的根因，
                #   最终判定会变差，而从日志上完全看不出是复核造成的。
                #   实测遇到过一次 full 臂漏根因、却无法回溯是否复核所致 ——
                #   缺这两个字段就只能靠猜。
                refreshed = True
                answer_before_refresh = answer
                tools_before_refresh = tools
                print(f"       [{time.perf_counter() - t0:5.0f}s] "
                      f"★ 数据时效复核触发（此前 {tools} 次工具）", flush=True)
            elif et in ("aborted", "continuation", "compacted", "model_fallback"):
                if et == "aborted":
                    aborted = True
                elif et == "continuation":
                    conts += 1
                print(f"       [{time.perf_counter() - t0:5.0f}s] ⚠️ {et}", flush=True)
    finally:
        config.HARNESS_PROFILE = orig
    return {"profile": profile, "answer": answer,
            "seconds": round(time.perf_counter() - t0, 1), "tool_calls": tools,
            "tokens": run.tokens_in + run.tokens_out,
            "cost_cny": run.usage_event()["est_cost_cny"],
            "verify_warning": warn, "verify_probe": run.verify_probe,
            # 复核可观测性：是否触发、触发前的回答与工具数 ——
            # 用于判断"复核重写是否丢失了原有结论"
            "refreshed": refreshed, "tools_before_refresh": tools_before_refresh,
            "answer_before_refresh": answer_before_refresh,
            "incomplete": bool(aborted or not (answer or "").strip()),
            "continuations": conts}


def run_case(cid: str, case: dict, wait_s: int, profiles: list, repeat: int) -> dict:
    print(f"\n{'─' * 74}\n▶ {cid} — {case['dimension']}")
    _clear_faults()
    time.sleep(8)
    for f in case["faults"]:
        inj = _inject(f)
        print(f"  注入 {f} → {inj.get('fault_id')}")
    print(f"  等待 {wait_s}s 让数据进入窗口…")
    time.sleep(wait_s)

    runs = []
    for profile, rep in itertools.product(profiles, range(repeat)):
        tag = f"（第 {rep + 1}/{repeat} 次）" if repeat > 1 else ""
        print(f"  → 排障（档位={profile}）{tag}…")
        kind = case.get("numeric_truth")
        before = truth_snapshot(kind) if kind else None
        a = _ask(cid, case["question"], profile)
        after = truth_snapshot(kind) if kind else None
        a["truth_before"], a["truth_after"] = before, after
        judge = JUDGES[cid]
        if kind:
            ok, note = judge(a["answer"], case, before, after)
        elif cid == "long_horizon":
            # 截断判据要用系统给的 incomplete，而不是猜模型措辞
            ok, note = judge(a["answer"], case, a["incomplete"])
        else:
            ok, note = judge(a["answer"], case)
        a["passed"], a["note"] = ok, note
        print(f"     [{profile:5s}] {'✅通过' if ok else '❌未过'}  {note}")
        print(f"     {a['seconds']}s {a['tool_calls']}工具 ¥{a['cost_cny']:.4f}"
              f"{' ⚠️未完成' if a['incomplete'] else ''}"
              f"{' 续跑' + str(a['continuations']) + '段' if a['continuations'] else ''}")
        print(f"     摘要: {(a['answer'] or '（空）')[:150]}")
        runs.append(a)
    _clear_faults()
    return {"case": cid, "dimension": case["dimension"], "runs": runs}


def summarize(rows: list):
    print(f"\n{'═' * 74}\n★ 复杂度分维度对比（完整 Harness vs 朴素基线）\n{'═' * 74}")
    by_prof: dict = {}
    print(f"\n{'维度':34s} {'full':>12s} {'naive':>12s}")
    for r in rows:
        cells = {}
        for prof in ("full", "naive"):
            rs = [a for a in r["runs"] if a["profile"] == prof]
            if not rs:
                cells[prof] = "—"
                continue
            ok = sum(1 for a in rs if a["passed"])
            cells[prof] = f"{ok}/{len(rs)}"
            st = by_prof.setdefault(prof, {"ok": 0, "n": 0, "sec": [], "cost": [],
                                           "inc": 0, "halluc": 0, "stale": 0,
                                           "stale_told": 0, "tools": []})
            st["ok"] += ok
            st["n"] += len(rs)
            st["sec"] += [a["seconds"] for a in rs]
            st["cost"] += [a["cost_cny"] for a in rs]
            st["tools"] += [a["tool_calls"] for a in rs]
            st["inc"] += sum(1 for a in rs if a["incomplete"])
            # A 类（编造标识符/资源名）与 C 类（数据过期）必须分开统计：
            # 前者是"回答写错了"，后者是"回答没错但数据底座已过期"。
            # 合并会同时误导两个方向 —— 把时效告警算成幻觉会虚增己方幻觉率，
            # 而基线那边探针只把 A/B 类记为 suspicious，时效数会被整个漏掉。
            for a in rs:
                w, pb = a["verify_warning"], (a.get("verify_probe") or {})
                st["halluc"] += 1 if ((w and w.get("fake_ids"))
                                      or pb.get("fake_ids")) else 0
                st["stale"] += (len((w or {}).get("stale_sources") or [])
                                + (pb.get("stale_sources") or 0))
                # full 臂会把时效问题告警给用户；naive 臂只被探针记录，用户看不到
                st["stale_told"] += len((w or {}).get("stale_sources") or [])
        print(f"{r['dimension']:34s} {cells.get('full', '—'):>12s} "
              f"{cells.get('naive', '—'):>12s}")

    if len(by_prof) < 2:
        return
    print(f"\n{'─' * 74}\n合计")
    for prof, st in by_prof.items():
        tag = "完整 Harness" if prof == "full" else "朴素基线  "
        print(f"  {tag} 通过 {st['ok']}/{st['n']} = {st['ok'] / st['n'] * 100:.0f}%"
              f"、耗时中位 {statistics.median(st['sec']):.0f}s"
              f"、工具中位 {statistics.median(st['tools']):.0f} 次"
              f"、成本中位 ¥{statistics.median(st['cost']):.4f}、未完成 {st['inc']}")
        print(f"    A 类幻觉（编造标识符/资源名）: {st['halluc']}"
              f"　│　C 类数据时效问题: {st['stale']} 处，"
              f"其中**告知用户** {st['stale_told']} 处"
              + ("（基线无告警机制，全部静默交付）" if prof == "naive" else ""))
    f, nv = by_prof.get("full"), by_prof.get("naive")
    if f and nv:
        dp = (f["ok"] / f["n"] - nv["ok"] / nv["n"]) * 100
        print(f"\n  → 复杂场景上{'提升' if dp >= 0 else '下降'} {abs(dp):.0f} 个百分点"
              f"（{nv['ok'] / nv['n'] * 100:.0f}% → {f['ok'] / f['n'] * 100:.0f}%）")
        print(f"  → 效率：工具调用中位 基线 {statistics.median(nv['tools']):.0f} 次 → "
              f"完整 {statistics.median(f['tools']):.0f} 次；"
              f"成本中位 ¥{statistics.median(nv['cost']):.2f} → ¥{statistics.median(f['cost']):.2f}")
        print(f"  → 可信度：A 类幻觉 基线 {nv['halluc']} vs 完整 {f['halluc']}；"
              f"C 类时效问题 基线 {nv['stale']} 处（告知 {nv['stale_told']} 处）"
              f" vs 完整 {f['stale']} 处（告知 {f['stale_told']} 处）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只跑指定用例（逗号分隔）")
    ap.add_argument("--wait", type=int, default=310,
                    help="注入后等待秒数（须 ≥ 规则窗口，否则窗口内混入故障前低值）")
    ap.add_argument("--compare-baseline", action="store_true",
                    help="完整 Harness vs 朴素基线两臂对比")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--json", help="结果写入 JSON")
    args = ap.parse_args()

    try:
        httpx.get(f"{MOCK}/health", timeout=5).raise_for_status()
    except Exception as e:                                       # noqa: BLE001
        raise SystemExit(f"❌ mock_server 不可达（{MOCK}）：{e}")

    db.init_db()
    registry.ensure_loaded()

    wanted = list(CASES)
    if args.only:
        wanted = [c.strip() for c in args.only.split(",") if c.strip()]
        for c in wanted:
            if c not in CASES:
                raise SystemExit(f"❌ 未知用例 {c}；可选：{', '.join(CASES)}")

    profiles = ["full", "naive"] if args.compare_baseline else ["full"]
    print(f"数据源   : {config.DATA_SOURCE}")
    print(f"规则窗口 : {config.RULE_WINDOW_MINUTES} 分钟")
    if args.wait < config.RULE_WINDOW_MINUTES * 60:
        print(f"  ⚠️ --wait {args.wait}s < 窗口 {config.RULE_WINDOW_MINUTES * 60}s："
              f"窗口内会混入故障前低值，漏报会是评测造的")
    print(f"用例     : {len(wanted)} 个 × {len(profiles)} 臂 × {args.repeat} 次")

    rows = []
    quota_dead = False
    for i, cid in enumerate(wanted, 1):
        print(f"\n[{i}/{len(wanted)}]", end="")
        try:
            rows.append(run_case(cid, CASES[cid], args.wait, profiles, args.repeat))
        except QuotaExhausted as e:
            # 立刻停：继续跑只会让每个用例都返回空回答、被判 0 分，
            # 最后汇总出"提升 0 个百分点"这种看起来像结论的假数据。
            print(f"\n\n❌ LLM 配额耗尽，评测中止：{e}")
            quota_dead = True
            break
        except Exception as e:                                   # noqa: BLE001
            print(f"\n  ❌ 用例 {cid} 异常：{e}")
            rows.append({"case": cid, "dimension": CASES[cid]["dimension"],
                         "runs": [], "error": str(e)})
    if quota_dead:
        _clear_faults()
        print(f"\n⚠️ 本轮数据【无效】，不可用于任何结论："
              f"已完成 {len(rows)}/{len(wanted)} 个用例，且最后一个可能只跑了一半。")
        print(f"   配额恢复后重跑即可（故障已清理，世界已回到稳态）。")
        return
    summarize(rows)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1,
                                              default=str), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")


if __name__ == "__main__":
    main()
