"""L3 复杂度压力评测的判定逻辑回归测试（eval/run_l3.py）。

## 为什么判定逻辑必须先被测

L3 要产出的结论是「复杂场景上 Harness 提升 X 个百分点」。这个数字完全由判定
函数决定，而启发式判定**两个方向都会错**：

  · 判太松 → 基线的错答被放过 → 提升被低估，甚至得出"Harness 没用"；
  · 判太严 → 完整臂的正确回答被误杀 → 提升被虚高，等于自己给自己注水。

L2 的串报判定就连错过两次（都是把正确回答判成错的），而它当时**一条常驻测试
都没有**。所以这里每加一条判定规则，都要同时有正反两侧用例守着。

## 用例取材

正样本尽量接近真机回答的措辞（多故障那条的"三个独立故障"、数值那条的
小数点写法）；负样本取"最容易被放过的错法" —— 把独立故障串成因果链、
把数字编成一个库里没有的值。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_eval_l3.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "eval_l3_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["DB_URL"] = f"sqlite:///{_TMP_DB}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config                                            # noqa: E402

if not config.DATABASE_URL.startswith("sqlite"):
    raise SystemExit(
        f"❌ 测试中止：数据库未隔离，当前指向 "
        f"{config.DATABASE_URL.split('@')[-1]}。测试必须连本地 SQLite。")

from eval import run_l3                                           # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── D3 多故障：既要认出各自根因，又不能串成因果链 ──

_MULTI_CASE = run_l3.CASES["multi_fault"]


def test_multi_fault_accepts_correct_independent_diagnosis():
    """正样本：三个独立故障各自定位清楚 → 必须通过。"""
    answer = (
        "集群当前存在 **3 个互相独立**的故障：\n"
        "1. 缓存实例 kvstore-redis-01 内存 86%、CPU 78%，缓存雪崩；\n"
        "2. rds-mysql-order 出现慢查询风暴，30 条慢日志；\n"
        "3. inventory-service 的 Pod 因内存超限被 OOMKilled 重启。\n"
        "三者根因不同，分别是缓存层、数据库层、应用内存配置，"
        "建议治理顺序：先缓存、再慢查询、最后 OOM。")
    ok, note = run_l3._judge_multi_fault(answer, _MULTI_CASE)
    _assert(ok, f"正确的多故障诊断被判错：{note}")


def test_entity_hit_accepts_short_alias_in_anomaly_context():
    """★★ 简称必须与全名同等对待 —— 守一次真实的假阴性。

    真机原文（full 臂）：它明确写了 Redis 的异常水位并引用了 CACHE 规则，
    与通过的那一臂对 Redis 的事实认定**逐项一致**（都发现水位飙升、都指出
    调用侧零错误），差别仅在分类标签（"风险项" vs "故障三"）与用词
    （简称 "Redis" vs 全名 "kvstore-redis-01"）。
    而原判定只匹配全名字符串，把这份等价正确的回答判成漏报，
    进而得出「完整 Harness 下降 25 个百分点」的错结论。
    """
    real = ("打满 core 连接；payment 的 DB 调用仅 5.1ms，其失败源于自身 OOM。"
            "Redis（内存 **87%**、CPU **80%** 尖峰）所有调用均成功、延迟 ~1.1ms，"
            "**只构成风险（CACHE-001/002），不是独立故障**。")
    assert run_l3._entity_hit(real, "kvstore-redis-01"), \
        "简称 Redis + 异常水位未被认作命中（真实假阴性）"
    print("  ✓ 简称 Redis 在异常语境中被正确认作命中")


def test_entity_hit_rejects_healthy_mention():
    """★ 反方向：顺带提一句「一切正常」不能算命中。

    别名放宽后最容易滑向"只要提到就算命中"，那样任何回答都能蒙对。
    所以要求实体名附近必须有异常语境词。
    """
    healthy = "巡检结论：集群整体健康。Redis 调用正常，无需处理。"
    assert not run_l3._entity_hit(healthy, "kvstore-redis-01"), \
        "顺带提及健康状态却算成命中 —— 判定被放宽成了永远及格"
    print("  ✓ 健康语境下的顺带提及不算命中")


def test_entity_hit_full_name_still_works():
    """全名当然要命中 —— 别名机制不能把原有能力弄丢。"""
    txt = "根因：kvstore-redis-01 内存 86% 触发淘汰，缓存雪崩。"
    assert run_l3._entity_hit(txt, "kvstore-redis-01")
    txt2 = "慢查询全部来自 rds-mysql-order，扫描 130 万行。"
    assert run_l3._entity_hit(txt2, "rds-mysql-order")
    print("  ✓ 全名匹配未被别名机制破坏")


def test_multi_fault_rejects_causal_chaining():
    """★ 负样本：把独立故障串成一条因果链 → 必须判错。

    这是运维上最危险的错法：会导致按错误顺序治理，治了半天不见好。
    而它表面上"逻辑通顺"，最容易被宽松判定放过。
    """
    answer = (
        "根因是 kvstore-redis-01 缓存雪崩，缓存失效后请求全部打到数据库，"
        "**从而导致** rds-mysql-order 出现慢查询风暴，"
        "数据库变慢又**级联导致** inventory-service 线程堆积 OOM。"
        "三个现象同一根因，治好 Redis 即可。")
    ok, note = run_l3._judge_multi_fault(answer, _MULTI_CASE)
    _assert(not ok, f"把独立故障串成因果链却判通过了：{note}")


def test_multi_fault_rejects_missing_root_cause():
    """负样本：只找到一个根因就收工 → 必须判错。"""
    answer = ("根因是 kvstore-redis-01 缓存雪崩，导致多个接口变慢。"
              "建议升配 Redis 实例。")
    ok, note = run_l3._judge_multi_fault(answer, _MULTI_CASE)
    _assert(not ok, f"漏掉根因却判通过了：{note}")


def test_multi_fault_rejects_independent_without_naming():
    """负样本：说了"独立"但没点出具体实例 → 不算定位到。"""
    answer = ("集群有三个互不相关的独立问题：缓存有压力、数据库有慢查询、"
              "有个 Pod 在重启。建议逐一排查。")
    ok, note = run_l3._judge_multi_fault(answer, _MULTI_CASE)
    _assert(not ok, f"空泛表述却判通过了：{note}")


# ── D2/D4 数值类：区间判定 ──

_NUM_CASE = run_l3.CASES["log_aggregate"]


def test_numbers_accept_value_inside_window():
    """正样本：数字落在 [排障前, 排障后] 区间内 → 通过。

    这一项守的是"世界在演变"这个现实：滑动窗口指标在排障期间本来就会变，
    只用单一时刻真值去卡会把正确回答判成错的。
    """
    # 两个键：一个考区间容忍，一个考静态值。单键会撞上"退化快照"护栏，
    # 而那条护栏是刻意加的（见 test_numbers_reject_degenerate_snapshot）。
    before = {"5xx 总条数": 100.0, "/api/pay 错误数": 12.0}
    after = {"5xx 总条数": 130.0, "/api/pay 错误数": 12.0}
    ok, note = run_l3._judge_numbers(
        "最近 30 分钟共 118 条 5xx，其中 /api/pay 12 条。",
        _NUM_CASE, before, after)
    _assert(ok, f"区间内的数字被判错：{note}")


def test_numbers_accept_rounding():
    """正样本：四舍五入/小数位差异要容忍（±5% 或 ±1，取宽者）。"""
    before = after = {"redis CPU": 76.36, "redis 内存": 85.32}
    ok, note = run_l3._judge_numbers(
        "Redis CPU 使用率 76.4%、内存 85.3%。", _NUM_CASE, before, after)
    _assert(ok, f"四舍五入被判错：{note}")


def test_numbers_reject_fabricated_value():
    """★ 负样本：编造一个库里根本没有的数 → 必须判错。"""
    before = after = {"5xx 总条数": 109.0}
    ok, note = run_l3._judge_numbers("最近 30 分钟共 4200 条 5xx 错误。",
                                     _NUM_CASE, before, after)
    _assert(not ok, f"编造的数值被放过了：{note}")


def test_numbers_reject_order_of_magnitude_error():
    """负样本：差一个数量级 → 必须判错（这类错在运维里会直接误导决策）。"""
    before = after = {"redis 内存": 85.3}
    ok, note = run_l3._judge_numbers("Redis 内存使用率 8.5%，水位健康。",
                                     _NUM_CASE, before, after)
    _assert(not ok, f"差一个数量级却判通过了：{note}")


def test_numbers_reject_degenerate_snapshot():
    """★★ 真值快照退化到只剩 1 个键时必须判失败，绝不能蒙对。

    这条守的是一个**假通过**：D2 的真值查询原本按毫秒过滤 ingress_logs，
    而那张表的 ts 是**秒**级（同库里 metrics 是毫秒、5 张日志表是秒，
    单位并不统一）。条件恒为假 → 查询恒返回 0 → 快照只剩
    「5xx 总条数 = 0」一个键。而容差是 ±1，回答里任何接近 0 的数字都能对上，
    于是两臂都被判「✅通过 1/1 个数值可核对」——数据完全无效却看不出来。

    数值类用例的意义就在于核对**多个**数。只剩一个键时，正确做法是
    判失败并喊出可疑，而不是产出一个漂亮的假结果。
    """
    before = after = {"5xx 总条数": 0.0}
    ok, note = run_l3._judge_numbers("最近 30 分钟共 0 条 5xx，一切正常。",
                                     _NUM_CASE, before, after)
    _assert(not ok, f"退化快照下蒙对却判通过了：{note}")
    _assert("疑似真值查询失效" in note, f"没有指出可疑原因：{note}")


def test_log_window_uses_seconds_not_millis():
    """日志类表的窗口起点必须是**秒**级 —— 与 metrics（毫秒）不同。

    直接断言量级，避免以后有人"顺手统一成毫秒"而静默废掉整个 D2 维度。
    """
    import time
    w = run_l3._log_window_start(30)
    now = int(time.time())
    _assert(abs(w - (now - 1800)) <= 5,
            f"窗口起点不是秒级：{w}（当前秒 {now}）")
    _assert(w < now * 10, f"窗口起点疑似用了毫秒：{w}")


def test_numbers_reject_empty_snapshot():
    """真值快照为空时必须判错，绝不能因为"没东西可比"而默认通过。"""
    ok, note = run_l3._judge_numbers("一切正常。", _NUM_CASE, {}, {})
    _assert(not ok, f"空真值却判通过了：{note}")


# ── D1 长任务：交付完整性 ──

_LONG_CASE = run_l3.CASES["long_horizon"]


def test_long_horizon_accepts_complete_delivery():
    """正样本：五个小问都覆盖且未被截断 → 通过。"""
    answer = ("## 拓扑\n异常边：api-gateway → order-service。\n"
              "## 风险\nP1 共 5 条、P2 共 3 条。\n"
              "## 阈值差距\nDB-001 当前 92% vs 阈值 80%。\n"
              "## 配置缺陷\nHA-002 单副本属长期配置缺陷，与本次故障无关。\n"
              "## 治理顺序\n先处理慢查询，CAP-003 超卖率放最后。")
    ok, note = run_l3._judge_long_horizon(answer, _LONG_CASE)
    _assert(ok, f"完整交付被判错：{note}")


def test_long_horizon_rejects_truncated_delivery():
    """★ 负样本：步数耗尽交半成品 → 必须判错。

    这正是基线臂（无 update_plan/无续跑）最可能的失败形态。
    若把它判成通过，D1 这个维度就完全测不出东西。
    判据用系统写死的中断前缀，而不是模型的自然语言。
    """
    answer = ("## 拓扑\n已梳理完成，异常边 api-gateway → order-service。\n"
              "## 风险\n已扫描出 P1 共 5 条。\n"
              "> ⚠️ 执行被中断（步数用尽），以下是中断前已获得的信息，**不是完整结论**")
    ok, note = run_l3._judge_long_horizon(answer, _LONG_CASE)
    _assert(not ok, f"被截断的半成品判成通过了：{note}")


def test_long_horizon_accepts_pending_governance_wording():
    """★★ 反方向：完整交付里说「治理动作尚未执行，是否确认」必须判**通过**。

    这条守的是一个真实误判：第一版用模型措辞猜截断（词表含"尚未"/"还需继续"），
    把一次覆盖 5/5、incomplete=False 的**正确交付**判成失败 ——
    只因它结尾写了"以上治理动作尚未执行。是否确认？"。

    而那句恰恰是**遵守权限门禁**的表现：治理动作执行前必须征得用户确认。
    判定把"守规矩"读成"没做完"，直接导致「Harness 下降 50 个百分点」的错结论。
    取材于真机回答原文。
    """
    answer = ("## ① 拓扑\n15 条边，异常边 api-gateway → order-service。\n"
              "## ② 风险\nP1 共 5 条、P2 共 3 条。\n"
              "## ③ 阈值差距\nDB-001 当前 92% vs 阈值 80%。\n"
              "## ④ 配置缺陷\nHA-002 单副本属长期配置缺陷，与本次故障无关。\n"
              "## ⑤ 治理顺序\n先索引、后升配，CAP-003 放最后。\n"
              "以上治理动作**尚未执行**。是否确认？建议先执行第 1、2 项，我逐项操作。")
    ok, note = run_l3._judge_long_horizon(answer, _LONG_CASE, incomplete=False)
    _assert(ok, f"守权限规矩的完整交付被误杀了：{note}")


def test_long_horizon_rejects_when_incomplete_flag_set():
    """incomplete 标记（来自 aborted 事件）必须能独立判负 ——
    有时中断发生在没有任何文字产出时，回答里不会有中断前缀。"""
    answer = ("## 拓扑\n完成。## 风险\n完成。## 阈值\n完成。"
              "## 配置缺陷\n完成。## 顺序\n完成。")
    ok, note = run_l3._judge_long_horizon(answer, _LONG_CASE, incomplete=True)
    _assert(not ok, f"incomplete=True 却判通过：{note}")


def test_long_horizon_rejects_partial_coverage():
    """负样本：只答了两个小问 → 必须判错。"""
    answer = "## 拓扑\n梳理完成。\n## 风险\n扫描完成，共 8 条。"
    ok, note = run_l3._judge_long_horizon(answer, _LONG_CASE)
    _assert(not ok, f"覆盖不足却判通过了：{note}")


# ── 配置自检 ──

def test_every_case_has_judge():
    """每个用例都必须有判定函数，否则跑到一半才 KeyError。"""
    missing = [c for c in run_l3.CASES if c not in run_l3.JUDGES]
    _assert(not missing, f"这些用例没有判定函数：{missing}")


def test_every_case_declares_dimension_and_faults():
    """用例元信息完整：维度用于分层报告，故障用于注入。"""
    for cid, case in run_l3.CASES.items():
        _assert(case.get("dimension"), f"{cid} 缺 dimension")
        _assert(case.get("faults"), f"{cid} 缺 faults")
        _assert(case.get("question"), f"{cid} 缺 question")


def main() -> int:
    groups = [
        ("D3 多故障：认出各自根因 vs 不许串成因果链", [
            test_multi_fault_accepts_correct_independent_diagnosis,
            test_entity_hit_accepts_short_alias_in_anomaly_context,
            test_entity_hit_rejects_healthy_mention,
            test_entity_hit_full_name_still_works,
            test_multi_fault_rejects_causal_chaining,
            test_multi_fault_rejects_missing_root_cause,
            test_multi_fault_rejects_independent_without_naming,
        ]),
        ("D2/D4 数值：容忍演变与舍入 vs 不许编造", [
            test_numbers_accept_value_inside_window,
            test_numbers_accept_rounding,
            test_numbers_reject_fabricated_value,
            test_numbers_reject_order_of_magnitude_error,
            test_numbers_reject_empty_snapshot,
            test_numbers_reject_degenerate_snapshot,
            test_log_window_uses_seconds_not_millis,
        ]),
        ("D1 长任务：完整交付 vs 半成品", [
            test_long_horizon_accepts_complete_delivery,
            test_long_horizon_rejects_truncated_delivery,
            test_long_horizon_accepts_pending_governance_wording,
            test_long_horizon_rejects_when_incomplete_flag_set,
            test_long_horizon_rejects_partial_coverage,
        ]),
        ("用例配置自检", [
            test_every_case_has_judge,
            test_every_case_declares_dimension_and_faults,
        ]),
    ]
    passed = failed = 0
    for title, tests in groups:
        print(f"\n=== {title} ===")
        for fn in tests:
            try:
                fn()
                passed += 1
                print(f"  ✓ {fn.__name__}")
            except Exception as e:                # noqa: BLE001
                failed += 1
                print(f"  ✗ {fn.__name__}: {e}")
    print("\n" + "=" * 46)
    print(f"通过 {passed} / 失败 {failed}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
