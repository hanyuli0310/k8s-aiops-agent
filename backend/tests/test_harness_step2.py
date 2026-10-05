"""第 2 步（P0-2 RunContext+abort / P0-3 重试与自愈）回归测试。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step2.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step2_test.db"
_TMP_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["HARNESS_STRICT"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db                                       # noqa: E402

# ── 数据库隔离闸门 ──
# 这些用例会 DELETE / INSERT。若 DATABASE_URL 没生效而指向线上 RDS，
# 后果是真实数据被删（写 data_collector 测试时真踩过一次）。
# 宁可整个测试跑不起来，也不允许带着错的连接串继续。
from app import config as _cfg                                   # noqa: E402

if not _cfg.DATABASE_URL.startswith("sqlite"):
    raise SystemExit(
        f"❌ 测试中止：数据库未隔离，当前指向 "
        f"{_cfg.DATABASE_URL.split('@')[-1]}。测试必须连本地 SQLite。")
from app.harness import llm as llm_mod                            # noqa: E402
from app.harness import runctx                                    # noqa: E402
from app.harness.context import ContextManager                    # noqa: E402
from app.harness.loop import run_agent                            # noqa: E402
from app.harness.runctx import RunContext                         # noqa: E402


# ── fake LLM 消息对象 ──

class _FakeFn:
    def __init__(self, name, arguments="{}"):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, cid, name="query_metrics"):
        self.id = cid
        self.type = "function"
        self.function = _FakeFn(name)


class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeUsage:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


def _run(chat_impl, run=None, tools=("query_metrics",), event_sink=None,
         execute_impl=None, model=None):
    """stub 掉 llm.chat_with_retry / registry.execute / _summarize 后跑 run_agent。

    execute_impl: 自定义工具执行实现（默认返回 ok）。注意必须由本 helper 统一
        设置，否则调用方自己 stub 会被这里覆盖。
    model: 显式指定 agent 的模型（模型降级用例需要）。
    """
    from app.harness import loop as loop_mod
    from app.tools import registry as reg_mod

    orig = (loop_mod.llm.chat_with_retry, reg_mod.execute, ContextManager._summarize)
    loop_mod.llm.chat_with_retry = chat_impl
    reg_mod.execute = execute_impl or (lambda name, args, run=None: '{"ok":true}')
    ContextManager._summarize = staticmethod(lambda msgs: "[STUB]")
    try:
        agent = {"name": "T", "system_prompt": "SYS", "tools": list(tools)}
        if model:
            agent["model"] = model
        return list(run_agent(agent, "测试任务", run=run, event_sink=event_sink))
    finally:
        loop_mod.llm.chat_with_retry, reg_mod.execute, ContextManager._summarize = orig


# ══════════════════════════════════════════════════════
# P0-2 RunContext + abort
# ══════════════════════════════════════════════════════

def test_should_stop_reasons():
    r = RunContext(session_id="s")
    assert r.should_stop() == (False, "")
    r.abort.set()
    assert r.should_stop()[0] and "中断" in r.should_stop()[1]

    r2 = RunContext(session_id="s", max_tokens=100)
    r2.add_usage(80, 40)                      # 120 > 100
    stop, why = r2.should_stop()
    assert stop and "token 预算" in why

    r3 = RunContext(session_id="s", max_wall_s=0.01)
    time.sleep(0.05)
    stop, why = r3.should_stop()
    assert stop and "时间预算" in why
    print("  ✓ should_stop 三类触发（中断 / token 预算 / 时间预算）均正确")


def test_invalid_mode_falls_back():
    r = RunContext(session_id="s", mode="不存在的模式")
    assert r.mode == runctx.MODE_CONFIRM, f"应回落到 confirm，实际 {r.mode}"
    print(f"  ✓ 非法模式回落到 {r.mode}（fail-safe）")


def test_abort_before_first_step():
    """开跑前已置位 → 第一步就该停，且不应调用 LLM。"""
    r = RunContext(session_id="s")
    r.abort.set()
    calls = {"n": 0}

    def chat(*a, **kw):
        calls["n"] += 1
        return _FakeMsg(content="不应被调用")

    evs = _run(chat, run=r)
    assert calls["n"] == 0, "中断后仍调用了 LLM"
    assert any(e["type"] == "aborted" for e in evs)
    assert not any(e["type"] == "answer" for e in evs)
    print("  ✓ 开跑前中断：零次 LLM 调用，产出 aborted 事件")


def test_abort_mid_tools_fills_pairing():
    """工具执行中被中断 → 剩余 tool_call 必须补齐结果，保持配对不变量。"""
    r = RunContext(session_id="s")
    captured = {}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        captured["messages"] = messages       # 拿到 ctx.messages 的引用
        return _FakeMsg(content="并行查 3 个",
                        tool_calls=[_FakeToolCall(f"c{i}") for i in range(3)])

    n = {"i": 0}

    def exec_then_abort(name, args, run=None):
        n["i"] += 1
        if n["i"] == 1:
            r.abort.set()                     # 第一个工具跑完就中断
        return '{"ok":true}'

    evs = _run(chat, run=r, execute_impl=exec_then_abort)

    assert any(e["type"] == "aborted" for e in evs), \
        f"未产出 aborted 事件：{[e['type'] for e in evs]}"
    assert n["i"] == 1, f"中断后不应继续执行工具，实际执行了 {n['i']} 个"

    # 校验配对：把捕获到的 messages 塞回一个 ctx 做不变量检查
    c = ContextManager("SYS")
    c.messages = captured["messages"]
    c.assert_api_invariants()                 # 不抛异常即通过
    tool_msgs = [m for m in c.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 3, f"3 个 tool_call 应有 3 个结果，实际 {len(tool_msgs)}"
    interrupted = [m for m in tool_msgs if "中断" in str(m.get("content"))]
    assert len(interrupted) == 2, f"应有 2 个被标记为中断，实际 {len(interrupted)}"
    print("  ✓ 工具执行中中断：3 个 tool_call 全部配对（2 个标记未执行），不变量通过")


def test_token_budget_stops_loop():
    """token 预算耗尽应停止循环，而不是跑满 12 步。"""
    r = RunContext(session_id="s", max_tokens=50)
    calls = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        calls["n"] += 1
        if run:
            run.add_usage(30, 30)             # 每次 60 > 50，第二步就该停
        return _FakeMsg(content="继续", tool_calls=[_FakeToolCall(f"c{calls['n']}")])

    evs = _run(chat, run=r)
    ab = [e for e in evs if e["type"] == "aborted"]
    assert ab and "token 预算" in ab[0]["reason"]
    assert calls["n"] < config.MAX_AGENT_STEPS, f"未提前停止，跑了 {calls['n']} 步"
    print(f"  ✓ token 预算触发：{calls['n']} 步后停止（上限 {config.MAX_AGENT_STEPS} 步）")


def test_usage_event_emitted():
    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        if run:
            run.add_usage(100, 50)
        if len([m for m in messages if m.get("role") == "tool"]) >= 1:
            return _FakeMsg(content="完成")
        return _FakeMsg(content="查一下", tool_calls=[_FakeToolCall("c1")])

    r = RunContext(session_id="s")
    evs = _run(chat, run=r)
    usage = [e for e in evs if e["type"] == "usage"]
    assert usage, "未推送 usage 事件"
    u = usage[0]
    assert u["tokens_in"] > 0 and "est_cost_cny" in u and "budget_pct" in u
    print(f"  ✓ usage 事件：in={u['tokens_in']} out={u['tokens_out']} "
          f"¥{u['est_cost_cny']} {u['budget_pct']}%")


def test_registry_and_child():
    r = RunContext(session_id="sess-A", mode=runctx.MODE_CONFIRM)
    runctx.register(r)
    assert runctx.get("sess-A") is r
    assert "sess-A" in runctx.active_sessions()

    child = r.child("risk-1")
    assert child.depth == r.depth + 1
    assert child.mode == runctx.MODE_READONLY, "子 Agent 应默认只读"
    assert child.abort is r.abort, "子 Agent 必须共享中断信号（父断则子断）"
    assert child.max_tokens == config.SUBAGENT_MAX_TOKENS, "子 Agent 应有独立预算"
    r.abort.set()
    assert child.should_stop()[0], "父中断后子未停止"

    runctx.unregister("sess-A")
    assert runctx.get("sess-A") is None
    print(f"  ✓ 注册表 + child()：depth={child.depth} mode={child.mode} "
          f"预算={child.max_tokens} 共享中断=True")


def test_event_sink_receives_all():
    """event_sink 应收到与 yield 相同的全部事件（子 Agent 透传的基础）。"""
    sunk = []

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        return _FakeMsg(content="直接答")

    evs = _run(chat, run=RunContext(session_id="s"), event_sink=sunk.append)
    assert len(sunk) == len(evs), f"sink 收到 {len(sunk)}，yield 了 {len(evs)}"
    assert [e["type"] for e in sunk] == [e["type"] for e in evs]
    print(f"  ✓ event_sink 收到全部 {len(sunk)} 个事件，与 yield 一致")


def test_push_event_never_raises():
    """推送通道异常绝不能影响主流程。"""
    r = RunContext(session_id="s")
    r.event_sink = lambda ev: (_ for _ in ()).throw(RuntimeError("sink 炸了"))
    r.push_event({"type": "test"})            # 不应抛出
    print("  ✓ push_event 吞掉 sink 异常，主流程不受影响")


# ══════════════════════════════════════════════════════
# P0-3 重试与自愈
# ══════════════════════════════════════════════════════

def test_error_classification():
    cases = [
        (Exception("Error code: 429 rate_limit_exceeded"), "retryable"),
        (Exception("HTTP 503 ServiceUnavailable"), "retryable"),
        (Exception("Read timed out"), "retryable"),
        (Exception("Range of input length should be [1, 30000]"), "context"),
        (Exception("maximum context length exceeded"), "context"),
        (Exception("invalid_api_key"), "fatal"),
    ]
    for exc, expect in cases:
        got = llm_mod._classify(exc)
        assert got == expect, f"{exc} → 期望 {expect}，实际 {got}"
    print(f"  ✓ 错误分级正确（{len(cases)} 个样例：retryable / context / fatal）")


def test_quota_exhausted_is_not_retryable():
    """★ 配额耗尽必须与限流区分开 —— 两者都是 429，但重试意义完全相反。

    限流退避几秒能过；配额要等下个计费周期（周/月）重置，重试只是把同一个
    错误再撞一遍。这个区分极易丢失：`"429"` 就在 RETRYABLE_MARKERS 里，
    配额判定只要排在它后面就会被抢走。

    为何值得一条常驻测试：批量评测撞上配额耗尽时后果特别隐蔽 ——
    每次调用都失败 → 每次排障返回空回答 → 判定全记 0 分 →
    汇总照样印出「提升 0 个百分点」这种看起来像结论的假数据。
    实测就这么废掉过一轮 L3 评测。
    """
    quota_cases = [
        "Error code: 429 - {'error': {'message': 'Your token-plan 1-week "
        "quota has been exhausted. The quota will reset at 08-14 09:41:00 UTC.'}}",
        "insufficient_quota: You exceeded your current quota",
        "账号余额不足，请充值",
    ]
    for msg in quota_cases:
        got = llm_mod._classify(Exception(msg))
        assert got == "quota", f"配额错误被归成 {got}：{msg[:60]}"

    # 反方向：普通限流不能被误判成配额（否则一次限流就中止整轮评测）
    for msg in ("Error code: 429 rate_limit_exceeded, please retry after 1s",
                "Throttling.User: request rate too high"):
        got = llm_mod._classify(Exception(msg))
        assert got == "retryable", f"普通限流被归成 {got}：{msg[:60]}"

    # chat_with_retry 必须抛专门的异常且**不重试**
    calls = {"n": 0}

    def _always_quota(**kw):
        calls["n"] += 1
        raise Exception("Error code: 429 - quota has been exhausted")

    orig = llm_mod.get_client
    llm_mod.get_client = lambda: type(
        "C", (), {"chat": type("Ch", (), {"completions": type(
            "Co", (), {"create": staticmethod(_always_quota)})()})()})()
    try:
        raised = None
        try:
            llm_mod.chat_with_retry([{"role": "user", "content": "hi"}])
        except Exception as e:                                    # noqa: BLE001
            raised = e
        assert isinstance(raised, llm_mod.QuotaExhausted), type(raised)
        assert calls["n"] == 1, f"配额耗尽却重试了 {calls['n']} 次"
    finally:
        llm_mod.get_client = orig
    print("  ✓ 配额耗尽独立成类、不重试；普通限流仍走重试")


def test_retry_then_success():
    """瞬时失败两次后成功 —— 整轮不应废掉。"""
    attempts = {"n": 0}

    class _Resp:
        def __init__(self):
            self.choices = [type("C", (), {"message": _FakeMsg(content="成功")})()]
            self.usage = _FakeUsage(10, 5)

    class _FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    attempts["n"] += 1
                    if attempts["n"] < 3:
                        raise Exception("Error code: 429 rate_limit")
                    return _Resp()

    orig = llm_mod.get_client
    llm_mod.get_client = lambda: _FakeClient
    r = RunContext(session_id="s")
    try:
        t0 = time.time()
        msg = llm_mod.chat_with_retry([{"role": "user", "content": "x"}], run=r)
        elapsed = time.time() - t0
    finally:
        llm_mod.get_client = orig

    assert msg.content == "成功"
    assert attempts["n"] == 3, f"应重试到第 3 次，实际 {attempts['n']}"
    assert r.llm_failures == 0, "成功后应重置连续失败计数"
    assert r.tokens_in == 10 and r.tokens_out == 5, "usage 未记账"
    print(f"  ✓ 重试成功：3 次尝试 / 退避 {elapsed:.1f}s / 计数已归零 / usage 已记账")


def test_fatal_not_retried():
    attempts = {"n": 0}

    class _FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    attempts["n"] += 1
                    raise Exception("invalid_api_key: 认证失败")

    orig = llm_mod.get_client
    llm_mod.get_client = lambda: _FakeClient
    try:
        try:
            llm_mod.chat_with_retry([{"role": "user", "content": "x"}])
        except Exception as e:
            assert "invalid_api_key" in str(e)
    finally:
        llm_mod.get_client = orig
    assert attempts["n"] == 1, f"fatal 错误不应重试，实际尝试 {attempts['n']} 次"
    print("  ✓ fatal 错误只尝试 1 次，不做无谓重试")


def test_context_too_long_raises_not_retries():
    attempts = {"n": 0}

    class _FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    attempts["n"] += 1
                    raise Exception("Range of input length should be [1, 30000]")

    orig = llm_mod.get_client
    llm_mod.get_client = lambda: _FakeClient
    try:
        try:
            llm_mod.chat_with_retry([{"role": "user", "content": "x"}])
            raise AssertionError("应抛 ContextTooLong")
        except llm_mod.ContextTooLong:
            pass
    finally:
        llm_mod.get_client = orig
    assert attempts["n"] == 1, "上下文超长不应重试（重试也没用）"
    print("  ✓ 上下文超长抛 ContextTooLong 且不重试，交由调用方压缩")


def test_abort_during_retry_backoff():
    """退避睡眠期间被中断，应尽快抛 Aborted 而不是睡完。"""
    class _FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise Exception("Error code: 503")

    orig = llm_mod.get_client
    llm_mod.get_client = lambda: _FakeClient
    r = RunContext(session_id="s")
    threading.Timer(0.3, r.abort.set).start()
    try:
        t0 = time.time()
        try:
            llm_mod.chat_with_retry([{"role": "user", "content": "x"}],
                                    run=r, max_attempts=5)
            raise AssertionError("应抛 Aborted")
        except llm_mod.Aborted:
            elapsed = time.time() - t0
    finally:
        llm_mod.get_client = orig
    assert elapsed < 3.0, f"中断响应太慢：{elapsed:.1f}s（退避期间未检查中断）"
    print(f"  ✓ 退避期间可中断：{elapsed:.2f}s 内响应（未睡满全部退避）")


def test_self_heal_context_too_long():
    """上下文超长 → 紧急压缩后重试成功。"""
    calls = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm_mod.ContextTooLong("Range of input length")
        return _FakeMsg(content="压缩后成功给出结论")

    r = RunContext(session_id="s")
    evs = _run(chat, run=r)
    types = [e["type"] for e in evs]
    assert "compacted" in types, f"未触发紧急压缩：{types}"
    ans = next(e for e in evs if e["type"] == "answer")
    assert "压缩后成功" in ans["text"]
    assert r.emergency_compacts == 1
    print(f"  ✓ 上下文超长自愈：compacted 事件 + 重试成功（压缩 {r.emergency_compacts} 次）")


def test_emergency_compact_circuit_breaker():
    """★ 熔断：紧急压缩只试一次，第二次超长必须放弃而非无限循环。"""
    calls = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        calls["n"] += 1
        raise llm_mod.ContextTooLong("一直超长")

    r = RunContext(session_id="s")
    evs = _run(chat, run=r)
    err = [e for e in evs if e["type"] == "error"]
    assert err and "紧急压缩后仍无法恢复" in err[0]["text"]
    assert calls["n"] == 2, f"应只尝试 2 次（原始+1次压缩重试），实际 {calls['n']}"
    assert r.emergency_compacts == r.max_emergency_compacts
    print(f"  ✓ 熔断生效：{calls['n']} 次调用后放弃，未陷入无限压缩循环")


def test_model_downgrade():
    """主模型连续失败 → 降级到快模型并继续。

    注意：本用例显式指定两个不同的模型名。项目 .env 目前把 LLM_MODEL 与
    LLM_MODEL_FAST 设成了同一个模型（qwen3.8-max），那种配置下降级逻辑
    是空转的 —— 这里不依赖 .env，以验证代码路径本身正确。
    """
    orig_fast = config.LLM_MODEL_FAST
    config.LLM_MODEL_FAST = "fast-model-for-test"
    seen_models = []

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        seen_models.append(model)
        if model != config.LLM_MODEL_FAST:
            if run:
                run.llm_failures = config.LLM_DOWNGRADE_AFTER   # 模拟已连续失败
            raise Exception("主模型 503 ServiceUnavailable")
        return _FakeMsg(content="降级后成功")

    r = RunContext(session_id="s")
    try:
        evs = _run(chat, run=r, model="main-model-for-test")
    finally:
        config.LLM_MODEL_FAST = orig_fast

    fb = [e for e in evs if e["type"] == "model_fallback"]
    assert fb, f"未降级：{[e['type'] for e in evs]}"
    assert fb[0]["to"] == "fast-model-for-test"
    ans = next(e for e in evs if e["type"] == "answer")
    assert "降级后成功" in ans["text"]
    assert r.model_downgraded is True
    print(f"  ✓ 模型降级：{seen_models[0]} → {seen_models[-1]}，随后成功")


def test_model_downgrade_only_once():
    """降级只发生一次，避免在两个模型间反复切换。"""
    orig_fast = config.LLM_MODEL_FAST
    config.LLM_MODEL_FAST = "fast-model-for-test"

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        if run:
            run.llm_failures = config.LLM_DOWNGRADE_AFTER
        raise Exception("两个模型都 503 ServiceUnavailable")

    r = RunContext(session_id="s")
    try:
        evs = _run(chat, run=r, model="main-model-for-test")
    finally:
        config.LLM_MODEL_FAST = orig_fast

    fb = [e for e in evs if e["type"] == "model_fallback"]
    err = [e for e in evs if e["type"] == "error"]
    assert len(fb) == 1, f"降级发生了 {len(fb)} 次，应只有 1 次"
    assert err, "降级后仍失败时应报错退出"
    print("  ✓ 降级只发生 1 次，之后如实报错（不反复切换）")


def test_downgrade_inert_when_models_identical():
    """★ 配置观察：主/快模型同名时降级必然空转，此时应如实报错而非静默重试。

    项目 .env 当前正是这种配置（LLM_MODEL == LLM_MODEL_FAST == qwen3.8-max），
    本用例把这个行为固化下来，避免误以为「已经有降级保护」。
    """
    orig_fast = config.LLM_MODEL_FAST
    config.LLM_MODEL_FAST = "same-model"
    calls = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        calls["n"] += 1
        if run:
            run.llm_failures = config.LLM_DOWNGRADE_AFTER
        raise Exception("503 ServiceUnavailable")

    r = RunContext(session_id="s")
    try:
        evs = _run(chat, run=r, model="same-model")     # 主模型 == 快模型
    finally:
        config.LLM_MODEL_FAST = orig_fast

    assert not [e for e in evs if e["type"] == "model_fallback"], "同名模型不应触发降级"
    assert [e for e in evs if e["type"] == "error"], "应如实报错"
    assert calls["n"] == 1, f"不应额外重试，实际调用 {calls['n']} 次"
    print("  ✓ 主/快模型同名时：不空转降级，直接如实报错（1 次调用）")


def test_llm_client_has_explicit_timeout_and_no_sdk_retry():
    """★ OpenAI 客户端必须显式设 timeout 且禁止 SDK 层重试。

    为何要守这一条（实测踩过）：
    SDK 默认 timeout=600s、max_retries=2，而外层 chat_with_retry 自己还要重试
    LLM_MAX_ATTEMPTS 次 —— 两层叠加后一次调用最坏阻塞近 1.5 小时。
    而阻塞期间 run.abort 根本检查不到（它只在每次 attempt 开头看一眼），
    "可中断"在这段时间里是失效的：实测出现过评测进程卡在一次调用上
    21 分钟、CPU 0%、日志零输出，从外部完全分不出"在思考"还是"挂了"。
    """
    llm_mod._client = None                     # 清掉可能已缓存的客户端
    orig_key = config.DASHSCOPE_API_KEY
    try:
        config.DASHSCOPE_API_KEY = "sk-test-not-real"
        client = llm_mod.get_client()
    finally:
        config.DASHSCOPE_API_KEY = orig_key
        llm_mod._client = None

    assert config.LLM_TIMEOUT_S > 0, "LLM_TIMEOUT_S 必须是正数"
    assert config.LLM_TIMEOUT_S <= 600, \
        f"LLM_TIMEOUT_S={config.LLM_TIMEOUT_S} 不应大于 SDK 默认的 600s，否则等于没设"
    # SDK 把 timeout 包成 httpx.Timeout，取 read 超时比对
    got = getattr(client, "timeout", None)
    read_to = getattr(got, "read", got)
    assert read_to == config.LLM_TIMEOUT_S, \
        f"客户端 timeout={got}，与 config.LLM_TIMEOUT_S={config.LLM_TIMEOUT_S} 不一致"
    assert client.max_retries == 0, (
        f"SDK 层重试应关掉（当前 {client.max_retries}），"
        f"否则与 chat_with_retry 叠加成乘法级的阻塞时长")
    print(f"  ✓ LLM 客户端 timeout={read_to}s、SDK 重试已关（外层重试 "
          f"{config.LLM_MAX_ATTEMPTS} 次，最坏阻塞 ≤ "
          f"{read_to * config.LLM_MAX_ATTEMPTS:.0f}s）")


def test_aborted_not_saved_to_memory():
    """中断的半截结论不应写入会话记忆。"""
    from app.harness import scheduler
    db.execute("DELETE FROM chat_messages")
    r = RunContext(session_id="sess-abort")
    r.abort.set()
    evs = list(scheduler.handle_message("sess-abort", "执行风险扫描", run=r))
    types = [e["type"] for e in evs]
    rows = db.fetch_all(
        "SELECT role FROM chat_messages WHERE session_id='sess-abort'")
    roles = [x["role"] for x in rows]
    assert "user" in roles, "用户消息应正常落库"
    assert "assistant" not in roles, f"中断后不应写 assistant 记忆，实际 {roles}"
    print(f"  ✓ 中断不写 assistant 记忆（事件={types[:3]}... 落库角色={roles}）")


# ══════════════════════════════════════════════════════

def main():
    db.init_db()
    groups = [
        ("P0-2 · RunContext 与中断", [
            test_should_stop_reasons,
            test_invalid_mode_falls_back,
            test_abort_before_first_step,
            test_abort_mid_tools_fills_pairing,
            test_token_budget_stops_loop,
            test_usage_event_emitted,
            test_registry_and_child,
            test_event_sink_receives_all,
            test_push_event_never_raises,
        ]),
        ("P0-3 · 重试与自愈", [
            test_error_classification,
        test_quota_exhausted_is_not_retryable,
            test_retry_then_success,
            test_fatal_not_retried,
            test_context_too_long_raises_not_retries,
            test_abort_during_retry_backoff,
            test_self_heal_context_too_long,
            test_emergency_compact_circuit_breaker,
            test_model_downgrade,
            test_model_downgrade_only_once,
            test_downgrade_inert_when_models_identical,
            test_llm_client_has_explicit_timeout_and_no_sdk_retry,
            test_aborted_not_saved_to_memory,
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
