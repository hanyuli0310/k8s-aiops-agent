"""评测判定逻辑自身的回归测试（eval/metrics.py）。

## 为什么评测代码也要测

这套判定决定了报告里每一个 ✅ / ❌。而它**已经错过两次**，两次都是同一个方向：
把**正确**的诊断判成串报。

  · 第一版：窗口 ±40 字符、排除词 11 个 → 把「order 库连接率 52%，处于正常范围」
    这种明确的对照说明切掉一半，判成串报。
  · 第二版：补到 27 个词，仍漏「时态性排除」→ 两种调度模式在
    core_db_conn_exhausted 上各写了一句很好的区分（"上一轮慢查询风暴平息"、
    "此前已平息，风险项已 resolved"），却被判成串报。
    我据此得出「串报是 Agent 推理层弱点」这个**错结论**并写进了评测报告与 commit。

**指标出错比被测系统出错更糟**：系统出错会被指标发现，指标出错会把优化引向
不存在的问题。而这套判定之前**一条常驻测试都没有** —— 这就是它连错两次的原因。

## 用例来源

真串报与正确排除两侧都取自**真机跑出来的回答原文**（或其最小变体），
不是我凭想象编的句式。放宽词表时特别容易把判定改成"永远及格"，
所以每加一个排除词，都要有对应的真串报用例守住反方向。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_eval_metrics.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "eval_metrics_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["DB_URL"] = f"sqlite:///{_TMP_DB}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import metrics                                          # noqa: E402

_OTHER = "rds-mysql-order"
_SC = {"must_not_flag_instance": _OTHER, "expected_instance": "rds-mysql-core"}

# ── 真串报：必须判为 cross=True ──
# 关键是第 2 条：它同时含排除词（"回落"）与根因断言（"仍是…根因之一"）。
# 补时态排除词时立刻把它误放过了，所以它是这组里最重要的一条。
_REAL_CROSS = [
    ("并列根因",
     "根因是 rds-mysql-core 与 rds-mysql-order 两个实例的连接数同时被打满，需同时升配。"),
    ("含排除词但断言仍是根因",
     "rds-mysql-order 连接率虽有回落，但它仍是本次故障的根因之一，需要一起治理。"),
    ("裸提并建议一起治",
     "病灶：rds-mysql-core、rds-mysql-order。建议对两者都执行 upgrade_rds_instance。"),
    ("共同导致",
     "两个库连接同时紧张，rds-mysql-order 与 core 库共同导致了本次 503。"),
]

# ── 正确排除：必须判为 cross=False ──
# 后两条是【真机原文】：model 与 intent 两种调度在 core_db_conn_exhausted 上
# 各自写出的区分说明。它们曾被误判为串报。
_CORRECT_EXCLUSION = [
    ("状态正常型",
     "当前病灶是 rds-mysql-core；rds-mysql-order 连接率 52%，处于正常范围。"),
    ("真机·model·附带观察",
     "⚠️ 附带观察：同一时刻 rds-mysql-order 连接率从 ~82% 回落到 ~55%"
     "（上一轮慢查询风暴平息），两个库呈“跷跷板”；order 库仍有 843 条未治理的"
     "全表扫描慢查询（orders 表无索引），随时可能复发。"),
    ("真机·intent·时态排除",
     "（此前 rds-mysql-order 的全表扫描故障已平息：其连接率已回落至 ~55%，"
     "订单/支付/库存接口 P99 均 <0.4s，相关风险项已 resolved。）"),
]


def test_real_cross_reports_are_caught():
    """★ 真串报必须被抓到 —— 守住"放宽词表"的反方向。"""
    missed = [name for name, text in _REAL_CROSS
              if not metrics.judge_root_cause(text, _SC).cross_flag]
    assert not missed, f"这些真串报被放过了（判定被放得过宽）：{missed}"
    print(f"  ✓ {len(_REAL_CROSS)} 条真串报全部判为 cross")


def test_correct_exclusions_not_flagged():
    """★★ 正确的排除性说明不能被判成串报（这个判定已因此错过两次）。"""
    wrong = [name for name, text in _CORRECT_EXCLUSION
             if metrics.judge_root_cause(text, _SC).cross_flag]
    assert not wrong, (
        f"这些**正确**的排除说明被判成串报：{wrong}\n"
        f"  后果不是分数低，而是把优化引向不存在的问题 —— 上一轮就因此"
        f"得出了「串报是 Agent 推理层弱点」的错结论。")
    print(f"  ✓ {len(_CORRECT_EXCLUSION)} 条正确排除（含 2 条真机原文）均未误判")


def test_override_words_do_not_swallow_valid_supplements():
    """★ 否决词不能把正常的补充说明当成根因断言。

    真机回答里有「order 库**仍有** 843 条未治理慢查询」「**两个库**呈跷跷板」——
    这些是有价值的补充信息，不是"仍是根因"。若把"仍有"/"两个库"/"同时"
    收进否决词，刚修好的误判会立刻回来。
    """
    for w in ("仍有", "两个库", "同时", "共同"):
        assert w not in metrics._CROSS_OVERRIDE, \
            f"否决词 {w!r} 过宽，会把正常的补充说明判成根因断言"
    # 反向确认：这两个短语单独出现时不触发串报
    for text in (f"{_OTHER} 仍有 843 条未治理的慢查询，已平息但需后续跟进。",
                 f"两个库呈跷跷板：{_OTHER} 已回落，core 库上升。"):
        assert not metrics.judge_root_cause(text, _SC).cross_flag, text
    print("  ✓ 否决词足够窄，未吞掉「仍有 / 两个库」这类正常补充")


def test_no_mention_means_no_cross():
    """完全没提另一个实例时，不该判串报（也不该报 None）。"""
    v = metrics.judge_root_cause("根因是 rds-mysql-core 连接数打满。", _SC)
    assert v.cross_flag is False, v.cross_flag
    # 场景没标 must_not_flag_instance 时，这一项应为 None（不适用）
    v2 = metrics.judge_root_cause("随便什么结论", {"expected_instance": "x"})
    assert v2.cross_flag is None, "未标注对照实例的场景不该产出串报判定"
    print("  ✓ 未提及=不串报；未标注对照实例=该项不适用（None）")


def test_strict_ok_requires_no_cross():
    """★ strict_ok 必须受串报否决 —— 否则串报了也算通过。"""
    v = metrics.judge_root_cause(
        f"根因是 rds-mysql-core 与 {_OTHER} 共同导致的。",
        dict(_SC, _expect_keywords_any=["连接"]))
    assert v.cross_flag is True
    assert v.strict_ok is False, "串报时 strict_ok 必须为 False"
    print("  ✓ 串报会否决 strict_ok")


def test_exclusion_window_covers_full_sentence():
    """排除窗口要够宽：中文一句技术表述常有 40~60 字。

    ±40 曾把「order 库连接率 52%，处于正常范围」这类说明切掉一半。
    """
    assert metrics._EXCL_WINDOW >= 80, \
        f"排除判定窗口 {metrics._EXCL_WINDOW} 太窄，会切断排除性说明"
    long_form = (f"当前病灶为 rds-mysql-core（连接率 84%、内存 88%，均已越阈）；"
                 f"作为对照，{_OTHER} 的连接率仅 52%、内存 70%，各项指标均处于正常范围。")
    assert not metrics.judge_root_cause(long_form, _SC).cross_flag, long_form
    print(f"  ✓ 窗口 ±{metrics._EXCL_WINDOW} 字符，长句对照说明不被切断")


# 机理关键词判定：口径取自 cases.py 的真实设定
_KW = ["索引", "全表扫描", "慢查询"]


def test_keyword_hit_normal_cases():
    """★ 正常说出机理关键词时必须命中。"""
    for text in ("orders 表缺索引，触发全表扫描",
                 "慢查询不是唯一原因，但确实存在"):   # 否定词在后，不影响
        assert metrics._keyword_hit(text, _KW), text
    print("  ✓ 正常机理表述（含否定词在后）均命中")


def test_keyword_hit_rejects_negated_mechanism():
    """★★ 机理说反时不能算命中 —— 纯子串匹配的假阳性。

    回答“本次不是慢查询问题，而是缓存雪崩”把机理说反了，
    却因为含“慢查询”而被判机理命中。这会让机理得分虚高。
    """
    for text in ("根因是缓存雪崩，不是慢查询问题",
                 "本次并非索引缺失导致",
                 "已排除全表扫描的可能"):
        assert not metrics._keyword_hit(text, _KW), f"机理说反却判命中：{text}"
    print("  ✓ 否定语境下的机理词不误判为命中")


def test_table_hit_not_faked_by_service_name():
    """★★ 表名不能被同名前缀的服务名假命中。

    本项目 `inventory` 表与 `inventory-service` 服务真实共存：
    只提服务名时，纯子串匹配会把 `inventory` 当成表级命中，使表级定位分虚高。
    """
    assert not metrics._table_hit("inventory-service 的 CPU 饱和，与库无关", "inventory"), \
        "只提 inventory-service 却判表级命中"
    # 真命中：确实说到那张表（包括服务名与表名同句出现的情形）
    for text in ("rds-mysql-order 的 inventory 表缺复合索引",
                 "inventory-service 跨库查了 inventory 表导致慢"):
        assert metrics._table_hit(text, "inventory"), f"真表级命中被漏判：{text}"
    print("  ✓ 表名不被服务名假命中，真实表级引用仍命中")


def test_real_machine_verdicts_unchanged():
    """★ 修复不得改变已报的真机结论（只堆假阳性通道，不动真实判定）。

    三个场景的六份真机回答（model/intent 各一）在 §6.5 里都是 strict_ok=True。
    这里用固化的回答片段重判，防止以后改判定时静默改变历史结论。
    """
    # inventory 场景：真机回答确实同时出现了 inventory-service 与 inventory 表
    inv_ans = ("问题组件：数据库 rds-mysql-order 的 orders 表——缺索引导致全表扫描，"
               "直接受害方是 inventory-service（跨库查了 orders 表）")
    v = metrics.judge_root_cause(inv_ans, {"expected_instance": "rds-mysql-order",
                                           "expected_table": "orders"})
    assert v.instance_hit and v.table_hit and v.strict_ok, v
    print("  ✓ 真机回答片段在修复后仍判 strict_ok（未改历史结论）")


def test_undecidable_excluded_but_fp_still_counted():
    """★ 「不可判定」这个口径不能变成放水通道。

    背景：live 世界里上一场景的故障可能残留到下一场景的基线。若某条数据类
    期望规则在基线里已经 open，「新增」判定对它天然失效 —— 算 fn 是把评测污染
    当成系统漏报，算 tp 是给自己放水。所以要排除并如实记录。

    但排除的实现极易写错。第一版我写成 `detected & judged`，
    结果把**期望之外的检出（fp）也一起过滤掉了** —— precision 永远 100%。
    这条测试就守这个：排除不可判定项之后，真正的误报必须还在。
    """
    expected = ["DB-001", "DB-002", "API-001"]
    undecidable = ["API-001"]                      # 基线残留中已 open
    judged = [r for r in expected if r not in undecidable]
    detected = ({"DB-001", "DB-002", "CACHE-001"} | set(undecidable)) - set(undecidable)

    r = metrics.compare_sets(sorted(detected), judged)
    assert r.tp == 2, f"命中数不对：{r}"
    assert r.fp == 1, f"误报被吃掉了（这正是踩过的坑）：{r}"
    assert r.fn == 0, f"不该有漏报：{r}"
    print("  ✓ 排除不可判定项后，真正的误报仍被算出（fp=1）")


def test_undecidable_does_not_become_false_negative():
    """不可判定项不能算成漏报 —— 否则评测污染会被写成系统缺陷。

    实测发生过：slow_query_storm 的 API-001 因上一场景残留而不算「新增」，
    被判成漏报，把规则侧 F1 从 100% 压到 98%。那 1 个 fn 是评测造的。
    """
    expected = ["DB-002", "DB-001", "API-001", "CAP-004"]
    undecidable = ["API-001"]
    judged = [r for r in expected if r not in undecidable]
    detected = {"CAP-004", "DB-001", "DB-002"}     # 真机实测的新增集

    r = metrics.compare_sets(sorted(detected), judged)
    assert (r.tp, r.fp, r.fn) == (3, 0, 0), \
        f"排除不可判定项后应当全对，实际 {r}"
    print("  ✓ 基线残留导致的不可判定项未被算成漏报")


def main():
    groups = [
        ("串报判定：两个方向都要守住", [
            test_real_cross_reports_are_caught,
            test_correct_exclusions_not_flagged,
            test_override_words_do_not_swallow_valid_supplements,
            test_no_mention_means_no_cross,
            test_strict_ok_requires_no_cross,
            test_exclusion_window_covers_full_sentence,
        ]),
        ("机理关键词与表级：防假阳性", [
            test_keyword_hit_normal_cases,
            test_keyword_hit_rejects_negated_mechanism,
            test_table_hit_not_faked_by_service_name,
            test_real_machine_verdicts_unchanged,
        ]),
        ("不可判定口径：排除污染 vs 不许放水", [
            test_undecidable_excluded_but_fp_still_counted,
            test_undecidable_does_not_become_false_negative,
        ]),
    ]
    passed = failed = 0
    for title, tests in groups:
        print(f"\n=== {title} ===")
        for fn in tests:
            try:
                fn()
                passed += 1
            except Exception as e:                # noqa: BLE001
                failed += 1
                print(f"  ✗ {fn.__name__}: {e}")
    print("\n" + "=" * 46)
    print(f"通过 {passed} / 失败 {failed}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
