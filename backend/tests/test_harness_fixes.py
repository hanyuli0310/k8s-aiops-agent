"""Part A（3 个 Bug）回归测试。

零依赖，直接运行即可（无需 pytest）：
    cd backend && .venv/bin/python tests/test_harness_fixes.py

用临时 SQLite 完全隔离，不会碰真实库。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ── 必须在导入 app.* 之前设置：指向临时 SQLite，避免污染真实库 ──
_TMP_DB = Path(tempfile.gettempdir()) / "harness_fix_test.db"
_TMP_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["HARNESS_STRICT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db                                    # noqa: E402

# ── 数据库隔离闸门 ──
# 这些用例会 DELETE / INSERT。若 DATABASE_URL 没生效而指向线上 RDS，
# 后果是真实数据被删（写 data_collector 测试时真踩过一次）。
# 宁可整个测试跑不起来，也不允许带着错的连接串继续。
from app import config as _cfg                                   # noqa: E402

if not _cfg.DATABASE_URL.startswith("sqlite"):
    raise SystemExit(
        f"❌ 测试中止：数据库未隔离，当前指向 "
        f"{_cfg.DATABASE_URL.split('@')[-1]}。测试必须连本地 SQLite。")
from app.harness import memory                                # noqa: E402
from app.harness.context import ContextManager                # noqa: E402
from app.harness.loop import run_agent                        # noqa: E402

# ── 测试用的消息构造 helper ──


def _sys():
    return {"role": "system", "content": "SYS"}


def _user(t="U"):
    return {"role": "user", "content": t}


def _assistant(*call_ids, content=""):
    """带 tool_calls 的 assistant 消息。"""
    entry = {"role": "assistant", "content": content}
    if call_ids:
        entry["tool_calls"] = [
            {"id": cid, "type": "function",
             "function": {"name": f"tool_{cid}", "arguments": '{"k":"v"}'}}
            for cid in call_ids
        ]
    return entry


def _tool(cid, size=100):
    return {"role": "tool", "tool_call_id": cid, "name": f"tool_{cid}",
            "content": "R" * size}


def _ctx(messages):
    """构造一个 ContextManager 并塞入指定消息，同时 stub 掉 LLM 摘要。

    注意：这里必须赋普通 lambda 而不是 staticmethod(...)。Python 3.9 中
    staticmethod 对象本身不可调用，赋给【实例】属性后调用会 TypeError
    （类属性可以，实例属性不行）。
    """
    c = ContextManager("SYS")
    c.messages = list(messages)
    c._summarize = lambda msgs: "[STUB-SUMMARY]"     # 实例属性遮蔽类方法，无需 self
    return c


# ══════════════════════════════════════════════════════════════
# Bug 1：压缩切断 tool_call / tool_result 配对
# ══════════════════════════════════════════════════════════════

def test_bug1_old_slicing_would_break():
    """先证明 bug 确实存在：旧的 messages[-6:] 切法会以孤立 tool 消息开头。"""
    msgs = [_sys(), _user(),
            _assistant("a"), _tool("a"),
            _assistant("b"), _tool("b"),
            _assistant("c", "d"), _tool("c"), _tool("d")]
    assert len(msgs) == 9
    old_tail = msgs[-6:]                       # 旧实现的切法
    assert old_tail[0]["role"] == "tool", "前置条件不成立，用例失效"
    assert old_tail[0]["tool_call_id"] == "a"
    # 而 tool(a) 对应的 assistant 在 msgs[2]，会被压进摘要 → 线上 400
    print("  ✓ 已复现旧切法的缺陷：tail 以孤立 tool(a) 开头")


def test_bug1_safe_tail_start_moves_back():
    """_safe_tail_start 应把起点从 3（tool）前移到 2（其 assistant）。"""
    c = _ctx([_sys(), _user(),
              _assistant("a"), _tool("a"),
              _assistant("b"), _tool("b"),
              _assistant("c", "d"), _tool("c"), _tool("d")])
    start = c._safe_tail_start(want_tail=6)
    assert start == 2, f"期望 2，实际 {start}"
    assert c.messages[start]["role"] == "assistant"
    print(f"  ✓ 起点从 3 前移到 {start}（assistant）")


class _small_window:
    """临时把上下文阈值调小到便于构造的量级（早期的 24000/24000）。

    生产阈值已按 200k 模型窗口标定（360k 字符 / 180k token），在那个量级上
    构造"越界"要几十万字符，既慢又脆。压缩逻辑只关心"是否越界"，
    与阈值绝对值无关 —— 让用例自带窗口，生产配置再调也不会打翻它们。
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


def test_bug1_compact_preserves_pairing_basic():
    c = _ctx([_sys(), _user(),
              _assistant("a"), _tool("a"),
              _assistant("b"), _tool("b"),
              _assistant("c", "d"), _tool("c"), _tool("d")])
    with _small_window():
        c.messages[1]["content"] = "U" * (config.CONTEXT_MAX_CHARS + 100)  # 强制超窗口
        c.compact()
    c.assert_api_invariants()                  # 不抛异常即通过
    assert c.messages[0]["role"] == "system"
    print(f"  ✓ 压缩后配对完好，剩余 {len(c.messages)} 条消息")


def test_bug1_compact_with_six_parallel_tool_calls():
    """单条 assistant 携带 6 个 tool_calls —— 旧实现下 tail 全是 tool，必崩。"""
    ids = list("abcdef")
    msgs = [_sys(), _user(), _assistant("z"), _tool("z"), _assistant(*ids)]
    msgs += [_tool(i) for i in ids]
    c = _ctx(msgs)
    assert all(m["role"] == "tool" for m in c.messages[-6:]), "前置条件不成立"
    c.messages[1]["content"] = "U" * (config.CONTEXT_MAX_CHARS + 100)
    c.compact()
    c.assert_api_invariants()
    print("  ✓ 6 个并行 tool_calls 场景压缩后配对完好")


def test_bug1_compact_many_rounds():
    """模拟 12 步 × 每步 3 个并行调用的长会话，反复压缩都不应破坏不变量。

    工具结果刻意放大到 1200 字符 —— 12×3×1200 ≈ 43k 会突破
    CONTEXT_MAX_CHARS(24000)，确保 compact() 真的被触发多次
    （否则这个用例是假通过：压缩从未执行）。
    """
    c = _ctx([_sys(), _user("查一下集群状态")])
    compacted_times = 0
    prev_len = len(c.messages)
    with _small_window():
        for step in range(12):
            ids = [f"s{step}_{k}" for k in range(3)]
            c.messages.append(_assistant(*ids, content=f"第{step}步思考"))
            for i in ids:
                c.messages.append(_tool(i, size=1200))
            before = len(c.messages)
            c.compact()
            if len(c.messages) < before:
                compacted_times += 1
            c.assert_api_invariants()
            prev_len = len(c.messages)
    assert compacted_times > 0, "压缩从未触发，用例无效（需放大工具结果）"
    print(f"  ✓ 12 步 ×3 并行调用：压缩触发 {compacted_times} 次，"
          f"全程配对完好，最终 {prev_len} 条 / {c.total_chars()} 字符")


def test_bug1_assert_detects_orphan():
    """护栏本身要有效：手工构造孤立 tool 消息，必须被检测出来。"""
    c = _ctx([_sys(), _user(), _tool("ghost")])
    try:
        c.assert_api_invariants()
    except AssertionError as e:
        assert "ghost" in str(e)
        print("  ✓ 护栏成功检出孤立 tool 消息")
        return
    raise AssertionError("护栏失效：孤立 tool 消息未被检出")


def test_bug1_force_compact():
    ids = list("abcdef")
    msgs = [_sys(), _user("U" * 5000), _assistant("z"), _tool("z"), _assistant(*ids)]
    msgs += [_tool(i) for i in ids]
    c = _ctx(msgs)
    before = c.total_chars()
    freed = c.force_compact(keep_tail=4)
    c.assert_api_invariants()
    assert freed > 0, "force_compact 应释放字符"
    assert c.total_chars() < before
    print(f"  ✓ force_compact 释放 {freed} 字符（{before} → {c.total_chars()}）")


def test_bug1_total_chars_counts_tool_calls():
    """total_chars 必须把 tool_calls 的 arguments 计入，否则该压缩时不压缩。"""
    big_args = '{"sql":"' + "S" * 3000 + '"}'
    msg = {"role": "assistant", "content": "",
           "tool_calls": [{"id": "x", "type": "function",
                           "function": {"name": "sql_query", "arguments": big_args}}]}
    c = _ctx([_sys(), msg])
    n = c.total_chars()
    assert n > 3000, f"tool_calls 未被计入，total_chars={n}"
    print(f"  ✓ total_chars 计入 tool_calls：{n} 字符")


# ══════════════════════════════════════════════════════════════
# Bug 2：步数耗尽丢弃已积累结论
# ══════════════════════════════════════════════════════════════

class _FakeFn:
    def __init__(self, name, arguments='{}'):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, cid, name):
        self.id = cid
        self.type = "function"
        self.function = _FakeFn(name)


class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


def _run_with_stubs(chat_impl, tools=("query_metrics",)):
    """在 stub 掉 LLM / registry.execute / _summarize 的前提下跑 run_agent。

    ⚠️ loop.py 走的是 llm.chat_with_retry（不是 chat）。这里两个都 stub：
    - chat_with_retry: run_agent 主路径
    - chat:            _summarize 等辅助路径
    并额外把 get_client 换成会抛错的实现作为【防线】—— 若将来 loop 改用别的
    入口导致 stub 失效，测试会立刻炸而不是静默走真实网络烧 token。
    （本用例组曾因此真实调用过线上模型。）
    """
    from app.harness import llm as llm_mod
    from app.harness import loop as loop_mod
    from app.tools import registry as reg_mod

    def _guard():
        raise AssertionError(
            "测试试图创建真实 LLM 客户端 —— stub 已失效，请检查 loop.py 的调用入口")

    orig = (llm_mod.chat, llm_mod.chat_with_retry, llm_mod.get_client,
            reg_mod.execute, ContextManager._summarize)
    llm_mod.chat = chat_impl
    llm_mod.chat_with_retry = chat_impl
    llm_mod.get_client = _guard
    reg_mod.execute = lambda name, args, run=None: '{"ok":true,"rows":[]}'
    ContextManager._summarize = staticmethod(lambda msgs: "[STUB]")
    try:
        agent = {"name": "TestAgent", "system_prompt": "SYS", "tools": list(tools)}
        return list(run_agent(agent, "定位下单接口变慢的根因"))
    finally:
        (llm_mod.chat, llm_mod.chat_with_retry, llm_mod.get_client,
         reg_mod.execute, ContextManager._summarize) = orig


def test_bug2_exhausted_steps_keeps_thinking():
    """模型始终调工具直到步数耗尽 —— 答案必须包含已积累的思考，而非固定 fallback。

    本用例同时覆盖 E-2 的一条闸门：stub 从不调 update_plan，所以 run.plan 为空，
    `can_continue()` 判定"没有任务清单，无法判断是否还有未完成步骤"→ 不自动续跑，
    直接走 wrap_up 收口。即"没有结构化的未完成信号就不猜"。
    """
    calls = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        calls["n"] += 1
        if tools is None:                       # 收口调用（tools=None）
            return _FakeMsg(content="【阶段性结论】order-service 的 P99 达 3.2s，"
                                    "疑似 orders 表无索引全表扫描；证据不足项：慢日志未确认")
        return _FakeMsg(content=f"第{calls['n']}步：继续查",
                        tool_calls=[_FakeToolCall(f"c{calls['n']}", "query_metrics")])

    events = _run_with_stubs(chat)
    answer = next(e for e in events if e["type"] == "answer")["text"]

    assert "未能得出结论" not in answer, f"仍走了旧的 fallback：{answer[:120]}"
    assert "阶段性结论" in answer
    assert "order-service" in answer
    # 措辞随 E-2 改为"已用尽执行预算（N 段 × M 步）"：续跑上线后，
    # 只说"撞了步数上限"会漏掉"跑了几段"这个关键信息。
    assert "已用尽执行预算" in answer, answer[:160]
    assert not any(e["type"] == "continued" for e in events), "无 plan 时不应续跑"
    # 收口调用发生了：共 MAX_AGENT_STEPS + 1 次
    assert calls["n"] == config.MAX_AGENT_STEPS + 1, f"实际调用 {calls['n']} 次"
    print(f"  ✓ 步数耗尽后成功收口（{calls['n']} 次 LLM 调用），结论保留且未误续跑")


def test_bug2_wrapup_falls_back_when_llm_fails():
    """收口调用也失败时，必须回落到最近一次思考文本，而不是丢空。"""
    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        if tools is None:
            raise RuntimeError("模拟收口调用失败")
        return _FakeMsg(content="已确认 trace 中 SQL 耗时占 87%",
                        tool_calls=[_FakeToolCall("c1", "query_metrics")])

    events = _run_with_stubs(chat)
    answer = next(e for e in events if e["type"] == "answer")["text"]
    assert "87%" in answer, f"未回落到 last_thinking：{answer[:120]}"
    assert "已用尽执行预算" in answer, answer[:160]
    print("  ✓ 收口失败时正确回落到 last_thinking")


def test_bug2_normal_finish_unaffected():
    """正常终态（模型不再调工具）行为不应改变。"""
    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        return _FakeMsg(content="根因已确认：orders 表缺少 (status, created_at) 复合索引")

    events = _run_with_stubs(chat)
    answer = next(e for e in events if e["type"] == "answer")["text"]
    assert answer.startswith("根因已确认")
    assert "已达最大执行步数" not in answer
    print("  ✓ 正常终态路径未受影响")


# ══════════════════════════════════════════════════════════════
# Bug 3：governance 记忆无限增长挤掉诊断结论
# ══════════════════════════════════════════════════════════════

def _reset_memory_table():
    db.execute("DELETE FROM agent_memory")


def test_bug3_governance_upsert_not_insert():
    """同一资源重复治理只应留 1 条记录。"""
    _reset_memory_table()
    from app.tools.remediation_tools import _record_governance
    for i in range(5):
        _record_governance("patch:order-service:set_replicas", f"replicas: 1 -> {i + 2}")
    rows = db.fetch_all(
        "SELECT content FROM agent_memory WHERE scope='governance' "
        "AND mem_key='patch:order-service:set_replicas'")
    assert len(rows) == 1, f"应 upsert 成 1 条，实际 {len(rows)} 条"
    assert "-> 6" in rows[0]["content"], "应保留最新内容"
    print("  ✓ 治理记录按资源合并（5 次写入 → 1 条，内容为最新）")


def test_bug3_recall_scope_quota():
    """大量 governance 记录不得把 conclusion 挤出 recall 结果。"""
    _reset_memory_table()
    memory.remember("conclusion", "最近一次故障定位结论",
                    "order-service P99 3.2s，根因 orders 表无索引")
    memory.remember("conclusion", "最近一次风险扫描摘要", "11 条风险：P1 4 / P2 7")
    for i in range(30):                        # 模拟多轮 demo 后的治理记录堆积
        memory.remember("governance", f"patch:svc-{i}:set_replicas", f"replicas -> {i}")

    rows = memory.recall(limit=8)
    scopes = [r["scope"] for r in rows]
    n_conclusion = scopes.count("conclusion")
    n_governance = scopes.count("governance")

    assert n_conclusion >= 1, f"conclusion 被挤空了：{scopes}"
    assert n_governance <= memory.SCOPE_QUOTA["governance"], \
        f"governance 超配额：{n_governance}"
    print(f"  ✓ 配额生效：conclusion={n_conclusion} governance={n_governance}"
          f"（配额 {memory.SCOPE_QUOTA}）")


def test_bug3_prune_never_touches_governance():
    """★ 回归护栏：governance 条目绝不能被 prune 删除。

    static 模式下 rules/builtin.py 的 _has_governance_prefix() 靠这些条目判定
    finding 是否 resolved_by_governance。删掉标记 = 已治理的风险翻回 open，
    直接破坏「全量治理后 0 open / 11 resolved」验收基准。

    （这是修 Bug 3 时真实踩到的坑：把「状态标记」当成「可淘汰的缓存」处理。）
    """
    _reset_memory_table()
    for i in range(40):                        # 远超 KEEP_PER_SCOPE
        memory.remember("governance", f"db_index:tbl_{i}", f"CREATE INDEX idx_{i}")
    n = db.fetch_one(
        "SELECT COUNT(*) AS c FROM agent_memory WHERE scope='governance'")["c"]
    assert n == 40, f"governance 条目被误删！应保留 40 条，实际 {n} 条"

    # 抽查最早的标记仍能被规则层的前缀查询命中
    hit = db.fetch_one(
        "SELECT id FROM agent_memory WHERE scope='governance' AND mem_key LIKE :k",
        {"k": "db_index:tbl_0%"})
    assert hit is not None, "最早的治理标记丢失，resolved 判定会失效"
    print(f"  ✓ governance 免于 prune：40 条全部保留，最早标记可查")


def test_bug3_prune_still_caps_other_scopes():
    """可清理的 scope（conclusion/preference）仍受 KEEP_PER_SCOPE 约束。"""
    _reset_memory_table()
    for i in range(40):
        memory.remember("preference", f"pref_{i}", f"偏好 {i}")
    n = db.fetch_one(
        "SELECT COUNT(*) AS c FROM agent_memory WHERE scope='preference'")["c"]
    assert n <= memory.KEEP_PER_SCOPE, f"preference 未被 prune，实际 {n} 条"
    print(f"  ✓ preference 仍被 prune：40 次写入 → {n} 条"
          f"（上限 {memory.KEEP_PER_SCOPE}）")


def test_bug3_governance_bounded_by_upsert():
    """governance 不 prune 也不会无限增长 —— key 是资源级的且走 upsert。"""
    _reset_memory_table()
    from app.tools.remediation_tools import _record_governance
    # 模拟 100 次治理，但只涉及 3 个资源 × 2 个动作
    for i in range(100):
        svc = ["order-service", "user-service", "frontend"][i % 3]
        act = ["set_replicas", "add_probes"][i % 2]
        _record_governance(f"patch:{svc}:{act}", f"第 {i} 次治理")
    n = db.fetch_one(
        "SELECT COUNT(*) AS c FROM agent_memory WHERE scope='governance'")["c"]
    assert n <= 6, f"upsert 未生效，100 次治理产生了 {n} 条（应 ≤6）"
    print(f"  ✓ upsert 天然有界：100 次治理 / 3 资源 ×2 动作 → {n} 条")


def test_bug3_memory_prompt_contains_conclusion():
    """端到端：注入系统提示词的记忆片段里必须还能看到诊断结论。"""
    _reset_memory_table()
    memory.remember("conclusion", "最近一次故障定位结论", "根因：orders 表缺索引")
    for i in range(25):
        memory.remember("governance", f"pdb:svc-{i}", f"创建 PDB svc-{i}")
    prompt = memory.memory_prompt("下单接口很慢")
    assert "orders 表缺索引" in prompt, f"诊断结论未进提示词：\n{prompt}"
    print("  ✓ memory_prompt 中诊断结论未被治理记录挤掉")


# ══════════════════════════════════════════════════════════════

def main():
    db.init_db()
    groups = [
        ("Bug 1 · 上下文压缩配对", [
            test_bug1_old_slicing_would_break,
            test_bug1_safe_tail_start_moves_back,
            test_bug1_compact_preserves_pairing_basic,
            test_bug1_compact_with_six_parallel_tool_calls,
            test_bug1_compact_many_rounds,
            test_bug1_assert_detects_orphan,
            test_bug1_force_compact,
            test_bug1_total_chars_counts_tool_calls,
        ]),
        ("Bug 2 · 步数耗尽保留结论", [
            test_bug2_exhausted_steps_keeps_thinking,
            test_bug2_wrapup_falls_back_when_llm_fails,
            test_bug2_normal_finish_unaffected,
        ]),
        ("Bug 3 · 长期记忆配额", [
            test_bug3_governance_upsert_not_insert,
            test_bug3_recall_scope_quota,
            test_bug3_prune_never_touches_governance,
            test_bug3_prune_still_caps_other_scopes,
            test_bug3_governance_bounded_by_upsert,
            test_bug3_memory_prompt_contains_conclusion,
        ]),
    ]

    passed = failed = 0
    for title, tests in groups:
        print(f"\n=== {title} ===")
        for fn in tests:
            try:
                fn()
                passed += 1
            except Exception as e:                       # noqa: BLE001
                failed += 1
                print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()

    print(f"\n{'=' * 46}\n通过 {passed} / 失败 {failed}\n{'=' * 46}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
