"""第 12 步（E-3 主动分级压缩）回归测试。

压缩的最大风险不是"压得不够"，而是**压掉了不该压的东西** ——
本项目所有结论都必须有数值出处，摘要一旦丢掉水位/行数/耗时，后续结论就失去依据。
所以本文件的重点是：分级是否按成本从低到高走、以及数值保真兜底是否真的兜住。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step12.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step12_test.db"
_TMP_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["DB_URL"] = f"sqlite:///{_TMP_DB}"

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app import config, db                                       # noqa: E402

if not config.DATABASE_URL.startswith("sqlite"):
    raise SystemExit(
        f"❌ 测试中止：数据库未隔离，当前指向 "
        f"{config.DATABASE_URL.split('@')[-1]}。测试必须连本地 SQLite。")

from app.harness import llm, loop, tool_results, verifier        # noqa: E402
from app.harness.context import ContextManager                   # noqa: E402
from app.harness.runctx import RunContext                        # noqa: E402
from app.tools import registry                                   # noqa: E402


def _big_tool_result(n_rows: int, marker: float) -> str:
    """造一份大工具结果，marker 放在靠后的行里（预览看不到，考验落盘取回）。"""
    rows = [{"url": f"/api/x{i}", "request_time": 0.1 + i / 1000} for i in range(n_rows)]
    rows.append({"url": "/api/orders", "request_time": marker})
    return json.dumps({"row_count": len(rows), "rows": rows}, ensure_ascii=False)


def _ctx_with_tool_results(n: int, rows: int = 60, prompt: str = "sys") -> ContextManager:
    ctx = ContextManager(prompt, session_id="step12")
    for i in range(n):
        ctx.messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "api_perf_stats", "arguments": "{}"}}]})
        # 直接塞进 messages，绕过 add_tool_result 的落盘 —— 本用例要的就是"未落盘的大结果"
        ctx.messages.append({
            "role": "tool", "tool_call_id": f"c{i}", "name": "api_perf_stats",
            "content": _big_tool_result(rows, 1000 + i)})
    return ctx


class _small_window:
    """临时把上下文阈值调小到便于构造的量级（默认回到早期的 24000/24000）。

    为什么需要：生产阈值现在按 200k 模型窗口标定（360k 字符 / 180k token），
    要在那个量级上构造"压力越界"得准备几十万字符的样本，既慢又脆。
    而压缩逻辑本身与阈值的绝对值无关，它只关心"是否越界"。
    让用例自带窗口，生产配置再怎么调都不会把这些用例打翻
    —— 上一次就是因为这些用例直接引用 config 的绝对值，调大窗口后集体失效。
    """

    def __init__(self, chars: int = 24000, tokens: int = 24000):
        self.want = (chars, tokens)

    def __enter__(self):
        self.orig = (config.CONTEXT_MAX_CHARS, config.CONTEXT_MAX_TOKENS)
        config.CONTEXT_MAX_CHARS, config.CONTEXT_MAX_TOKENS = self.want
        return self

    def __exit__(self, *exc):
        config.CONTEXT_MAX_CHARS, config.CONTEXT_MAX_TOKENS = self.orig
        return False


class _NoLLM:
    """确保用例不触达任何真实 LLM：可用性为 False，且调用即失败。"""

    def __enter__(self):
        self._orig = (llm.available, llm.chat_text, llm.get_client)
        llm.available = lambda: False
        llm.chat_text = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("本用例不应调用 LLM"))
        llm.get_client = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("本用例不应触达 LLM 客户端"))
        return self

    def __exit__(self, *exc):
        llm.available, llm.chat_text, llm.get_client = self._orig


# ══════════════════════════════════════════════════════
# 1. 压力度量：字符 + token 双维度
# ══════════════════════════════════════════════════════

def test_pressure_uses_max_of_two_dimensions():
    """★ 压力取字符占比与 token 占比的较大者。

    只看字符会让中文为主的会话压缩得太晚（中文 ~1 token/字，
    同样字符数的 token 数是 JSON 的 4 倍多）。
    """
    cjk = ContextManager("中" * 6000, session_id="p1")
    jsonish = ContextManager('{"k":"v"}' * 700, session_id="p2")
    assert len(cjk.messages[0]["content"]) < len(jsonish.messages[0]["content"]), \
        "构造失败：中文样本应当更短"
    # 中文更短却压力更大 —— 说明 token 维度起了作用
    assert cjk.pressure() > jsonish.pressure(), (cjk.pressure(), jsonish.pressure())
    print(f"  ✓ 中文 {len(cjk.messages[0]['content'])} 字符压力 {cjk.pressure():.2f} > "
          f"JSON {len(jsonish.messages[0]['content'])} 字符压力 {jsonish.pressure():.2f}")


def test_pressure_below_soft_ratio_does_nothing():
    """★ 压力低于软阈值时压缩必须【零动作】—— 不能没事就压。"""
    ctx = ContextManager("sys", session_id="p3")
    ctx.add_user("一句很短的话")
    before = list(ctx.messages)
    with _NoLLM():
        acted = ctx.compact()
    assert acted is None, acted
    assert ctx.messages == before
    print(f"  ✓ 压力 {ctx.pressure():.3f} <= {config.CONTEXT_SOFT_RATIO}，未做任何动作")


# ══════════════════════════════════════════════════════
# 2. L1 微压缩：零 LLM 成本
# ══════════════════════════════════════════════════════

def test_micro_compact_costs_no_llm_call():
    """★★ 微压缩必须零 LLM 调用 —— 这是它作为第一档存在的全部理由。

    _NoLLM 会让任何 LLM 调用直接抛 AssertionError，所以这条能真正证明"没花钱"。
    """
    with _small_window():
        ctx = _ctx_with_tool_results(6)
        assert ctx.pressure() > config.CONTEXT_SOFT_RATIO, ctx.pressure()
        with _NoLLM():
            freed = ctx.micro_compact()
        assert freed > 0, "什么都没释放"
    print(f"  ✓ 微压缩释放 {freed} 字符，全程零 LLM 调用")


def test_micro_compact_keeps_data_retrievable():
    """★★ 微压缩不丢信息：被压掉的内容仍可用 read_tool_result 取回。

    这是它与"硬截断"的本质区别 —— 截断是永久丢失，落盘只是变成按需。
    """
    ctx = _ctx_with_tool_results(6)
    marker = 1000.0                     # 第 0 条结果里靠后的行
    with _NoLLM():
        ctx.micro_compact()
    tool_msg = next(m for m in ctx.messages if m.get("role") == "tool")
    assert "<persisted-output>" in tool_msg["content"], tool_msg["content"][:200]
    # 从预览里抠出落盘路径，验证全文确实取得回来
    import re
    m = re.search(r"已保存到：(\S+)", tool_msg["content"])
    assert m, tool_msg["content"]
    registry.ensure_loaded()
    out = json.loads(registry.execute("read_tool_result",
                                      {"path": m.group(1), "limit": 5}))
    assert "error" not in out, out
    body = "".join(out["lines"])
    assert f"{marker:g}" in body, "落盘全文里找不到 marker（构造或取回有问题）"
    print(f"  ✓ 被压内容仍可取回（落盘 {out['total_lines']} 行，marker {marker:g} 在内）")


def test_micro_compact_prefers_oldest():
    """★ 从最老的开始压，尾窗里的新结果尽量不动（那是模型当下正在用的）。"""
    ctx = _ctx_with_tool_results(8)
    with _NoLLM():
        ctx.micro_compact()
    tool_msgs = [m for m in ctx.messages if m.get("role") == "tool"]
    compressed = ["<persisted-output>" in m["content"] for m in tool_msgs]
    assert compressed[0], "最老的一条没被压"
    assert not compressed[-1], "最新的一条被压了（应尽量保留尾窗）"
    print(f"  ✓ {sum(compressed)}/{len(compressed)} 条被压，"
          f"顺序为最老优先（最新一条保留）")


def test_micro_compact_stops_when_pressure_ok():
    """压到压力回落就停 —— 不做过度压缩。"""
    ctx = _ctx_with_tool_results(10)
    with _NoLLM():
        ctx.micro_compact()
    tool_msgs = [m for m in ctx.messages if m.get("role") == "tool"]
    compressed = sum("<persisted-output>" in m["content"] for m in tool_msgs)
    assert 0 < compressed < len(tool_msgs), \
        f"压了 {compressed}/{len(tool_msgs)} 条，要么没压要么全压了"
    assert ctx.pressure() <= config.CONTEXT_SOFT_RATIO or compressed == len(tool_msgs)
    print(f"  ✓ 压到压力 {ctx.pressure():.2f} 即停（{compressed}/{len(tool_msgs)} 条）")


def test_micro_compact_never_recompresses():
    """★ 已是指针形态的结果不会被反复压。

    注意"幂等"在这里【不等于】第二次释放 0：压力仍高于软阈值时，
    第二次会继续压**后面还没压过的**条目，这是正确行为。
    真正的不变量是：同一条 tool 结果不会被压第二次（否则每步都白跑一遍，
    还会把预览再套一层预览）。
    """
    ctx = _ctx_with_tool_results(6)
    with _NoLLM():
        ctx.micro_compact()
        snapshot = {i: m["content"] for i, m in enumerate(ctx.messages)
                    if m.get("role") == "tool" and "<persisted-output>" in m["content"]}
        assert snapshot, "第一次什么都没压"
        ctx.micro_compact()
    for i, before in snapshot.items():
        assert ctx.messages[i]["content"] == before, f"第 {i} 条被重复压缩了"
    # 也不该出现预览套预览
    for m in ctx.messages:
        if m.get("role") == "tool":
            assert str(m["content"]).count("<persisted-output>") <= 1, m["content"][:200]
    print(f"  ✓ 已压的 {len(snapshot)} 条内容不变，且无预览套预览")


def test_small_tool_results_untouched():
    """小结果不动 —— 换成指针省不下多少，反而丢了直接可读性。"""
    ctx = ContextManager("sys", session_id="small")
    ctx.messages.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c0", "type": "function",
         "function": {"name": "get_topology", "arguments": "{}"}}]})
    tiny = json.dumps({"nodes": 3, "edges": 2})
    ctx.messages.append({"role": "tool", "tool_call_id": "c0",
                         "name": "get_topology", "content": tiny})
    with _NoLLM():
        ctx.micro_compact()
    assert ctx.messages[-1]["content"] == tiny
    print(f"  ✓ {len(tiny)} 字符的小结果保持原样"
          f"（阈值 {config.MICRO_COMPACT_MIN_CHARS}）")


# ══════════════════════════════════════════════════════
# 3. 阶梯顺序：先便宜的，再贵的
# ══════════════════════════════════════════════════════

def test_ladder_tries_micro_before_summary():
    """★★ 阶梯顺序：能靠微压缩解决就不动 LLM 摘要。

    原来只要越过阈值就直接花一次 LLM 往返，而中段往往就是几条大工具结果。
    """
    with _small_window():
        ctx = _ctx_with_tool_results(6)
        with _NoLLM():                  # LLM 一旦被调用就抛错
            acted = ctx.compact()
        assert acted and acted["level"] == "micro", acted
        assert acted["freed_chars"] > 0
        assert ctx.total_chars() <= config.CONTEXT_MAX_CHARS
    print(f"  ✓ 微压缩即解决（释放 {acted['freed_chars']} 字符），未动用 LLM 摘要")


def test_ladder_falls_back_to_summary_when_micro_not_enough():
    """★ 微压缩不够时才升级到 LLM 摘要，且返回 level=summary。

    构造：大量【无法微压缩】的内容（assistant 文本，不是 tool 结果）。
    """
    with _small_window():
        ctx = ContextManager("sys", session_id="ladder2")
        for i in range(40):
            ctx.messages.append({"role": "user",
                                 "content": f"第{i}轮：" + "详细描述" * 200})
            ctx.messages.append({"role": "assistant",
                                 "content": f"回答{i}：" + "分析" * 200})
        assert ctx.pressure() > 1.0, ctx.pressure()

        calls = {"n": 0}
        orig = (llm.available, llm.chat_text)
        llm.available = lambda: True
        llm.chat_text = lambda *a, **kw: (calls.__setitem__("n", calls["n"] + 1)
                                          or "摘要：前序为压力测试内容")
        try:
            acted = ctx.compact()
        finally:
            llm.available, llm.chat_text = orig
        assert acted and acted["level"] == "summary", acted
        assert calls["n"] == 1, f"LLM 调用 {calls['n']} 次"
        assert ctx.pressure() < 1.0, ctx.pressure()
    print(f"  ✓ 微压缩无从下手 → 升级摘要（1 次快模型调用，"
          f"压力降到 {ctx.pressure():.2f}）")


def test_compact_emits_event_through_loop():
    """★ 压缩动作要能被前端看到 —— 此前只有紧急压缩有事件，主动压缩完全不可见。"""
    registry.ensure_loaded()
    state = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return type("M", (), {"content": "查", "tool_calls": [
                type("T", (), {"id": "c1", "type": "function",
                               "function": type("F", (), {
                                   "name": "api_perf_stats",
                                   "arguments": "{}"})()})()]})()
        return type("M", (), {"content": "结论", "tool_calls": []})()

    # 压力必须来自【无法微压缩】之外的地方也能触发：这里用一个很大的中文
    # system prompt 把压力顶上去。工具结果本身会被 add_tool_result 按
    # max_result_chars 自动落盘，反而顶不起来。
    big = _big_tool_result(30, 999)
    orig = (llm.chat_with_retry, llm.get_client, registry.execute)
    llm.chat_with_retry = chat
    llm.get_client = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    registry.execute = lambda n, a, run=None: big
    try:
        with _small_window():
            run = RunContext(session_id="evt", mode="readonly")
            events = list(loop.run_agent(
                {"name": "T", "system_prompt": "系统提示词" * 3000, "model": "m",
                 "tools": ["api_perf_stats"]}, "任务", run=run))
    finally:
        llm.chat_with_retry, llm.get_client, registry.execute = orig

    comp = [e for e in events if e["type"] == "compacted"]
    assert comp, [e["type"] for e in events]
    assert comp[0]["level"] in ("micro", "summary"), comp[0]
    assert comp[0]["freed_chars"] > 0 and comp[0]["reason"]
    print(f"  ✓ loop 产出 compacted 事件：level={comp[0]['level']} "
          f"freed={comp[0]['freed_chars']}")


# ══════════════════════════════════════════════════════
# 4. 数值保真兜底（压缩最大的风险）
# ══════════════════════════════════════════════════════

def test_fidelity_note_keeps_numbers_verbatim():
    """★★ 补录的数值必须与原文**逐字一致**，不能被格式化改写。

    第一版用 f"{v:g}" 格式化，把 1162951 写成 `1.16295e+06`：模型对不上
    工具结果里的原始数字，而且精度真的丢了（1.16295e6 = 1162950）。
    "保真补录"用一个会改写数值的格式化函数，等于自相矛盾。
    """
    source = "扫描 1162951 行，P99 1.244s，内存 85.3%，请求数 1850"
    note = ContextManager._number_fidelity_note(source, "摘要：无数值")
    for raw in ("1162951", "1.244", "85.3", "1850"):
        assert raw in note, f"{raw} 未逐字保留：{note}"
    assert "e+" not in note and "E+" not in note, f"出现科学计数法：{note}"
    print(f"  ✓ 4 个数值逐字保留、无科学计数法")


def test_fidelity_note_recovers_lost_numbers():
    """★★ 摘要丢掉的关键数值必须被补录回来。

    本项目所有结论都要有数值出处，"请保留关键数值"只是请求 ——
    模型做不到时没人知道。所以压缩后做机械对比并补录。
    """
    source = ("连接使用率 81.9%，rows_examined 1162951，rows_sent 7，"
              "P99 1.244s，超卖率 166.67%")
    digest = "摘要：数据库压力偏高，接口变慢"        # 故意把所有数值都丢了
    note = ContextManager._number_fidelity_note(source, digest)
    assert "保真补录" in note, note
    for n in ("1162951", "166.67", "81.9", "1.244"):
        assert n in note, f"{n} 未被补录：{note}"
    print(f"  ✓ 4 个关键数值全部补录：{note[-60:]}")


def test_fidelity_note_silent_when_nothing_lost():
    """摘要保住了数值就不该加噪声。"""
    source = "连接使用率 81.9%，扫描 1162951 行"
    digest = "连接使用率 81.9%，扫描行数 1162951"
    assert ContextManager._number_fidelity_note(source, digest) == ""
    print("  ✓ 数值未丢失时不追加任何内容")


def test_fidelity_ignores_small_integers():
    """★ 只补录有信息量的数值：序号、条数这类小整数丢了无所谓。

    否则补录行会被 1/2/3 淹没，真正重要的水位反而看不见。
    """
    source = "共 7 条，其中 P1 4 条、P2 3 条；内存使用率 85.3%"
    digest = "存在若干风险"
    note = ContextManager._number_fidelity_note(source, digest)
    assert "85.3" in note, note
    assert " 7," not in note and " 4," not in note, f"小整数被补录了：{note}"
    print(f"  ✓ 只补录 85.3，忽略 7/4/3 这类小整数")


def test_fidelity_caps_note_length():
    """补录条数有上限，否则"压缩"反而让上下文膨胀。"""
    source = " ".join(str(1000 + i) for i in range(200))
    note = ContextManager._number_fidelity_note(source, "摘要")
    assert note.count(",") <= 12, note
    assert len(note) < 400, len(note)
    print(f"  ✓ 200 个数值只补录 {note.count(',') + 1} 个（{len(note)} 字符）")


def test_summarize_appends_fidelity_note():
    """★ 保真兜底要真的接在 _summarize 的产出上（不只是个独立函数）。"""
    messages = [
        {"role": "user", "content": "查一下连接使用率"},
        {"role": "assistant", "content": "连接使用率 81.9%，扫描 1162951 行，P99 1.244s"},
    ]
    orig = (llm.available, llm.chat_text)
    llm.available = lambda: True
    llm.chat_text = lambda *a, **kw: "摘要：数据库压力偏高"   # 丢掉全部数值
    try:
        digest = ContextManager._summarize(messages)
    finally:
        llm.available, llm.chat_text = orig
    assert "保真补录" in digest and "1162951" in digest, digest
    print("  ✓ _summarize 的产出已带保真补录")


def test_summarize_offline_fallback_unaffected():
    """无 LLM 时回落到硬截断，不该因为兜底逻辑而报错。"""
    messages = [{"role": "user", "content": "内存 85.3%"} for _ in range(50)]
    with _NoLLM():
        digest = ContextManager._summarize(messages)
    assert digest and "85.3" in digest
    assert "保真补录" not in digest, "离线回落路径不该加补录（原文已在里面）"
    print(f"  ✓ 离线回落到截断（{len(digest)} 字符），未附补录")


def test_extract_numbers_shared_with_verifier():
    """★ 保真校验复用 verifier 的数值提取 —— 两处口径必须一致。

    若各写一份，"什么算关键数值"就会在核对与压缩之间漂移。
    """
    text = "P99 1.244s，扫描 1162951 行，共 7 条"
    nums = verifier.extract_numbers(text)
    assert 1.244 in nums and 1162951.0 in nums
    assert 7.0 not in nums, "小整数不该算关键数值"
    print(f"  ✓ 共用 verifier.extract_numbers（本例提取 {len(nums)} 个）")


def main():
    db.init_db()
    registry.ensure_loaded()
    tool_results.cleanup("step12")
    groups = [
        ("压力度量（字符 + token 双维度）", [
            test_pressure_uses_max_of_two_dimensions,
            test_pressure_below_soft_ratio_does_nothing,
        ]),
        ("L1 微压缩（零 LLM 成本）", [
            test_micro_compact_costs_no_llm_call,
            test_micro_compact_keeps_data_retrievable,
            test_micro_compact_prefers_oldest,
            test_micro_compact_stops_when_pressure_ok,
            test_micro_compact_never_recompresses,
            test_small_tool_results_untouched,
        ]),
        ("阶梯顺序与可观测性", [
            test_ladder_tries_micro_before_summary,
            test_ladder_falls_back_to_summary_when_micro_not_enough,
            test_compact_emits_event_through_loop,
        ]),
        ("数值保真兜底", [
            test_fidelity_note_keeps_numbers_verbatim,
            test_fidelity_note_recovers_lost_numbers,
            test_fidelity_note_silent_when_nothing_lost,
            test_fidelity_ignores_small_integers,
            test_fidelity_caps_note_length,
            test_summarize_appends_fidelity_note,
            test_summarize_offline_fallback_unaffected,
            test_extract_numbers_shared_with_verifier,
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
    tool_results.cleanup("step12")
    print("\n" + "=" * 46)
    print(f"通过 {passed} / 失败 {failed}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
