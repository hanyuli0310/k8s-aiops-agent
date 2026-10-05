"""第 9 步（E-1 CI / E-2 Plan 与分段续跑）回归测试。

E-2 的核心风险不是"续不上"，而是**续跑失去硬上限**。所以本文件的重点是
把三道闸门逐条钉死：预算优先、次数上限、必须有结构化的未完成信号。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step9.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step9_test.db"
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

from app.agents import base as agents_base                       # noqa: E402
from app.harness import llm, loop                                # noqa: E402
from app.harness.context import ContextManager                   # noqa: E402
from app.harness.runctx import RunContext                        # noqa: E402
from app.tools import registry                                   # noqa: E402


# ══════════════════════════════════════════════════════
# 测试替身
# ══════════════════════════════════════════════════════

class _FakeFn:
    def __init__(self, name, args="{}"):
        self.name = name
        self.arguments = args


class _FakeToolCall:
    def __init__(self, cid, name, args="{}"):
        self.id = cid
        self.type = "function"
        self.function = _FakeFn(name, args)


class _FakeMsg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


def _agent(tools=None):
    return {"name": "TestAgent", "system_prompt": "sys", "model": "m",
            "tools": tools or ["query_metrics", "update_plan"]}


def _run_loop(chat_fn, run, tools=None, exec_fn=None):
    """用 stub 跑一遍 loop，返回事件列表。

    stub 打在 llm.chat_with_retry 与 registry.execute 两处 I/O 边界上，
    并给 llm.get_client 装一道 AssertionError 防线 —— 只挡内部函数名的 mock
    会随实现演进静默失效，失效方式是真的去调线上接口烧 token。
    """
    orig_chat, orig_exec, orig_client = (
        llm.chat_with_retry, registry.execute, llm.get_client)

    def guard(*a, **kw):
        raise AssertionError("测试不应触达真实 LLM 客户端")

    llm.chat_with_retry = chat_fn
    llm.get_client = guard
    registry.execute = exec_fn or (
        lambda name, args, run=None: json.dumps({"ok": name}))
    try:
        return list(loop.run_agent(_agent(tools), "任务", run=run))
    finally:
        llm.chat_with_retry, registry.execute, llm.get_client = (
            orig_chat, orig_exec, orig_client)


def _always_tool_chat(plan_after=None, handoff_text="交接：已确认 P99=1.244s，下一步查慢日志"):
    """永远调工具的 stub，必然把步数耗尽。

    plan_after: 传入则在第 1 次调用后把该 plan 写进 run（模拟模型调了 update_plan）。
    """
    state = {"n": 0, "handoffs": 0, "run": None}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        state["n"] += 1
        state["run"] = run
        if tools is None:                       # 交接摘要 / 收口调用
            state["handoffs"] += 1
            return _FakeMsg(content=handoff_text)
        if plan_after is not None and state["n"] == 1 and run is not None:
            run.set_plan(plan_after)
        return _FakeMsg(content=f"第{state['n']}步",
                        tool_calls=[_FakeToolCall(f"c{state['n']}", "query_metrics")])

    return chat, state


# ══════════════════════════════════════════════════════
# E-2 · update_plan 工具
# ══════════════════════════════════════════════════════

def test_update_plan_normalizes_and_reports():
    """★ 清单来自模型输出，必须做归一化：非法 status 归 pending、空 title 丢弃。"""
    registry.ensure_loaded()
    run = RunContext(session_id="p1")
    out = json.loads(registry.execute("update_plan", {"steps": [
        {"title": "圈定病灶接口", "status": "done"},
        {"title": "  ", "status": "done"},                 # 空 title → 丢弃
        {"title": "抓一条问题链路", "status": "IN_PROGRESS"},  # 大写 → 归一
        {"title": "多源收口", "status": "不存在的状态"},        # 非法 → pending
        "这不是 dict",                                      # 类型错误 → 丢弃
    ]}, run=run))
    assert out["total"] == 3, out
    assert out["done"] == 1, out
    assert [s["status"] for s in out["plan"]] == ["done", "in_progress", "pending"], out
    print(f"  ✓ 5 条输入归一为 {out['total']} 步（done {out['done']}），非法值已兜住")


def test_update_plan_without_run_reports_error():
    """★ 没有 run 时必须明确报错，不能静默丢弃。

    静默丢弃的后果：模型以为计划登记成功，而续跑判断永远拿不到它。
    """
    registry.ensure_loaded()
    out = json.loads(registry.execute("update_plan", {"steps": [
        {"title": "x", "status": "pending"}]}))          # 不传 run
    assert "error" in out, out
    print("  ✓ 无运行上下文时明确报错")


def test_update_plan_is_readonly_but_not_parallel():
    """★ 安全属性：只读（对被管系统零副作用）但不可并行（写共享状态）。"""
    registry.ensure_loaded()
    spec = registry.get_spec("update_plan")
    assert spec.is_read_only, "应标只读，否则 readonly 模式下列不了计划"
    assert not spec.concurrency_safe, "不能可并行：并行批里改共享 plan 会竞态"
    assert spec.needs_run, "必须 needs_run 才能拿到 RunContext"
    # 不可并行 → 它不会和只读查询合成同一批
    calls = [_FakeToolCall("a", "query_metrics"), _FakeToolCall("b", "update_plan"),
             _FakeToolCall("c", "query_metrics")]
    batches = loop._partition(calls)
    assert [len(b) for b in batches] == [1, 1, 1], \
        f"update_plan 被并入并行批：{[[c.function.name for c in b] for b in batches]}"
    print("  ✓ 只读 + 不可并行，且确实不会被并入并行批")


def test_plan_digest_and_unfinished():
    run = RunContext(session_id="p2")
    run.set_plan([{"title": "a", "status": "done"},
                  {"title": "b", "status": "in_progress"},
                  {"title": "c", "status": "pending"}])
    assert len(run.unfinished_steps()) == 2
    digest = run.plan_digest()
    assert digest.splitlines() == ["[x] a", "[~] b", "[ ] c"], digest
    print("  ✓ plan_digest / unfinished_steps 正确")


# ══════════════════════════════════════════════════════
# E-2 · 续跑的三道闸门
# ══════════════════════════════════════════════════════

def test_no_plan_means_no_continuation():
    """★ 闸门 3：没有任务清单就不自动续跑（不去猜任务完成没完成）。"""
    run = RunContext(session_id="c1")
    chat, state = _always_tool_chat(plan_after=None)
    events = _run_loop(chat, run)
    assert not any(e["type"] == "continued" for e in events)
    assert run.continuations == 0
    # MAX_AGENT_STEPS 次带工具 + 1 次收口
    assert state["n"] == config.MAX_AGENT_STEPS + 1, state
    print(f"  ✓ 无 plan → 不续跑（{state['n']} 次 LLM 调用后收口）")


def test_unfinished_plan_triggers_continuation():
    """★★ 有未完成步骤时自动续跑，且段数受 max_continuations 约束。"""
    run = RunContext(session_id="c2", max_continuations=2)
    chat, state = _always_tool_chat(plan_after=[
        {"title": "第一步", "status": "done"},
        {"title": "第二步", "status": "in_progress"},
        {"title": "第三步", "status": "pending"},
    ])
    events = _run_loop(chat, run)
    cont = [e for e in events if e["type"] == "continued"]
    assert len(cont) == 2, f"应续跑 2 次，实际 {len(cont)}"
    assert [e["segment"] for e in cont] == [2, 3], cont
    assert run.continuations == 2
    # 3 段 × 12 步 + 2 次交接 + 1 次收口
    expect = 3 * config.MAX_AGENT_STEPS + 3
    assert state["n"] == expect, f"LLM 调用 {state['n']}，预期 {expect}"
    answer = next(e for e in events if e["type"] == "answer")["text"]
    assert "3 段" in answer, answer[:160]
    assert "尚余 2 项未完成" in answer, answer[:160]
    print(f"  ✓ 续跑 2 次共 3 段（{state['n']} 次 LLM 调用），收口文案说清了段数与剩余项")


def test_budget_beats_continuation():
    """★★ 闸门 1：超预算时即使清单未完成也不续跑 —— 预算优先于续跑。

    这是整个 E-2 最关键的一条：续跑只重置步数，绝不重置预算。
    若顺序反了，加了续跑就等于把 token 硬上限废掉。
    """
    run = RunContext(session_id="c3", max_tokens=100, max_continuations=5)
    run.set_plan([{"title": "没做完", "status": "pending"}])
    # 必须真的把用量打上去 —— should_stop 比的是累计用量，光把上限设小不会触发
    run.add_usage(prompt_tokens=200)
    ok, why = run.can_continue()
    assert not ok and "token 预算" in why, why

    # 走完整 loop：第一步就该因超预算 aborted，连一次续跑都不会发生
    run2 = RunContext(session_id="c3b", max_tokens=100, max_continuations=5)
    run2.set_plan([{"title": "没做完", "status": "pending"}])
    run2.add_usage(prompt_tokens=200)
    chat, _ = _always_tool_chat(plan_after=None)
    events = _run_loop(chat, run2)
    assert any(e["type"] == "aborted" for e in events), [e["type"] for e in events]
    assert run2.continuations == 0
    print(f"  ✓ 超预算优先于续跑（{why}）")


def test_abort_beats_continuation():
    """★ 用户中断时不续跑（同样属于闸门 1）。"""
    run = RunContext(session_id="c4", max_continuations=5)
    run.set_plan([{"title": "没做完", "status": "pending"}])
    run.abort.set()
    ok, why = run.can_continue()
    assert not ok and "用户中断" in why, why
    print("  ✓ 已中断 → 不续跑")


def test_all_done_plan_means_no_continuation():
    """清单全部 done 时不续跑（否则会为已完成的任务白烧一段）。"""
    run = RunContext(session_id="c5")
    run.set_plan([{"title": "a", "status": "done"}, {"title": "b", "status": "done"}])
    ok, why = run.can_continue()
    assert not ok and "已全部完成" in why, why
    print("  ✓ 清单全完成 → 不续跑")


def test_continuation_limit_respected():
    """闸门 2：续跑次数达上限后不再续。"""
    run = RunContext(session_id="c6", max_continuations=1)
    run.set_plan([{"title": "没做完", "status": "pending"}])
    run.continuations = 1
    ok, why = run.can_continue()
    assert not ok and "续跑上限" in why, why
    print("  ✓ 达续跑上限 → 不续跑")


def test_subagent_cannot_continue():
    """★ 子 Agent 禁止续跑：它只做一个自包含子任务，续跑会让预算失控。"""
    parent = RunContext(session_id="root")
    child = parent.child("sub-diagnose")
    assert child.max_continuations == 0, child.max_continuations
    assert child.plan == [], "子 run 不应继承父的任务清单"
    parent.set_plan([{"title": "父的步骤", "status": "pending"}])
    child2 = parent.child("sub-2")
    assert child2.plan == [], "派生后仍不应继承 plan"
    ok, _ = child2.can_continue()
    assert not ok
    print("  ✓ 子 Agent 不继承 plan 且 max_continuations=0")


# ══════════════════════════════════════════════════════
# E-2 · 交接摘要与上下文重置
# ══════════════════════════════════════════════════════

def test_continuation_shrinks_context():
    """★ 续跑必须把上下文压回去，否则分段只是把爆上下文的时间推后。"""
    ctx = ContextManager("system prompt", session_id="h1")
    ctx.scratchpad["keep"] = "这条要留下"
    for i in range(20):
        ctx.add_user(f"user {i} " + "x" * 500)
    before = ctx.total_chars()
    freed = ctx.reset_for_continuation("交接摘要：已确认 P99=1.244s")
    assert freed > 0 and ctx.total_chars() < before / 5, (before, ctx.total_chars())
    assert len(ctx.messages) == 2 and ctx.messages[0]["role"] == "system"
    assert "交接摘要" in ctx.messages[1]["content"]
    assert ctx.scratchpad["keep"] == "这条要留下", "scratchpad 不该被清掉"
    print(f"  ✓ 上下文 {before} → {ctx.total_chars()} 字符，system 与 scratchpad 保留")


def test_handoff_carries_plan_snapshot():
    """★ 交接单必须带上清单快照 —— 不依赖模型有没有在摘要里写全。"""
    run = RunContext(session_id="h2")
    run.set_plan([{"title": "已完成的一步", "status": "done"},
                  {"title": "待办的一步", "status": "pending"}])
    ctx = ContextManager("sys", session_id="h2")
    ctx.add_user("任务")

    captured = {}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        captured["prompt"] = messages[-1]["content"]
        return _FakeMsg(content="已确认 rows_examined=1162951")

    orig = llm.chat_with_retry
    llm.chat_with_retry = chat
    try:
        text = loop._handoff(ctx, run, "m")
    finally:
        llm.chat_with_retry = orig

    assert "1162951" in text
    assert "[任务清单快照]" in text and "待办的一步" in text, text
    # 交接指令本身也要把清单给模型看
    assert "当前任务清单" in captured["prompt"] and "[x] 已完成的一步" in captured["prompt"]
    print("  ✓ 交接单含清单快照，且交接指令里已注入清单")


def test_handoff_failure_aborts_continuation():
    """★ 交接摘要生成失败时放弃续跑，而不是带着空交接单从零重开一段。"""
    run = RunContext(session_id="h3", max_continuations=2)
    state = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        state["n"] += 1
        if tools is None:
            if state.get("wrapup_done"):
                return _FakeMsg(content="阶段性结论：仅确认了 P99")
            state["wrapup_done"] = True
            raise RuntimeError("模拟交接摘要生成失败")
        if state["n"] == 1 and run is not None:
            run.set_plan([{"title": "没做完", "status": "pending"}])
        return _FakeMsg(content=f"第{state['n']}步",
                        tool_calls=[_FakeToolCall(f"c{state['n']}", "query_metrics")])

    events = _run_loop(chat, run)
    assert not any(e["type"] == "continued" for e in events), "交接失败却续跑了"
    assert run.continuations == 1, "计数已加但续跑被放弃，属于预期"
    assert any(e["type"] == "answer" for e in events)
    print("  ✓ 交接失败 → 放弃续跑并正常收口")


def test_plan_update_event_on_main_channel():
    """★★ plan_update 必须走【主通道】yield，不能只靠工具内部的旁路推送。

    第一版把它写在 update_plan 工具里用 run.push_event 推，真机验证时
    4 次 update_plan 调用产出 0 个事件 —— 因为离线脚本没注入 event_sink，
    旁路直接 return。前端能收到只是因为 HTTP 路径恰好注入了。
    这个用例刻意【不设 event_sink】，逼出主通道行为。
    """
    run = RunContext(session_id="p3")             # 注意：无 event_sink
    assert run.event_sink is None

    state = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        state["n"] += 1
        if tools is None or state["n"] > 2:
            return _FakeMsg(content="做完了")
        return _FakeMsg(content="列计划",
                        tool_calls=[_FakeToolCall(f"c{state['n']}", "update_plan",
                                                 json.dumps({"steps": [
                                                     {"title": f"第{state['n']}步",
                                                      "status": "done"}]}))])

    def real_exec(name, args, run=None):
        # 用真实 registry 执行 update_plan，才能验证它确实写进了 run.plan
        return _orig_exec(name, args, run=run)

    _orig_exec = registry.execute
    events = _run_loop(chat, run, tools=["update_plan"], exec_fn=real_exec)
    plan_evs = [e for e in events if e["type"] == "plan_update"]
    assert len(plan_evs) == 2, f"应有 2 次 plan_update，实际 {len(plan_evs)}"
    assert plan_evs[0]["plan"][0]["title"] == "第1步", plan_evs[0]
    print(f"  ✓ 无 event_sink 时仍从主通道收到 {len(plan_evs)} 个 plan_update")


def test_plan_update_only_on_change():
    """清单没变就不该重复推事件（否则时间线会被噪声淹掉）。"""
    run = RunContext(session_id="p4")
    state = {"n": 0}
    same = json.dumps({"steps": [{"title": "同一份清单", "status": "pending"}]})

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        state["n"] += 1
        if tools is None or state["n"] > 3:
            return _FakeMsg(content="结束")
        return _FakeMsg(content="重复提交同一份清单",
                        tool_calls=[_FakeToolCall(f"c{state['n']}", "update_plan", same)])

    _orig_exec = registry.execute
    events = _run_loop(chat, run, tools=["update_plan"],
                       exec_fn=lambda n, a, run=None: _orig_exec(n, a, run=run))
    plan_evs = [e for e in events if e["type"] == "plan_update"]
    assert len(plan_evs) == 1, f"重复提交同一份清单只该推 1 次，实际 {len(plan_evs)}"
    print("  ✓ 清单未变化时不重复推送")


def test_continued_event_shape():
    """★ continued 事件要带够信息：第几段、为什么续、释放了多少、当前清单。"""
    run = RunContext(session_id="c7", max_continuations=1)
    chat, _ = _always_tool_chat(plan_after=[
        {"title": "做完了", "status": "done"},
        {"title": "还没做", "status": "pending"}])
    events = _run_loop(chat, run)
    ev = next(e for e in events if e["type"] == "continued")
    assert ev["segment"] == 2
    assert "1 个未完成步骤" in ev["reason"], ev
    assert ev["freed_chars"] > 0, ev
    assert [s["title"] for s in ev["plan"]] == ["做完了", "还没做"], ev
    assert "第 2 段" in ev["text"] and "上限 2 段" in ev["text"], ev["text"]
    print(f"  ✓ continued 事件完整：{ev['text']}")


# ══════════════════════════════════════════════════════
# E-2 · Agent 装配
# ══════════════════════════════════════════════════════

def test_long_task_agents_have_update_plan():
    """★ 长任务 Agent 必须有 update_plan，否则续跑机制对它们等于不存在。"""
    registry.ensure_loaded()
    for key in ("general", "risk", "diagnose"):
        tools = agents_base.build_agent(key)["tools"]
        assert "update_plan" in tools, f"{key} 缺 update_plan"
    # 简单 Agent 刻意不给：列计划要花 token，单步任务不值当
    for key in ("data", "topology"):
        assert "update_plan" not in agents_base.build_agent(key)["tools"], \
            f"{key} 不该有 update_plan"
    print("  ✓ general/risk/diagnose 有 update_plan，data/topology 刻意没有")


def test_prompt_teaches_update_plan():
    """★ 机制做对但模型不用等于白做 —— 提示词里必须讲清它的作用。"""
    p = agents_base.build_agent("general")["system_prompt"]
    assert "update_plan" in p, "提示词没提 update_plan"
    assert "任务清单" in p
    # 必须说明"为什么要用"，而不只是"有这个工具"
    assert "步数" in p and "自动开新一段" in p, "没讲清它与续跑的关系"
    print("  ✓ 系统提示词已交代 update_plan 与续跑的关系")


# ══════════════════════════════════════════════════════
# E-1 · CI 执行体
# ══════════════════════════════════════════════════════

def test_run_tests_script_exists_and_lints():
    """★ CI 执行体必须存在且语法正确（bash -n）。"""
    script = _ROOT.parent / "scripts" / "run_tests.sh"
    assert script.is_file(), f"缺少 {script}"
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"语法错误：{r.stderr}"
    print("  ✓ scripts/run_tests.sh 存在且语法正确")


def test_no_bare_var_before_cjk_in_scripts():
    """★★ 紧邻中文的 $VAR 必须带花括号，否则 bash 把非 ASCII 字节当变量名。

    这个坑在本项目踩过三次（start_all.sh 的 $FRONTEND_HOST、
    run_tests.sh 的 $base 与 $code），且症状是 "unbound variable" 这种
    与真实原因毫无关联的报错，排查成本很高，所以做成常驻检查。
    """
    import re
    pat = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7F])")
    bad = []
    for sh in sorted((_ROOT.parent / "scripts").glob("*.sh")):
        for i, line in enumerate(sh.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):        # 注释不会被求值
                continue
            m = pat.search(line)
            if m:
                bad.append(f"{sh.name}:{i} ${m.group(1)} 紧邻非 ASCII 字符")
    assert not bad, "\n  " + "\n  ".join(bad) + "\n  改成 ${VAR} 即可"
    print(f"  ✓ {len(list((_ROOT.parent / 'scripts').glob('*.sh')))} 个脚本无裸变量紧邻中文")


def test_isolation_gate_covers_all_db_tests():
    """★ 所有会连库的测试文件都必须带隔离闸门（与 run_tests.sh 的自检同口径）。

    在 Python 侧也查一遍：CI 脚本可能被跳过，但这个用例一定会跑。
    """
    import re
    need, missing = [], []
    for d in ("backend/tests", "mock_server/tests", "data_collector/tests"):
        for f in sorted((_ROOT.parent / d).glob("test_*.py")):
            text = f.read_text(encoding="utf-8")
            if not re.search(r"^from (app|collector) import .*\bdb\b", text, re.M):
                continue
            need.append(f.name)
            if "sqlite:///" not in text or 'startswith("sqlite")' not in text:
                missing.append(f.name)
    assert need, "一个涉库测试都没找到，检查逻辑坏了"
    assert not missing, f"缺隔离闸门：{missing}"
    print(f"  ✓ {len(need)} 个涉库测试全部带隔离闸门")


def test_gitlab_ci_config_valid_yaml():
    """★ .gitlab-ci.yml 必须是合法 YAML 且调用 run_tests.sh。

    注意：本用例只保证配置写对了，**不代表 CI 已经跑通** ——
    runner 可用性无法在本地验证，文件头部已标注"未经 runner 验证"。
    """
    import yaml
    p = _ROOT.parent / ".gitlab-ci.yml"
    assert p.is_file(), "缺少 .gitlab-ci.yml"
    conf = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "test" in conf, conf.keys()
    script = " ".join(conf["test"]["script"])
    assert "scripts/run_tests.sh" in script, script
    # 兜底：CI 环境变量必须把库钉在 sqlite
    for key in ("DATABASE_URL", "DB_URL"):
        assert conf["variables"][key].startswith("sqlite"), conf["variables"]
    assert "未经 runner 验证" in p.read_text(encoding="utf-8"), \
        "跑通前必须保留未验证标注，避免误认为 CI 已生效"
    print("  ✓ .gitlab-ci.yml 合法且调用 run_tests.sh（仍标注未验证）")


def main():
    db.init_db()
    registry.ensure_loaded()
    groups = [
        ("E-2 · update_plan 工具", [
            test_update_plan_normalizes_and_reports,
            test_update_plan_without_run_reports_error,
            test_update_plan_is_readonly_but_not_parallel,
            test_plan_digest_and_unfinished,
            test_plan_update_event_on_main_channel,
            test_plan_update_only_on_change,
        ]),
        ("E-2 · 续跑的三道闸门", [
            test_no_plan_means_no_continuation,
            test_unfinished_plan_triggers_continuation,
            test_budget_beats_continuation,
            test_abort_beats_continuation,
            test_all_done_plan_means_no_continuation,
            test_continuation_limit_respected,
            test_subagent_cannot_continue,
        ]),
        ("E-2 · 交接摘要与上下文", [
            test_continuation_shrinks_context,
            test_handoff_carries_plan_snapshot,
            test_handoff_failure_aborts_continuation,
            test_continued_event_shape,
        ]),
        ("E-2 · Agent 装配", [
            test_long_task_agents_have_update_plan,
            test_prompt_teaches_update_plan,
        ]),
        ("E-1 · CI 执行体", [
            test_run_tests_script_exists_and_lints,
            test_no_bare_var_before_cjk_in_scripts,
            test_isolation_gate_covers_all_db_tests,
            test_gitlab_ci_config_valid_yaml,
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
