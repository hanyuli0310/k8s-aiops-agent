"""第 5 步（P1-2 dispatch_agent 子 Agent / P1-3 分模型路由）回归测试。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step5.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step5_test.db"
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
from app.agents.base import AGENT_SPECS, build_agent             # noqa: E402
from app.harness import llm                                      # noqa: E402
from app.harness.context import ContextManager                   # noqa: E402
from app.harness.runctx import RunContext                        # noqa: E402
from app.tools import registry                                   # noqa: E402
from app.tools.agent_tools import SUBAGENT_TYPES                 # noqa: E402


class _FakeFn:
    def __init__(self, name, arguments="{}"):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, cid, name, arguments="{}"):
        self.id = cid
        self.type = "function"
        self.function = _FakeFn(name, arguments)


class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _Stub:
    """把 LLM 与 registry.execute 一起打桩，防止真实网络/DB 调用。

    ★ 必须同时挡住 llm.chat 与 llm.chat_with_retry，并把 get_client 换成
      抛异常的防线 —— 只挡内部函数名的话，实现一改就会静默打真实接口烧 token。
    """

    def __init__(self, chat_impl, execute_impl=None):
        self.chat_impl = chat_impl
        self.execute_impl = execute_impl

    def __enter__(self):
        from app.harness import loop as loop_mod
        self._orig = (loop_mod.llm.chat_with_retry, llm.chat, llm.get_client,
                      registry.execute, ContextManager._summarize)
        loop_mod.llm.chat_with_retry = self.chat_impl
        llm.chat = self.chat_impl

        def _boom(*a, **kw):
            raise AssertionError("测试期间不应创建真实 LLM 客户端")

        llm.get_client = _boom
        if self.execute_impl:
            registry.execute = self.execute_impl
        ContextManager._summarize = staticmethod(lambda m: "[STUB]")
        return self

    def __exit__(self, *exc):
        from app.harness import loop as loop_mod
        (loop_mod.llm.chat_with_retry, llm.chat, llm.get_client,
         registry.execute, ContextManager._summarize) = self._orig
        return False


def _dispatch(args: dict, run=None) -> dict:
    """直接调用真实的 dispatch_agent（不经 registry），返回解析后的结果。"""
    from app.tools.agent_tools import dispatch_agent
    out = dispatch_agent(_run=run, **args)
    return out if isinstance(out, dict) else json.loads(out)


# ══════════════════════════════════════════════════════
# P1-2 子 Agent 的机制性约束
# ══════════════════════════════════════════════════════

def test_subagent_tools_filtered_to_readonly():
    """★ 子 Agent 的只读性靠【代码过滤】保证，不靠提示词。

    risk 类型本身带 5 个治理工具（patch_deployment 等），派成子 Agent 后
    必须一个都不剩 —— 否则子 Agent 能绕过主流程的确认直接改集群。
    """
    registry.ensure_loaded()
    seen = {}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        seen["tools"] = [t["function"]["name"] for t in (tools or [])]
        return _FakeMsg(content="结论：无风险")

    with _Stub(chat):
        _dispatch({"subagent_type": "risk", "task": "扫描风险"})

    raw = build_agent("risk")["tools"]
    writers = [t for t in raw if not registry.get_spec(t).is_read_only]
    assert writers, "前提变了：risk Agent 没有写类工具了"
    leaked = [t for t in seen["tools"] if not registry.get_spec(t).is_read_only]
    assert not leaked, f"子 Agent 拿到了写类工具：{leaked}"
    print(f"  ✓ risk 子 Agent 摘除全部 {len(writers)} 个写类工具，"
          f"只留 {len(seen['tools'])} 个只读工具")


def test_subagent_cannot_spawn_subagent():
    """★ 防繁殖：子 Agent 的工具集里不能有 dispatch_agent。"""
    registry.ensure_loaded()
    seen = {}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        seen["tools"] = [t["function"]["name"] for t in (tools or [])]
        return _FakeMsg(content="done")

    with _Stub(chat):
        _dispatch({"subagent_type": "diagnose", "task": "定位"})

    assert "dispatch_agent" in build_agent("diagnose")["tools"], "前提变了"
    assert "dispatch_agent" not in seen["tools"], "子 Agent 还能再派子 Agent"
    print("  ✓ 子 Agent 工具集里没有 dispatch_agent（防无限繁殖）")


def test_depth_guard_blocks_deep_nesting():
    """深度护栏：达到上限时拒绝派发，而不是继续嵌套。"""
    registry.ensure_loaded()
    deep = RunContext(session_id="s", mode="readonly",
                      depth=config.SUBAGENT_MAX_DEPTH)
    out = _dispatch({"subagent_type": "risk", "task": "x"}, run=deep)
    assert "error" in out and "上限" in out["error"], out
    print(f"  ✓ depth={config.SUBAGENT_MAX_DEPTH} 时拒绝派发（第二道护栏）")


def test_invalid_subagent_type_rejected():
    out = _dispatch({"subagent_type": "hacker", "task": "x"})
    assert "error" in out and "不支持" in out["error"], out
    print(f"  ✓ 非法类型被拒（仅允许 {SUBAGENT_TYPES}）")


def test_only_conclusion_returned():
    """★ 核心价值：中间数据不进主上下文，只回传结论。"""
    registry.ensure_loaded()
    big = json.dumps({"logs": [{"msg": "x" * 200} for _ in range(200)]})
    step = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        step["n"] += 1
        if step["n"] == 1:
            return _FakeMsg(content="查日志",
                            tool_calls=[_FakeToolCall("c1", "query_logs")])
        return _FakeMsg(content="结论：order-service 有 200 条超时日志，P99 达 3200ms")

    with _Stub(chat, execute_impl=lambda n, a, run=None: big):
        out = _dispatch({"subagent_type": "diagnose", "task": "查日志"})

    assert "结论" in out["conclusion"]
    assert out["tool_calls"] == 1
    blob = json.dumps(out, ensure_ascii=False)
    assert len(blob) < len(big) / 5, \
        f"回传体量过大（{len(blob)}），中间数据泄漏到主上下文了"
    print(f"  ✓ 子 Agent 内部 {len(big)} 字符中间数据，只回传 {len(blob)} 字符结论")


def test_subagent_has_independent_cap_but_charges_parent():
    """子 Agent 的【上限】独立，但【消耗】要向上归集。

    ⚠️ 契约变更（E-5 实测后修正）：本用例原来断言 `parent.tokens_in == 0`，
    即"父完全不计子的用量"。那个语义有洞 ——
      · RUN_MAX_TOKENS=120000 号称整轮硬上限，但主 Agent 并行派 3 个子 Agent
        （每个上限 40000）就能额外烧掉 120000，父的计数器还停在自己那点；
      · 前端用量条会严重低估真实消耗（真机实测：父自身 52233，
        子 Agent 合计 126016，不归集就只显示 52233 / 预算 13%）。
    正确语义是把两件事分开：**上限独立（防单个子任务跑飞），消耗归集（保整轮封顶）**。
    """
    registry.ensure_loaded()
    parent = RunContext(session_id="p", mode="confirm", max_tokens=100_000)
    captured = {}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        if run is not None:
            captured["max_tokens"] = run.max_tokens
            captured["depth"] = run.depth
            run.add_usage(500, 500)
        return _FakeMsg(content="ok")

    with _Stub(chat):
        _dispatch({"subagent_type": "risk", "task": "x"}, run=parent)

    assert captured["max_tokens"] == config.SUBAGENT_MAX_TOKENS, captured
    assert captured["depth"] == parent.depth + 1, captured
    # 子用了 500+500，必须体现在父身上
    assert parent.tokens_in == 500 and parent.tokens_out == 500, \
        f"子 Agent 的用量没有归集到父：in={parent.tokens_in} out={parent.tokens_out}"
    print(f"  ✓ 子上限 {captured['max_tokens']} tokens、depth={captured['depth']}，"
          f"消耗已归集到父（in={parent.tokens_in} out={parent.tokens_out}）")


def test_subagent_runs_readonly_mode():
    """子 run 必须是 readonly 模式（即使父是 confirm/auto）。"""
    registry.ensure_loaded()
    for parent_mode in ("confirm", "auto"):
        parent = RunContext(session_id="p", mode=parent_mode)
        got = {}

        def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
            got["mode"] = run.mode if run else None
            return _FakeMsg(content="ok")

        with _Stub(chat):
            _dispatch({"subagent_type": "risk", "task": "x"}, run=parent)
        assert got["mode"] == "readonly", f"父 {parent_mode} → 子 {got['mode']}"
    print("  ✓ 父为 confirm/auto 时子 run 仍为 readonly")


# ══════════════════════════════════════════════════════
# P1-2 中断与事件透传（评审决策 D4）
# ══════════════════════════════════════════════════════

def test_parent_abort_stops_subagent():
    """★ 父被停时子必须同时停 —— 否则子 Agent 在后台继续烧 token。"""
    registry.ensure_loaded()
    parent = RunContext(session_id="p", mode="confirm")
    steps = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        steps["n"] += 1
        if steps["n"] == 1:
            parent.abort.set()               # 父在子跑第一步时被停
        return _FakeMsg(content="继续",
                        tool_calls=[_FakeToolCall(f"c{steps['n']}", "query_logs")])

    with _Stub(chat, execute_impl=lambda n, a, run=None: '{"ok":1}'):
        out = _dispatch({"subagent_type": "diagnose", "task": "x"}, run=parent)

    assert steps["n"] <= 2, f"父已中断，子仍跑了 {steps['n']} 步"
    assert "aborted" in out["conclusion"] or "中断" in out["conclusion"], out
    print(f"  ✓ 父中断 → 子 {steps['n']} 步内停止（共享 abort 信号）")


def test_subagent_events_forwarded_with_attribution():
    """★ D4：子 Agent 事件要冒到前端，且带归属标记可区分层级。"""
    registry.ensure_loaded()
    sunk = []
    parent = RunContext(session_id="p", mode="confirm", event_sink=sunk.append)
    step = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        step["n"] += 1
        if step["n"] == 1:
            return _FakeMsg(content="我先查日志",
                            tool_calls=[_FakeToolCall("c1", "query_logs")])
        return _FakeMsg(content="结论：已定位")

    with _Stub(chat, execute_impl=lambda n, a, run=None: '{"ok":1}'):
        _dispatch({"subagent_type": "diagnose", "task": "查因",
                   "description": "日志分析"}, run=parent)

    types = [e["type"] for e in sunk]
    assert "subagent_start" in types and "subagent_done" in types, types
    assert "tool_call" in types, f"子 Agent 的工具调用未透传：{types}"

    forwarded = [e for e in sunk if e["type"] in ("thinking", "tool_call", "tool_result")]
    assert forwarded, "无透传事件"
    for e in forwarded:
        assert e.get("subagent") == "DiagnoseAgent", f"缺归属标记：{e}"
        assert e.get("depth") == 1, f"层级标记错误：{e}"
        assert e.get("description") == "日志分析", f"缺 UI 标签：{e}"

    done = next(e for e in sunk if e["type"] == "subagent_done")
    assert done["tool_calls"] == 1 and "tokens" in done, done
    print(f"  ✓ 透传 {len(sunk)} 条事件，全部带 subagent/depth/description 标记")


def test_no_parent_run_still_works():
    """离线脚本/直接调用（无父 run）时不能崩，退化为独立 run。"""
    registry.ensure_loaded()

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        return _FakeMsg(content="独立跑通")

    with _Stub(chat):
        out = _dispatch({"subagent_type": "topology", "task": "x"}, run=None)
    assert out["conclusion"] == "独立跑通", out
    print("  ✓ 无父 run 时退化为独立 run，不崩")


def test_dispatch_via_registry_gets_run_injected():
    """★ needs_run 通道：经 registry.execute 调用时 run 必须被注入。

    这是事件透传能工作的前提 —— 拿不到父 run 就既无法共享中断也无法冒事件。
    """
    registry.ensure_loaded()
    sunk = []
    parent = RunContext(session_id="p", mode="confirm", event_sink=sunk.append)

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        return _FakeMsg(content="ok")

    from app.harness import loop as loop_mod
    orig = (loop_mod.llm.chat_with_retry, llm.chat, ContextManager._summarize)
    loop_mod.llm.chat_with_retry = chat
    llm.chat = chat
    ContextManager._summarize = staticmethod(lambda m: "[STUB]")
    try:
        out = json.loads(registry.execute(
            "dispatch_agent",
            {"subagent_type": "risk", "task": "x", "description": "风险"},
            run=parent))
    finally:
        (loop_mod.llm.chat_with_retry, llm.chat,
         ContextManager._summarize) = orig

    assert out["conclusion"] == "ok", out
    assert any(e["type"] == "subagent_start" for e in sunk), \
        "经 registry 调用时 run 未注入，事件没冒上来"
    print("  ✓ registry.execute(run=...) 正确注入 _run，事件正常透传")


def test_other_tools_unaffected_by_run_injection():
    """run 注入只对 needs_run 工具生效，不能污染其他工具的签名。"""
    registry.ensure_loaded()
    others = [t for t in registry.describe_tools() if not t["needs_run"]]
    assert len(others) >= 15, f"needs_run 标记泛滥：只有 {len(others)} 个未标"
    out = json.loads(registry.execute("list_risk_rules", {}, run=RunContext(session_id="s")))
    assert "error" not in out or "_run" not in str(out), out
    print(f"  ✓ {len(others)} 个普通工具签名未受影响")


def test_parallel_dispatch_possible():
    """dispatch_agent 标了 concurrency_safe，多个子 Agent 应能并批（配合 P1-1）。"""
    registry.ensure_loaded()
    from app.harness.loop import _partition
    spec = registry.get_spec("dispatch_agent")
    assert spec.is_read_only and spec.concurrency_safe, "标记不对，无法并行派发"
    batches = _partition([_FakeToolCall(f"d{i}", "dispatch_agent") for i in range(3)])
    assert len(batches) == 1 and len(batches[0]) == 3, \
        f"多个 dispatch 未合批：{[len(b) for b in batches]}"
    print("  ✓ 3 个 dispatch_agent 合成 1 批，可并行派发")


def test_concurrent_dispatch_isolated():
    """并发派发多个子 Agent 时，各自的 run 与结论不能串味。"""
    registry.ensure_loaded()
    sunk, lock = [], threading.Lock()
    parent = RunContext(session_id="p", mode="confirm",
                        event_sink=lambda e: (lock.acquire(), sunk.append(e),
                                              lock.release()) and None)

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        time.sleep(0.1)
        return _FakeMsg(content=f"结论-{run.session_id.rsplit('/', 1)[-1]}")

    results = {}

    def go(t):
        with _Stub(chat):
            results[t] = _dispatch({"subagent_type": t, "task": "x"}, run=parent)

    with _Stub(chat):
        threads = [threading.Thread(target=go, args=(t,)) for t in SUBAGENT_TYPES]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

    for t in SUBAGENT_TYPES:
        assert f"sub-{t}" in results[t]["conclusion"], \
            f"{t} 的结论串味了：{results[t]['conclusion']}"
    print(f"  ✓ {len(SUBAGENT_TYPES)} 个子 Agent 并发派发，结论互不串味")


# ══════════════════════════════════════════════════════
# P1-3 分模型路由 + token 估算
# ══════════════════════════════════════════════════════

def test_model_routing_per_agent():
    """★ 轻结构化任务走快模型，决策/推理走主模型。"""
    expect = {
        "data": config.LLM_MODEL_FAST,
        "topology": config.LLM_MODEL_FAST,
        "risk": config.LLM_MODEL,          # 治理不可逆，需强推理
        "diagnose": config.LLM_MODEL,      # 根因推理
        "general": config.LLM_MODEL,
    }
    bad = []
    for k, want in expect.items():
        got = build_agent(k)["model"]
        if got != want:
            bad.append(f"{k}: 期望 {want} 实际 {got}")
    assert not bad, "; ".join(bad)
    assert config.LLM_MODEL != config.LLM_MODEL_FAST, \
        "两个模型配成同一个，分模型路由等于空转（.env 需修正）"
    print(f"  ✓ 路由正确：data/topology→{config.LLM_MODEL_FAST}，"
          f"risk/diagnose/general→{config.LLM_MODEL}")


def test_agent_model_actually_used_by_loop():
    """路由不能只写在 spec 里 —— 必须真的传给 LLM 调用。"""
    registry.ensure_loaded()
    from app.harness.loop import run_agent
    seen = []

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        seen.append(model)
        return _FakeMsg(content="ok")

    with _Stub(chat):
        agent = build_agent("topology")
        list(run_agent(agent, "梳理拓扑", run=RunContext(session_id="s")))

    assert seen and seen[0] == config.LLM_MODEL_FAST, \
        f"loop 未使用 agent 的 model：{seen}"
    print(f"  ✓ TopologyAgent 实际调用模型 = {seen[0]}")


def test_all_specs_have_model():
    """每个 Agent 都要显式指定 model，避免新增时漏配默默走贵模型。"""
    missing = [k for k, v in AGENT_SPECS.items() if not v.get("model")]
    assert not missing, f"未指定 model 的 Agent：{missing}"
    print(f"  ✓ {len(AGENT_SPECS)} 个 Agent 全部显式配置 model")


def test_est_tokens_counts_tool_call_args():
    """token 估算必须计入 tool_calls 的 arguments，且中文密度高于英文。

    ⚠️ 契约变更（依据：真机实测校准）：原先断言"中文按 ~1 token/字"，
    是拍脑袋的系数。拿本项目文档与工具 JSON 对照 qwen tokenizer 实测后：
        中文实测 ≈0.50 token/字符、ASCII/JSON 实测 ≈0.42 token/字符，
    两者只差约 1.2 倍，远不到旧公式假设的 4 倍。
    校准后系数为 CJK 0.75 / 其余 0.5（各留余量），故此处改判 1.4 倍。
    真正要守住的不变量是"计入 arguments"与"中文更贵"，这两条没变。
    """
    c = ContextManager("SYS")
    base = c.est_tokens()
    c.messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "sql_query",
                                     "arguments": json.dumps({"sql": "x" * 400})}}],
    })
    assert c.est_tokens() > base + 50, \
        f"tool_calls 未计入估算（{base} → {c.est_tokens()}）"

    cn = ContextManager("SYS")
    cn.messages.append({"role": "user", "content": "服务" * 100})     # 200 汉字
    en = ContextManager("SYS")
    en.messages.append({"role": "user", "content": "a" * 200})
    assert cn.est_tokens() > en.est_tokens() * 1.4, \
        f"中文密度未高于英文：中文 {cn.est_tokens()} vs 英文 {en.est_tokens()}"
    print(f"  ✓ 计入 tool_calls；200 汉字≈{cn.est_tokens()} tokens，"
          f"200 字母≈{en.est_tokens()} tokens")


def test_est_tokens_is_conservative():
    """估算应偏保守（宁高勿低）—— 低估会撞上限，整轮白费。

    ⚠️ 契约变更（依据：真机实测校准）：原断言是 `est_tokens() > 1000`（1000 汉字），
    即拿【字符数】当 token 基准。这个基准本身站不住 ——
    实测 1000 汉字真实只有约 500 token，要求估算超过 1000 等于强制 2 倍虚高，
    代价是明明还能装的内容被提前压缩掉。
    现在改成对照实测比率：估算要落在 (真实, 真实×2) 之间 —— 保守但不虚高。
    """
    real_per_cjk = 0.5                      # 实测：中文约 0.50 token/字符
    c = ContextManager("")
    c.messages = [{"role": "user", "content": "运维" * 500}]          # 1000 汉字
    est, real = c.est_tokens(), int(1000 * real_per_cjk)
    assert est > real, f"低估了：{est} <= 实测量级 {real}"
    assert est < real * 2, f"虚高过头：{est} >= {real}×2，会导致过早压缩"
    print(f"  ✓ 1000 汉字估为 {est} tokens（实测量级 {real}，"
          f"保守 {est / real:.2f}x）")


def main():
    db.init_db()
    registry.ensure_loaded()
    groups = [
        ("P1-2 · 子 Agent 机制约束", [
            test_subagent_tools_filtered_to_readonly,
            test_subagent_cannot_spawn_subagent,
            test_depth_guard_blocks_deep_nesting,
            test_invalid_subagent_type_rejected,
            test_only_conclusion_returned,
            test_subagent_has_independent_cap_but_charges_parent,
            test_subagent_runs_readonly_mode,
        ]),
        ("P1-2 · 中断与事件透传（D4）", [
            test_parent_abort_stops_subagent,
            test_subagent_events_forwarded_with_attribution,
            test_no_parent_run_still_works,
            test_dispatch_via_registry_gets_run_injected,
            test_other_tools_unaffected_by_run_injection,
            test_parallel_dispatch_possible,
            test_concurrent_dispatch_isolated,
        ]),
        ("P1-3 · 分模型路由与 token 估算", [
            test_model_routing_per_agent,
            test_agent_model_actually_used_by_loop,
            test_all_specs_have_model,
            test_est_tokens_counts_tool_call_args,
            test_est_tokens_is_conservative,
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
                import traceback
                traceback.print_exc()
    print("\n" + "=" * 46)
    print(f"通过 {passed} / 失败 {failed}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
