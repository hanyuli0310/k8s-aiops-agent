"""第 11 步（E-5 并行子 Agent 实测）回归测试。

`dispatch_agent` 早就标了 `is_read_only=True, concurrency_safe=True`，
`_partition()` 也会把连续的多个 dispatch_agent 合成一批并发跑 ——
**但在此之前没有任何用例证明它真的并行**。属于"以为有、实际没验证"的一类风险，
本文件把它变成事实。

用例刻意走**完整真实路径**：主 Agent 发两个 dispatch_agent → _partition 合批 →
ThreadPoolExecutor → dispatch_agent → build_agent → run_agent（子 loop）。
只把最外层的 `llm.chat_with_retry` 换成受控替身（子 Agent 那次故意 sleep），
不去 stub `registry.execute` —— 那样就绕过了被测对象本身。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step11.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step11_test.db"
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

from app.harness import llm, loop                                # noqa: E402
from app.harness.runctx import RunContext                        # noqa: E402
from app.tools import agent_tools, registry                      # noqa: E402

# 子 Agent 里模拟的单步耗时。取值要同时满足：
#   · 足够大，让"串行 vs 并行"的差距远超线程调度抖动；
#   · 足够小，别把测试拖长。
SLEEP_S = 0.6

MAIN_PROMPT = "__MAIN_AGENT__"


class _FakeFn:
    def __init__(self, name, args="{}"):
        self.name, self.arguments = name, args


class _FakeToolCall:
    def __init__(self, cid, name, args="{}"):
        self.id, self.type, self.function = cid, "function", _FakeFn(name, args)


class _FakeMsg:
    def __init__(self, content="", tool_calls=None):
        self.content, self.tool_calls = content, tool_calls or []


def _dispatch_call(cid, subagent_type, desc):
    return _FakeToolCall(cid, "dispatch_agent", json.dumps({
        "subagent_type": subagent_type,
        "task": f"{desc}：请给出结论",
        "description": desc,
    }))


class _Harness:
    """受控替身：主 Agent 派两个子 Agent，子 Agent 各 sleep 后返回结论。

    通过 system prompt 区分主/子 —— 主 Agent 的提示词由本测试指定，
    子 Agent 的由 build_agent 真实生成。
    """

    def __init__(self, subagents=("capacity", "dbops"), sub_tool=None):
        self.subagents = subagents
        self.sub_tool = sub_tool          # 子 Agent 是否先调一个工具再作答
        self.main_turns = 0
        self.lock = threading.Lock()
        self.sub_calls = 0
        self.concurrent_peak = 0
        self._inflight = 0
        self.sub_prompts = []

    def chat(self, messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        system = messages[0]["content"]
        if system == MAIN_PROMPT:
            self.main_turns += 1
            if self.main_turns == 1:
                return _FakeMsg(content="并行派两个专家", tool_calls=[
                    _dispatch_call(f"d{i}", t, f"任务{i}")
                    for i, t in enumerate(self.subagents)])
            return _FakeMsg(content="已汇总两个子 Agent 的结论")

        # ── 子 Agent ──
        with self.lock:
            self.sub_calls += 1
            self._inflight += 1
            self.concurrent_peak = max(self.concurrent_peak, self._inflight)
            self.sub_prompts.append(system[:40])
        try:
            time.sleep(SLEEP_S)
            # 让子 Agent 的用量可观测：每次调用记 1000+500 token
            if run is not None:
                run.add_usage(prompt_tokens=1000, completion_tokens=500)
            if self.sub_tool and len([p for p in self.sub_prompts]) and tools:
                # 只在第一次带工具的调用里发起工具调用，第二次直接作答
                if not getattr(run, "_did_tool", False):
                    run._did_tool = True
                    return _FakeMsg(content="先查数据", tool_calls=[
                        _FakeToolCall("st1", self.sub_tool)])
            return _FakeMsg(content="子 Agent 结论：已确认")
        finally:
            with self.lock:
                self._inflight -= 1


def _run_main(h: _Harness, run: RunContext, tools=None):
    """跑主 Agent 一轮，返回【合并后】的事件列表。registry.execute 保持真实。

    ★ 必须合并两个来源：
      · generator yield 的     —— 主 Agent 自己的事件
      · run.event_sink 旁路的  —— 子 Agent 的 subagent_start/done 与透传事件

    子 Agent 事件为什么只能走旁路：它们产生在 ThreadPoolExecutor 的 **worker 线程**里，
    而主 generator 阻塞在 `ex.map()` 上，跨线程没法 yield。
    这与 E-4 学到的"能 yield 就走主通道"不矛盾 —— 这里恰恰是不能 yield 的场景。
    代价是：任何不注入 event_sink 的调用方（离线脚本、本测试的第一版）
    完全看不到子 Agent 的动静。
    """
    orig = (llm.chat_with_retry, llm.get_client)
    sideband: list = []
    run.event_sink = sideband.append

    def guard(*a, **kw):
        raise AssertionError("测试不应触达真实 LLM 客户端")

    llm.chat_with_retry = h.chat
    llm.get_client = guard
    agent = {"name": "MainAgent", "system_prompt": MAIN_PROMPT, "model": "m",
             "tools": tools or ["dispatch_agent"]}
    try:
        main_events = list(loop.run_agent(agent, "并行派发", run=run))
    finally:
        llm.chat_with_retry, llm.get_client = orig
    return main_events + sideband


# ══════════════════════════════════════════════════════
# 1. 真的并行
# ══════════════════════════════════════════════════════

def test_dispatch_agent_is_batched_for_parallel():
    """★ 前置条件：连续的多个 dispatch_agent 必须被 _partition 合成一批。"""
    registry.ensure_loaded()
    calls = [_dispatch_call("d0", "capacity", "a"), _dispatch_call("d1", "dbops", "b")]
    batches = loop._partition(calls)
    assert len(batches) == 1 and len(batches[0]) == 2, \
        [[c.function.name for c in b] for b in batches]
    spec = registry.get_spec("dispatch_agent")
    assert spec.is_read_only and spec.concurrency_safe
    print("  ✓ 两个 dispatch_agent 合成一批（只读 + 可并行）")


def test_two_subagents_run_concurrently():
    """★★ 两个子 Agent 各 sleep SLEEP_S，总耗时必须接近 1 份而不是 2 份。

    这是 E-5 的核心断言 —— 在此之前"并行子 Agent"只是标记齐全，从未被证明。
    """
    registry.ensure_loaded()
    h = _Harness()
    run = RunContext(session_id="par1", mode="readonly")
    t0 = time.perf_counter()
    events = _run_main(h, run)
    elapsed = time.perf_counter() - t0

    assert h.sub_calls == 2, h.sub_calls
    assert h.concurrent_peak == 2, f"峰值并发只有 {h.concurrent_peak}，说明是串行跑的"
    assert elapsed < SLEEP_S * 1.8, \
        f"耗时 {elapsed:.2f}s 接近串行的 {SLEEP_S * 2:.2f}s"
    dones = [e for e in events if e["type"] == "subagent_done"]
    assert len(dones) == 2, [e["type"] for e in events]
    print(f"  ✓ 2 个子 Agent 并发执行：峰值并发 {h.concurrent_peak}，"
          f"总耗时 {elapsed:.2f}s（串行需 {SLEEP_S * 2:.1f}s）")


def test_parallel_respects_worker_cap():
    """并发度受 MAX_PARALLEL_TOOLS 约束，不会无限开线程。"""
    registry.ensure_loaded()
    n = config.MAX_PARALLEL_TOOLS + 2
    h = _Harness(subagents=tuple(["capacity"] * n))
    run = RunContext(session_id="par2", mode="readonly")
    _run_main(h, run)
    assert h.sub_calls == n, h.sub_calls
    assert h.concurrent_peak <= config.MAX_PARALLEL_TOOLS, \
        f"峰值并发 {h.concurrent_peak} 超过上限 {config.MAX_PARALLEL_TOOLS}"
    assert h.concurrent_peak > 1, "完全没有并行"
    print(f"  ✓ {n} 个子 Agent 的峰值并发被压在 {h.concurrent_peak} "
          f"(<= MAX_PARALLEL_TOOLS={config.MAX_PARALLEL_TOOLS})")


# ══════════════════════════════════════════════════════
# 2. 并发下的隔离与归属
# ══════════════════════════════════════════════════════

def test_each_subagent_gets_its_own_prompt():
    """★ 两个子 Agent 必须各自拿到自己的系统提示词（不能串成同一个）。"""
    registry.ensure_loaded()
    h = _Harness(subagents=("capacity", "dbops"))
    run = RunContext(session_id="iso1", mode="readonly")
    events = _run_main(h, run)
    names = {e["agent"] for e in events if e["type"] == "subagent_start"}
    assert names == {"CapacityAgent", "DBOpsAgent"}, names
    assert len(set(h.sub_prompts)) == 2, h.sub_prompts
    print(f"  ✓ 两个子 Agent 提示词互不相同，归属标记为 {sorted(names)}")


def test_forwarded_events_carry_owner_tag():
    """★ 并发下透传上来的事件必须带正确的 subagent 归属，否则前端时间线会串。"""
    registry.ensure_loaded()
    h = _Harness(subagents=("capacity", "dbops"), sub_tool="get_risk_report")
    run = RunContext(session_id="iso2", mode="readonly")
    events = _run_main(h, run)
    tagged = [e for e in events if e.get("subagent")]
    assert tagged, "没有任何带 subagent 标记的透传事件"
    owners = {e["subagent"] for e in tagged}
    assert owners == {"CapacityAgent", "DBOpsAgent"}, owners
    # 每条透传事件的 description 必须与它的 subagent 对应，不能错配
    pairs = {(e["subagent"], e.get("description")) for e in tagged}
    assert len(pairs) == 2, pairs
    print(f"  ✓ {len(tagged)} 条透传事件归属正确（{len(pairs)} 组 subagent/description 配对）")


def test_subagent_audit_records_not_lost():
    """★ 并发下两个子 Agent 的工具调用都要落审计 —— 审计是合规凭证，不允许丢。

    注意子 loop 的写审计【不走】父的 _Deferred（那只覆盖父自己的写），
    所以这里确实是并发写库。SQLite 上如果发生锁竞争，这条会红。
    """
    registry.ensure_loaded()
    db.execute("DELETE FROM agent_audit")
    h = _Harness(subagents=("capacity", "dbops"), sub_tool="get_risk_report")
    run = RunContext(session_id="audit1", mode="readonly")
    _run_main(h, run)
    rows = db.fetch_all(
        "SELECT agent_name, tool_name FROM agent_audit WHERE tool_name='get_risk_report'")
    agents = {r["agent_name"] for r in rows}
    assert len(rows) == 2, f"应有 2 条子 Agent 审计，实得 {len(rows)}：{rows}"
    assert agents == {"CapacityAgent", "DBOpsAgent"}, agents
    # 父自己的 dispatch_agent 调用也要各记一条
    parent = db.fetch_all(
        "SELECT COUNT(*) AS c FROM agent_audit WHERE tool_name='dispatch_agent'")[0]["c"]
    assert parent == 2, parent
    print(f"  ✓ 并发下审计无丢失：子 Agent {len(rows)} 条 + 父派发 {parent} 条")


# ══════════════════════════════════════════════════════
# 3. 预算归集（本步抓出的真缺陷）
# ══════════════════════════════════════════════════════

def test_subagent_tokens_charged_to_parent():
    """★★ 子 Agent 烧的 token 必须计入父 run 的预算。

    这是 E-5 实测抓出的真缺陷：`child()` 给的是【独立预算】，
    而独立不等于免费 —— 修之前子 Agent 的消耗完全不计入父，
    主 Agent 并行派 N 个子 Agent 就能绕过 RUN_MAX_TOKENS 这条整轮硬上限
    （每个子 Agent 上限 SUBAGENT_MAX_TOKENS=40000，派 3 个就是 120000）。

    正确语义：**子的上限独立（防单个跑飞），但消耗要向上归集（保整轮封顶）。**
    """
    registry.ensure_loaded()
    h = _Harness()
    run = RunContext(session_id="budget1", mode="readonly")
    _run_main(h, run)
    # 两个子 Agent 各记 1000+500，合计 3000 必须体现在父身上
    assert run.tokens_in >= 2000, f"父 run 未计入子 Agent 的 prompt tokens：{run.tokens_in}"
    assert run.tokens_out >= 1000, f"父 run 未计入子 Agent 的 completion tokens：{run.tokens_out}"
    print(f"  ✓ 子 Agent 用量已归集到父：in={run.tokens_in} out={run.tokens_out}")


def test_parent_budget_can_be_exhausted_by_subagents():
    """★★ 归集之后，并行子 Agent 真的会把父预算烧穿并触发中断。

    这条才是归集的意义所在 —— 否则"整轮硬上限"只是一句话。
    """
    registry.ensure_loaded()
    h = _Harness(subagents=("capacity", "dbops"))
    # 父预算刚好容不下两个子 Agent 的 3000
    run = RunContext(session_id="budget2", mode="readonly", max_tokens=2500)
    events = _run_main(h, run)
    assert any(e["type"] == "aborted" for e in events), [e["type"] for e in events]
    aborted = next(e for e in events if e["type"] == "aborted")
    assert "token 预算" in aborted["reason"], aborted
    print(f"  ✓ 子 Agent 把父预算烧穿后整轮中断：{aborted['reason']}")


class _capped_subagent:
    """临时给子 Agent 恢复预算上限。

    生产默认已把 SUBAGENT_MAX_TOKENS 设为 0（不限制），目标是让 Agent 跑到
    任务完成。但"超预算时如何收场"这条机制必须继续被测到 ——
    所以用例自己把上限设回一个小值来构造场景，而不是依赖生产默认值。
    """

    def __init__(self, max_tokens: int = 3000, max_wall_s: float = 60):
        self.want = (max_tokens, max_wall_s)

    def __enter__(self):
        self.orig = (config.SUBAGENT_MAX_TOKENS, config.SUBAGENT_MAX_WALL_S)
        config.SUBAGENT_MAX_TOKENS, config.SUBAGENT_MAX_WALL_S = self.want
        return self

    def __exit__(self, *exc):
        config.SUBAGENT_MAX_TOKENS, config.SUBAGENT_MAX_WALL_S = self.orig
        return False


def test_subagent_still_has_independent_cap():
    """★ 子 Agent 的预算取自【子的配置】，不继承父的额度，计数也从 0 起。

    注意：生产默认已把子预算设为 0（不限制），所以这里显式设一个上限来验证
    "机制仍然成立"，而不是断言某个具体默认值 —— 默认值是策略，机制才是契约。
    """
    with _capped_subagent(max_tokens=40000, max_wall_s=120):
        parent = RunContext(session_id="budget3", max_tokens=999999)
        child = parent.child("sub-x")
        assert child.max_tokens == 40000, child.max_tokens
        assert child.max_wall_s == 120
        # 子的计数从 0 起，不继承父已用的量（否则一开始就可能判超）
        parent.add_usage(prompt_tokens=50000)
        child2 = parent.child("sub-y")
        assert child2.tokens_in == 0 and child2.tokens_out == 0
    print("  ✓ 子 Agent 用子的上限（40000/120s）而非父的额度，且计数从 0 起")


def test_zero_budget_means_unlimited():
    """★★ 预算 0 = 不限制，但用户中断【永远有效】。

    这是"让 Agent 跑到任务完成"的实现方式：停用预算熔断，而不是把上限调到
    一个很大的数字。0 这个取值必须能穿过 child() 而不被 `or` 吞掉。
    """
    run = RunContext(session_id="unlimited", max_tokens=0, max_wall_s=0)
    run.add_usage(prompt_tokens=10_000_000, completion_tokens=10_000_000)
    stop, why = run.should_stop()
    assert not stop, f"预算已停用却仍然停止：{why}"
    assert not run.budget_enabled()
    # 用量照常累计（花费统计不受影响）
    assert run.usage_event()["tokens_in"] == 10_000_000
    assert run.usage_event()["budget_pct"] is None, "无上限时不该给出百分比"
    assert run.usage_event()["est_cost_cny"] > 0, "花费统计必须继续工作"
    # 人工开关仍然有效
    run.abort.set()
    stop, why = run.should_stop()
    assert stop and why == "用户中断", (stop, why)

    # 0 必须能穿过 child()：`max_tokens or config.X` 那种写法会把 0 吞掉
    child = RunContext(session_id="p", max_tokens=999).child("c", max_tokens=0)
    assert child.max_tokens == 0, f"显式的 0 被吞掉了：{child.max_tokens}"
    print("  ✓ 2000 万 tokens 不触发停止、花费仍在统计、中断仍可用、0 能穿过 child()")


def test_aborted_subagent_returns_partial_conclusion():
    """★★ 子 Agent 超预算/被中断时要把【已积累的信息】带回来，不能空手而归。

    这是遗留缺口的第一条：原来 aborted 路径直接 return，父只拿到一句
    「[子 Agent aborted] 超出 token 预算」，什么都用不上，于是倾向于原样重派
    （真机白烧过 40435 token）。

    收口刻意**不调 LLM** —— 触发原因往往就是预算耗尽，再花一次往返自相矛盾。
    last_thinking 是已经产生并付过费的信息，缺陷在于把它丢了。
    """
    registry.ensure_loaded()

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        # 子 Agent 第一步给出有价值的分析，同时把自己的预算烧穿；
        # 下一步开头的 should_stop 就会命中，走中断收口。
        if run is not None and run.max_tokens > 0:
            run.add_usage(prompt_tokens=run.max_tokens + 1)
        return _FakeMsg(content="已确认 sum_pod_cpu_limit_m=39000，节点可分配 23400m",
                        tool_calls=[_FakeToolCall("s1", "get_risk_report")])

    orig = (llm.chat_with_retry, llm.get_client)
    llm.chat_with_retry = chat
    llm.get_client = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no"))
    try:
        with _capped_subagent(max_tokens=3000):     # 生产默认不限制，用例自设上限
            out = json.loads(registry.execute(
                "dispatch_agent",
                {"subagent_type": "capacity", "task": "核算超卖率",
                 "description": "核算"},
                run=RunContext(session_id="partial1", mode="readonly")))
    finally:
        llm.chat_with_retry, llm.get_client = orig

    assert out["status"] == "aborted", out
    conclusion = out["conclusion"]
    # 核心：中断前算出来的数值必须随结论回到父手里
    assert "39000" in conclusion and "23400" in conclusion, conclusion
    assert "不是完整结论" in conclusion, conclusion
    assert not conclusion.startswith("[子 Agent"), \
        f"仍然是空手而归的占位文案：{conclusion[:80]}"
    assert out["retry"] is False           # 与"劝阻重试"配合：有料可用 + 别重派
    print(f"  ✓ aborted 子 Agent 回传了含 39000/23400 的部分结论"
          f"（{len(conclusion)} 字符），且标注为不完整")


def test_partial_conclusion_costs_no_llm_call():
    """★ 中断收口必须零 LLM 调用（预算已耗尽时再花一次往返自相矛盾）。"""
    from app.harness.context import ContextManager
    ctx = ContextManager("sys", session_id="partial2")
    ctx.note("oversale_pct", 166.67)
    # _NoLLM 式防线：任何 LLM 调用都会抛错
    orig = llm.chat_with_retry
    llm.chat_with_retry = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("中断收口不应调用 LLM"))
    try:
        evs = list(loop._abort_events("超出 token 预算", "已确认 P99=1.244s", ctx))
    finally:
        llm.chat_with_retry = orig
    assert [e["type"] for e in evs] == ["answer", "aborted"], evs
    assert evs[0]["partial"] is True
    assert "1.244" in evs[0]["text"] and "166.67" in evs[0]["text"], evs[0]["text"]
    print("  ✓ 零 LLM 调用，且 answer 在 aborted 之前（否则会被'已中断'盖掉）")


def test_no_partial_answer_when_nothing_accumulated():
    """★ 一无所获时不产出空 answer —— 否则会盖掉"已中断"这个唯一有用的信息。"""
    from app.harness.context import ContextManager
    ctx = ContextManager("sys", session_id="partial3")
    evs = list(loop._abort_events("用户中断", "", ctx))
    assert [e["type"] for e in evs] == ["aborted"], evs
    print("  ✓ 无可交付内容时只发 aborted")


def test_failed_subagent_tells_parent_not_to_retry():
    """★ 子 Agent 非 ok 收场时，返回值必须明确劝阻重试。

    真机实测过这个浪费：dbops 子 Agent 因目标表为空反复试探、烧穿 40000 token
    被中断，父看到 aborted 后**原样重派了一次**，又烧掉 40435。
    同样的输入不会有不同的结果，而子的消耗已归集到父 —— 重试等于双倍消耗总预算。
    """
    registry.ensure_loaded()
    run = RunContext(session_id="retry1", mode="readonly")
    # 让子 Agent 一进去就被中断，必然 aborted
    run.abort.set()
    out = json.loads(registry.execute("dispatch_agent", {
        "subagent_type": "capacity", "task": "任务", "description": "x"}, run=run))
    assert out["status"] != "ok", out
    assert out.get("retry") is False, out
    assert "不要用相同任务再派一次" in out.get("hint", ""), out
    print(f"  ✓ 非 ok 收场时返回劝阻重试的提示（status={out['status']}）")


def test_successful_subagent_has_no_retry_hint():
    """成功时不该塞这些字段 —— 只在需要时才占模型的注意力。"""
    registry.ensure_loaded()
    h = _Harness(subagents=("capacity",))
    run = RunContext(session_id="retry2", mode="readonly")
    events = _run_main(h, run)
    done = next(e for e in events if e["type"] == "subagent_done")
    assert done["status"] == "ok", done
    results = [e for e in events
               if e["type"] == "tool_result" and e.get("tool") == "dispatch_agent"]
    assert results, "没有 dispatch_agent 的结果事件"
    payload = results[0]["result"]
    assert isinstance(payload, dict) and "retry" not in payload, payload
    print("  ✓ 成功时不带 retry/hint 字段")


def test_empty_result_guidance_in_prompt():
    """★ "空结果本身就是结论"必须写进系统提示词。

    这是真机实测的直接产物：DBOpsAgent 面对空的 slow_logs 反复换写法试探，
    几十次 sql_query 后烧穿预算却什么也没查到。机制层面拦不住这种行为，
    只能在提示词里把"0 行是有效信息"讲清楚。
    """
    from app.agents import base as agents_base
    p = agents_base.build_agent("dbops")["system_prompt"]
    assert "空结果本身就是结论" in p, "通用准则缺失"
    assert "不要反复换写法试探" in p
    # dbops 还要有针对性的那条（slow_logs 稳态为空）
    assert "slow_logs" in p and "稳态" in p, "dbops 缺针对性约束"
    print("  ✓ 通用准则 + dbops 针对性约束均已注入提示词")


def test_abort_propagates_to_parallel_subagents():
    """★ 父被中断时正在并发跑的子 Agent 要一起停（abort 共享），整轮以 aborted 收场。

    这里刻意**不断言"全部子 Agent 都非 ok"**：中断是协作式的，检查点在 loop
    每步开头，信号 set 的那一瞬间已经进入执行的那一步不会被打断 ——
    所以"一个 aborted、一个恰好跑完"是合法结果（实测就出现过 ['aborted', 'ok']）。
    把它写成必须全部 aborted 会得到一个 flaky 用例，而 flaky 测试是负资产。
    真正要守住的不变量是：**中断确实传到了子 Agent，且整轮没有继续往下跑。**
    """
    registry.ensure_loaded()
    # 让子 Agent 走两步（先调工具再作答），第二步开头必然命中中断检查点
    h = _Harness(subagents=("capacity", "dbops"), sub_tool="get_risk_report")
    run = RunContext(session_id="abort1", mode="readonly")

    orig_chat = h.chat

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        if messages[0]["content"] != MAIN_PROMPT and run is not None:
            run.abort.set()               # 模拟用户在子 Agent 跑起来后按「停止」
        return orig_chat(messages, tools=tools, model=model,
                         temperature=temperature, run=run, **kw)

    h.chat = chat
    events = _run_main(h, run)
    dones = [e for e in events if e["type"] == "subagent_done"]
    assert dones, "没有 subagent_done 事件"
    assert any(d["status"] != "ok" for d in dones), [d["status"] for d in dones]
    assert any(e["type"] == "aborted" for e in events), \
        "父 run 没有以 aborted 收场"
    print(f"  ✓ 中断传播到并发子 Agent（状态 {[d['status'] for d in dones]}），"
          f"整轮已中止")


def main():
    db.init_db()
    registry.ensure_loaded()
    groups = [
        ("真的并行", [
            test_dispatch_agent_is_batched_for_parallel,
            test_two_subagents_run_concurrently,
            test_parallel_respects_worker_cap,
        ]),
        ("并发下的隔离与归属", [
            test_each_subagent_gets_its_own_prompt,
            test_forwarded_events_carry_owner_tag,
            test_subagent_audit_records_not_lost,
        ]),
        ("预算归集", [
            test_subagent_tokens_charged_to_parent,
            test_parent_budget_can_be_exhausted_by_subagents,
            test_subagent_still_has_independent_cap,
            test_zero_budget_means_unlimited,
            test_abort_propagates_to_parallel_subagents,
        ]),
        ("真机实测催出的健壮性修复", [
            test_aborted_subagent_returns_partial_conclusion,
            test_partial_conclusion_costs_no_llm_call,
            test_no_partial_answer_when_nothing_accumulated,
            test_failed_subagent_tells_parent_not_to_retry,
            test_successful_subagent_has_no_retry_hint,
            test_empty_result_guidance_in_prompt,
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
