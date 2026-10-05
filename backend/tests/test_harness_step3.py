"""第 3 步（P0-1 权限门禁 / P1-4 审计日志）回归测试。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step3.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step3_test.db"
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
from app.harness import approvals, audit, permissions            # noqa: E402
from app.harness.context import ContextManager                   # noqa: E402
from app.harness.loop import run_agent                           # noqa: E402
from app.harness.runctx import RunContext                        # noqa: E402
from app.tools import registry                                   # noqa: E402


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


def _run_agent_with(tool_calls_seq, run, tools, execute_impl=None):
    """跑 run_agent：第 1 步发出给定的 tool_calls，第 2 步收尾。"""
    from app.harness import loop as loop_mod
    step = {"n": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        step["n"] += 1
        if step["n"] == 1:
            return _FakeMsg(content="开始执行", tool_calls=tool_calls_seq)
        return _FakeMsg(content="执行完毕")

    orig = (loop_mod.llm.chat_with_retry, registry.execute,
            ContextManager._summarize)
    loop_mod.llm.chat_with_retry = chat
    if execute_impl:
        registry.execute = execute_impl
    ContextManager._summarize = staticmethod(lambda m: "[STUB]")
    try:
        agent = {"name": "TestAgent", "system_prompt": "SYS", "tools": list(tools)}
        return list(run_agent(agent, "测试", run=run))
    finally:
        (loop_mod.llm.chat_with_retry, registry.execute,
         ContextManager._summarize) = orig


def _reset_audit():
    db.execute("DELETE FROM agent_audit")


# ══════════════════════════════════════════════════════
# 决策矩阵：18 个工具 × 3 种模式
# ══════════════════════════════════════════════════════

def test_decision_matrix():
    """核心矩阵：只读放行 / readonly 拦治理但放业务写 / 破坏性始终 ask。"""
    cases = [
        # (工具, readonly, confirm, auto)
        ("query_metrics",        "allow", "allow", "allow"),   # 只读
        ("sql_query",            "allow", "allow", "allow"),
        ("get_topology",         "allow", "allow", "allow"),
        # 写业务表、不碰集群 → readonly 也放行（评审决策 D1 的核心）
        ("run_risk_scan",        "allow", "ask",   "allow"),
        ("build_topology",       "allow", "ask",   "allow"),
        # 配置变更：刻意不标 writes_business_data → readonly 拒绝
        ("create_risk_rule",     "deny",  "ask",   "ask"),
        # 破坏性：readonly 拒绝，confirm/auto 都要人工确认
        ("patch_deployment",     "deny",  "ask",   "ask"),
        ("create_pdb",           "deny",  "ask",   "ask"),
        ("create_db_index",      "deny",  "ask",   "ask"),
        ("upgrade_rds_instance", "deny",  "ask",   "ask"),
    ]
    bad = []
    for tool, *expect in cases:
        for mode, exp in zip(("readonly", "confirm", "auto"), expect):
            got = permissions.decide(tool, {"name": "x"}, mode=mode).behavior
            if got != exp:
                bad.append(f"{tool}/{mode}: 期望 {exp} 实际 {got}")
    assert not bad, "决策矩阵不符：\n    " + "\n    ".join(bad)
    print(f"  ✓ 决策矩阵全对（{len(cases)} 工具 × 3 模式 = {len(cases)*3} 组合）")


def test_auto_mode_uses_whitelist_not_blacklist():
    """★ auto 模式必须用白名单：只放行明确标记 writes_business_data 的工具。

    实施时踩到的坑：原逻辑是 `not is_destructive` 就放行（黑名单），导致
      1. create_risk_rule（配置变更，改变后续所有扫描行为）被自动放行；
      2. 更危险的是未来新增工具若忘标 is_destructive，auto 模式会自动放行它。
    改为白名单后，未分类的工具默认需要确认（fail-closed）。
    本用例把这个决定固化，防止被改回黑名单。
    """
    # ① 明确标记 writes_business_data 的 → auto 放行
    for t in ("run_risk_scan", "build_topology"):
        d = permissions.decide(t, {}, mode="auto")
        assert d.behavior == "allow", f"{t} 在 auto 下应放行，实际 {d.behavior}"

    # ② 非破坏性但未标 writes_business_data（配置变更）→ auto 仍需确认
    d = permissions.decide("create_risk_rule", {"rule_id": "AI-001"}, mode="auto")
    assert d.behavior == "ask", \
        f"配置变更类在 auto 下应仍需确认（白名单语义），实际 {d.behavior}"

    # ③ 模拟「新增工具忘标安全属性」：必须 fail-closed 为 ask
    from app.tools.registry import ToolSpec, _REGISTRY
    _REGISTRY["__新工具_忘标了__"] = ToolSpec(
        func=lambda: None,
        schema={"type": "function",
                "function": {"name": "__新工具_忘标了__", "description": "",
                             "parameters": {"type": "object", "properties": {}}}})
    try:
        for mode in ("confirm", "auto"):
            d = permissions.decide("__新工具_忘标了__", {}, mode=mode)
            assert d.behavior == "ask", \
                f"忘标安全属性的工具在 {mode} 下应 ask，实际 {d.behavior}"
        d = permissions.decide("__新工具_忘标了__", {}, mode="readonly")
        assert d.behavior == "deny", "忘标的工具在 readonly 下应拒绝"
    finally:
        _REGISTRY.pop("__新工具_忘标了__", None)

    print("  ✓ auto 用白名单：业务写放行 / 配置变更仍确认 / 忘标工具 fail-closed")


def test_bug4_sql_query_can_reach_numeric_tables():
    """★ Bug 4 回归：sql_query 必须能查带数字的表名（k8s_resources / k8s_events）。

    原实现的表名提取正则是 [a-z_]+（不含数字），k8s_resources 被截成 "k"，
    再对照白名单必然失败 —— 等于这两张最关键的配置表【永远查不了】。
    实测中模型连试 3 次 sql_query 全被拒，只能放弃或绕路。
    """
    allowed = [
        "SELECT kind, COUNT(*) c FROM k8s_resources GROUP BY kind",
        "SELECT reason FROM k8s_events LIMIT 3",
        "SELECT metric_name FROM metrics LIMIT 2",
        # 带别名的 JOIN：两张表都要被正确识别
        "SELECT t.service FROM trace_spans t JOIN k8s_resources k ON 1=1 LIMIT 1",
    ]
    rejected = [
        "SELECT * FROM users",              # 白名单外
        "DELETE FROM metrics",              # 非 SELECT
        "SELECT * FROM sqlite_master",      # 元数据表
    ]
    import json as _json
    for sql in allowed:
        r = _json.loads(registry.execute("sql_query", {"sql": sql}))
        assert "error" not in r, f"应放行但被拒: {sql} → {r['error']}"
    for sql in rejected:
        r = _json.loads(registry.execute("sql_query", {"sql": sql}))
        assert "error" in r, f"应拒绝但放行了: {sql}"
    print(f"  ✓ 带数字表名可查（{len(allowed)} 条），白名单防护仍有效（{len(rejected)} 条被拒）")


def test_unknown_tool_denied():
    d = permissions.decide("不存在的工具", {})
    assert d.behavior == "deny" and d.reason_type == permissions.REASON_RULE
    print("  ✓ 未知工具被拒（第 1 层）")


def test_readonly_message_is_actionable():
    """readonly 拒绝时的说明必须能指导用户下一步，而不只是说'不行'。"""
    d = permissions.decide("patch_deployment", {"name": "x"}, mode="readonly")
    assert "只读巡检模式" in d.message and "切换" in d.message
    print(f"  ✓ 拒绝原因可操作：{d.message[:38]}...")


def test_ingest_data_denied_in_live():
    """ingest_data 会 DELETE 七张观测表 —— live 模式必须机制层拒绝。"""
    orig = config.DATA_SOURCE
    try:
        config.DATA_SOURCE = "live"
        d = permissions.decide("ingest_data", {}, mode="confirm")
        assert d.behavior == "deny", f"live 模式未拒绝 ingest_data：{d}"
        config.DATA_SOURCE = "static"
        d2 = permissions.decide("ingest_data", {}, mode="confirm")
        assert d2.behavior == "ask", f"static 模式应为 ask：{d2}"
    finally:
        config.DATA_SOURCE = orig
    print("  ✓ ingest_data：live 拒绝 / static 需确认（从提示词层升级为机制层）")


def test_approval_key_granularity():
    """会话内批准的粒度是「工具+资源」，换资源仍需重新确认。"""
    k1 = permissions.approval_key("patch_deployment", {"name": "order-service"})
    k2 = permissions.approval_key("patch_deployment", {"name": "user-service"})
    k3 = permissions.approval_key("create_pdb", {"app": "frontend"})
    k4 = permissions.approval_key("create_db_index", {"table": "orders"})
    assert k1 != k2, "不同资源不应共用批准"
    assert "order-service" in k1 and "frontend" in k3 and "orders" in k4
    print(f"  ✓ 批准粒度：{k1} / {k3} / {k4}")


def test_session_approval_grants():
    d = permissions.decide("patch_deployment", {"name": "svc"}, mode="confirm",
                           session_approvals=frozenset())
    assert d.behavior == "ask"
    key = permissions.approval_key("patch_deployment", {"name": "svc"})
    d2 = permissions.decide("patch_deployment", {"name": "svc"}, mode="confirm",
                            session_approvals=frozenset({key}))
    assert d2.behavior == "allow" and d2.reason_type == permissions.REASON_USER
    print("  ✓ 会话内已批准 → 同资源直接放行（reason=user）")


def test_check_permissions_exception_fails_closed():
    """工具的权限判定函数自己抛异常时，必须按 ask 处理（fail-closed）。"""
    spec = registry.get_spec("patch_deployment")
    orig = spec.check_permissions
    try:
        spec.check_permissions = lambda a: (_ for _ in ()).throw(RuntimeError("炸"))
        d = permissions.decide("patch_deployment", {"name": "x"}, mode="confirm")
        assert d.behavior == "ask", f"应 fail-closed 为 ask，实际 {d.behavior}"
    finally:
        spec.check_permissions = orig
    print("  ✓ 权限判定函数异常 → fail-closed 按 ask 处理")


def test_all_destructive_tools_tagged():
    """护栏：治理类工具必须都标了 is_destructive，防止新增工具漏标。"""
    must_be_destructive = {"patch_deployment", "create_pdb", "create_db_index",
                           "upgrade_rds_instance", "ingest_data"}
    missing = [t for t in must_be_destructive
               if not registry.get_spec(t).is_destructive]
    assert not missing, f"以下工具漏标 is_destructive: {missing}"
    # 反向：只读工具不能被误标为破坏性
    wrong = [r["name"] for r in registry.describe_tools()
             if r["read_only"] and r["destructive"]]
    assert not wrong, f"只读工具被误标破坏性: {wrong}"
    print(f"  ✓ {len(must_be_destructive)} 个治理工具均已标 destructive，只读工具无误标")


# ══════════════════════════════════════════════════════
# 阻塞式确认（方案 A）
# ══════════════════════════════════════════════════════

def test_blocking_approval_granted():
    """确认通过 → 工具真正执行。"""
    _reset_audit()
    run = RunContext(session_id="s-grant", mode="confirm")
    executed = []

    def fake_exec(name, args, run=None):
        executed.append(name)
        return '{"status":"patched"}'

    tc = _FakeToolCall("req-1", "patch_deployment",
                       '{"name":"order-service","action":"set_replicas","value":"3"}')
    # 后台 0.3s 后批准
    threading.Timer(0.3, lambda: approvals.resolve("req-1", True)).start()
    evs = _run_agent_with([tc], run, ["patch_deployment"], execute_impl=fake_exec)

    types = [e["type"] for e in evs]
    assert "permission_request" in types, f"未推确认请求：{types}"
    assert "permission_granted" in types, f"未推批准事件：{types}"
    assert executed == ["patch_deployment"], f"工具未执行：{executed}"

    pr = next(e for e in evs if e["type"] == "permission_request")
    assert pr["is_destructive"] is True
    assert "order-service" in pr["summary"] and "set_replicas" in pr["summary"]
    print(f"  ✓ 批准后执行；确认卡片摘要=\"{pr['summary']}\"")


def test_blocking_approval_rejected():
    """用户拒绝 → 工具不执行，且拒绝信息回传给模型（含配对完好）。"""
    _reset_audit()
    run = RunContext(session_id="s-reject", mode="confirm")
    executed = []
    tc = _FakeToolCall("req-2", "upgrade_rds_instance", '{"instance_id":"rds-01"}')
    threading.Timer(0.3, lambda: approvals.resolve("req-2", False)).start()
    evs = _run_agent_with([tc], run, ["upgrade_rds_instance"],
                          execute_impl=lambda n, a, run=None: executed.append(n) or "{}")

    assert executed == [], "被拒绝仍执行了工具！"
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert "用户拒绝" in str(tr["result"]), f"拒绝信息未回传模型：{tr}"
    rows = audit.query(limit=5)
    assert rows[0]["result_status"] == audit.REJECTED
    print(f"  ✓ 拒绝后不执行，信息回传模型，审计 status={rows[0]['result_status']}")


def test_approval_timeout_denies():
    """超时按拒绝处理（fail-closed），并推「即将超时」提醒。"""
    _reset_audit()
    orig_t, orig_w = config.APPROVAL_TIMEOUT_S, config.APPROVAL_WARN_BEFORE_S
    config.APPROVAL_TIMEOUT_S, config.APPROVAL_WARN_BEFORE_S = 1.0, 0.5
    warned = []
    run = RunContext(session_id="s-timeout", mode="confirm")
    run.event_sink = lambda ev: warned.append(ev)
    executed = []
    tc = _FakeToolCall("req-3", "create_db_index",
                       '{"table":"orders","columns":["status"]}')
    try:
        evs = _run_agent_with([tc], run, ["create_db_index"],
                              execute_impl=lambda n, a, run=None: executed.append(n) or "{}")
    finally:
        config.APPROVAL_TIMEOUT_S, config.APPROVAL_WARN_BEFORE_S = orig_t, orig_w

    assert executed == [], "超时仍执行了工具！"
    expiring = [e for e in warned if e.get("type") == "permission_expiring"]
    assert expiring, f"未推超时提醒，旁路事件={[e.get('type') for e in warned]}"
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert "超时" in str(tr["result"])
    rows = audit.query(limit=5)
    assert rows[0]["result_status"] == audit.TIMEOUT
    print(f"  ✓ 超时拒绝 + 提醒已推（seconds_left={expiring[0]['seconds_left']}）")


def test_remember_skips_second_confirm():
    """勾选「本次会话都允许」→ 同资源第二次不再询问。"""
    _reset_audit()
    run = RunContext(session_id="s-remember", mode="confirm")
    calls = []
    args = '{"name":"order-service","action":"add_probes"}'
    tc1 = _FakeToolCall("req-4a", "patch_deployment", args)
    tc2 = _FakeToolCall("req-4b", "patch_deployment", args)
    threading.Timer(0.3, lambda: approvals.resolve("req-4a", True, remember=True)).start()
    evs = _run_agent_with([tc1, tc2], run, ["patch_deployment"],
                          execute_impl=lambda n, a, run=None: calls.append(n) or '{"ok":1}')

    reqs = [e for e in evs if e["type"] == "permission_request"]
    assert len(reqs) == 1, f"应只询问 1 次，实际 {len(reqs)} 次"
    assert len(calls) == 2, f"两次都应执行，实际 {len(calls)}"
    print(f"  ✓ remember 生效：询问 1 次、执行 2 次")


def test_readonly_mode_blocks_governance_end_to_end():
    """端到端：readonly 模式下治理被拦，且不弹确认卡片（直接拒绝）。"""
    _reset_audit()
    run = RunContext(session_id="s-ro", mode="readonly")
    executed = []
    tc = _FakeToolCall("req-5", "patch_deployment", '{"name":"svc","action":"add_probes"}')
    evs = _run_agent_with([tc], run, ["patch_deployment"],
                          execute_impl=lambda n, a, run=None: executed.append(n) or "{}")
    types = [e["type"] for e in evs]
    assert "permission_request" not in types, "readonly 不该弹确认，应直接拒绝"
    assert executed == []
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert "只读巡检模式" in str(tr["result"])
    rows = audit.query(limit=5)
    assert rows[0]["decision"] == "deny" and rows[0]["reason_type"] == "mode"
    print(f"  ✓ readonly 端到端拦截，审计 decision=deny reason=mode")


def test_readonly_allows_risk_scan_end_to_end():
    """★ D1 决策验证：readonly 模式下风险扫描仍可执行（否则'巡检'失去意义）。"""
    _reset_audit()
    run = RunContext(session_id="s-ro2", mode="readonly")
    executed = []
    tc = _FakeToolCall("req-6", "run_risk_scan", "{}")
    evs = _run_agent_with([tc], run, ["run_risk_scan"],
                          execute_impl=lambda n, a, run=None: executed.append(n) or '{"summary":{}}')
    assert executed == ["run_risk_scan"], "readonly 下扫描被误拦，D1 决策未生效"
    assert "permission_request" not in [e["type"] for e in evs]
    print("  ✓ readonly 下 run_risk_scan 正常执行（D1：只写业务表不算治理）")


def test_no_env_backdoor_bypasses_confirmation():
    """★ 防后门：不存在任何环境变量能让 ask 绕过人工确认。

    背景：前端确认卡片就绪前曾有 PERMISSION_AUTO_APPROVE 逃生阀。前端接入后已移除
    —— 留着等于给"绕过人工确认"开了一个改环境变量就能打开的后门。
    本用例固化该安全属性，防止日后被重新引入。
    """
    assert not hasattr(config, "PERMISSION_AUTO_APPROVE"), \
        "PERMISSION_AUTO_APPROVE 又回来了 —— 这是绕过人工确认的后门，不允许存在"

    # 即使设置了同名环境变量，也必须照常走确认流程（超时按拒绝）
    os.environ["PERMISSION_AUTO_APPROVE"] = "1"
    orig_t = config.APPROVAL_TIMEOUT_S
    config.APPROVAL_TIMEOUT_S = 0.6
    _reset_audit()
    run = RunContext(session_id="s-backdoor", mode="confirm")
    executed = []
    tc = _FakeToolCall("req-bd", "patch_deployment",
                       '{"name":"svc","action":"add_probes"}')
    try:
        evs = _run_agent_with([tc], run, ["patch_deployment"],
                              execute_impl=lambda n, a, run=None: executed.append(n) or "{}")
    finally:
        config.APPROVAL_TIMEOUT_S = orig_t
        os.environ.pop("PERMISSION_AUTO_APPROVE", None)

    assert executed == [], "环境变量绕过了人工确认 —— 后门仍存在"
    assert any(e["type"] == "permission_request" for e in evs), "未走确认流程"
    rows = audit.query(limit=3)
    assert rows[0]["result_status"] == audit.TIMEOUT, \
        f"应超时按拒绝，实际 {rows[0]['result_status']}"
    assert rows[0]["reason_type"] != permissions.REASON_AUTO_APPROVE
    print("  ✓ 无环境变量后门：设了 PERMISSION_AUTO_APPROVE=1 仍照常要求确认")


def test_abort_cancels_pending_approval():
    """会话中断时必须唤醒挂起的确认，否则 worker 线程泄漏。"""
    orig_t = config.APPROVAL_TIMEOUT_S
    config.APPROVAL_TIMEOUT_S = 30.0            # 足够长，确保是被 cancel 唤醒的
    run = RunContext(session_id="s-cancel", mode="confirm")
    executed = []
    tc = _FakeToolCall("req-8", "patch_deployment", '{"name":"svc","action":"add_probes"}')

    def canceller():
        time.sleep(0.4)
        run.abort.set()
        # 走真实路径：/api/chat/stop 用的是会话级取消
        approvals.cancel_session(run.session_id)

    threading.Thread(target=canceller, daemon=True).start()
    try:
        t0 = time.time()
        evs = _run_agent_with([tc], run, ["patch_deployment"],
                              execute_impl=lambda n, a, run=None: executed.append(n) or "{}")
        elapsed = time.time() - t0
    finally:
        config.APPROVAL_TIMEOUT_S = orig_t

    assert elapsed < 5.0, f"中断未唤醒等待，耗时 {elapsed:.1f}s（应远小于 30s 超时）"
    assert executed == [], "中断后仍执行了工具"
    assert not approvals.pending(), f"仍有挂起请求：{approvals.pending()}"
    print(f"  ✓ 中断 {elapsed:.1f}s 内唤醒挂起确认，无线程泄漏")


# ══════════════════════════════════════════════════════
# 审计日志
# ══════════════════════════════════════════════════════

def test_cancel_is_session_scoped():
    """★ 中止会话 A 不得取消会话 B 的待确认。

    真实链路验证时发现的 bug：cancel_all() 是全局的，而 request_id 是 LLM 的
    tool_call id（call_xxx），不含会话信息，所以原先的 prefix 参数形同虚设。
    日志实证：「手动中止会话 v-abort2」却取消了 v-abort 的待确认。
    """
    # 手工登记两个不同会话的等待者
    tA = threading.Thread(
        target=lambda: approvals.wait("rid-A", timeout=10, session_id="sess-A"),
        daemon=True)
    tB = threading.Thread(
        target=lambda: approvals.wait("rid-B", timeout=10, session_id="sess-B"),
        daemon=True)
    tA.start()
    tB.start()
    time.sleep(0.3)
    assert set(approvals.pending()) >= {"rid-A", "rid-B"}
    assert approvals.pending("sess-A") == ["rid-A"], \
        f"按会话过滤失效：{approvals.pending('sess-A')}"

    n = approvals.cancel_session("sess-A")
    assert n == 1, f"应只取消 1 个，实际 {n}"
    time.sleep(0.3)
    assert "rid-B" in approvals.pending(), "会话 B 的待确认被误取消了"

    approvals.cancel_session("sess-B")       # 清理
    time.sleep(0.2)
    print("  ✓ 取消按会话隔离：中止 A 不影响 B（修复全局误伤）")


def test_abort_records_aborted_not_rejected():
    """★ 审计保真：按「停止」导致确认被取消，应记 aborted 而非 rejected。

    浏览器实测中发现的失真：cancel_all() 唤醒挂起的确认后，原实现只按
    timeout/rejected 二分，于是"用户按了停止"被记成"用户拒绝了此操作"。
    对面向合规的审计功能，这种归因错误必须避免。
    """
    _reset_audit()
    orig_t = config.APPROVAL_TIMEOUT_S
    config.APPROVAL_TIMEOUT_S = 30.0            # 足够长，确保是被 cancel 唤醒
    run = RunContext(session_id="s-abort-audit", mode="confirm")
    executed = []
    tc = _FakeToolCall("req-ab", "patch_deployment",
                       '{"name":"svc","action":"add_probes"}')

    def canceller():
        time.sleep(0.4)
        run.abort.set()
        # 走真实路径：/api/chat/stop 用的是会话级取消
        approvals.cancel_session(run.session_id)

    threading.Thread(target=canceller, daemon=True).start()
    try:
        evs = _run_agent_with([tc], run, ["patch_deployment"],
                              execute_impl=lambda n, a, run=None: executed.append(n) or "{}")
    finally:
        config.APPROVAL_TIMEOUT_S = orig_t

    assert executed == [], "中断后仍执行了工具"
    rows = audit.query(limit=5)
    assert rows, "无审计记录"
    assert rows[0]["result_status"] == audit.ABORTED, \
        f"应记 aborted，实际 {rows[0]['result_status']}（把中断错记成拒绝会导致审计失真）"
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert "会话已中断" in str(tr["result"]), f"回给模型的说明不准确：{tr['result']}"
    print("  ✓ 停止导致的取消记为 aborted（与用户主动拒绝区分开）")


def test_audit_records_full_chain():
    """审计要记全：谁、什么模式、什么决定、依据哪层、结果、耗时。"""
    _reset_audit()
    run = RunContext(session_id="s-audit", mode="confirm")
    tc = _FakeToolCall("req-9", "patch_deployment",
                       '{"name":"payment-service","action":"set_memory_limit","value":"2048Mi"}')
    threading.Timer(0.2, lambda: approvals.resolve("req-9", True)).start()
    _run_agent_with([tc], run, ["patch_deployment"],
                    execute_impl=lambda n, a, run=None: '{"status":"patched"}')

    rows = audit.query(limit=5)
    assert rows, "无审计记录"
    r = rows[0]
    checks = {
        "session_id": r["session_id"] == "s-audit",
        "agent_name": r["agent_name"] == "TestAgent",
        "tool_name": r["tool_name"] == "patch_deployment",
        "decision=allow": r["decision"] == "allow",
        "reason=user": r["reason_type"] == "user",
        "run_mode=confirm": r["run_mode"] == "confirm",
        "is_destructive=1": r["is_destructive"] == 1,
        "status=ok": r["result_status"] == "ok",
        "有耗时": r["duration_ms"] >= 0,
        "摘要含资源名": "payment-service" in (r["audit_repr"] or ""),
        "参数已存": "2048Mi" in (r["args_json"] or ""),
    }
    bad = [k for k, v in checks.items() if not v]
    assert not bad, f"审计字段不全: {bad}\n实际: {r}"
    print(f"  ✓ 审计完整（{len(checks)} 项）：\"{r['audit_repr']}\" by {r['reason_type']}")


def test_audit_readonly_tools_also_recorded():
    """只读工具也要进审计（否则无法还原完整操作序列）。"""
    _reset_audit()
    run = RunContext(session_id="s-ro-audit", mode="confirm")
    tc = _FakeToolCall("req-10", "query_metrics", '{"metric_name":"cpu"}')
    _run_agent_with([tc], run, ["query_metrics"],
                    execute_impl=lambda n, a, run=None: '{"rows":[]}')
    rows = audit.query(limit=5)
    assert rows and rows[0]["tool_name"] == "query_metrics"
    assert rows[0]["decision"] == "allow" and rows[0]["reason_type"] == "rule"
    assert rows[0]["is_destructive"] == 0
    print("  ✓ 只读工具也记审计（decision=allow reason=rule destructive=0）")


def test_audit_destructive_filter_and_summary():
    _reset_audit()
    run = RunContext(session_id="s-mix", mode="confirm")
    # 一次只读 + 一次被 readonly 拒绝的破坏性
    _run_agent_with([_FakeToolCall("m1", "query_metrics", "{}")], run,
                    ["query_metrics"], execute_impl=lambda n, a, run=None: "{}")
    run2 = RunContext(session_id="s-mix", mode="readonly")
    _run_agent_with([_FakeToolCall("m2", "patch_deployment", '{"name":"s","action":"add_probes"}')],
                    run2, ["patch_deployment"], execute_impl=lambda n, a, run=None: "{}")

    all_rows = audit.query(limit=50)
    dest = audit.query(limit=50, destructive_only=True)
    s = audit.summary("s-mix")
    assert len(all_rows) == 2, f"应有 2 条，实际 {len(all_rows)}"
    assert len(dest) == 1 and dest[0]["tool_name"] == "patch_deployment"
    assert s["total"] == 2 and s["destructive"] == 1 and s["denied"] == 1
    print(f"  ✓ 审计筛选与概览：total={s['total']} destructive={s['destructive']} "
          f"denied={s['denied']} by_status={s['by_status']}")


def test_audit_failure_does_not_break_agent():
    """审计写入失败绝不能搞挂 Agent。"""
    run = RunContext(session_id="s-auditfail", mode="confirm")
    orig = db.execute
    executed = []
    try:
        db.execute = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB 挂了"))
        evs = _run_agent_with([_FakeToolCall("f1", "query_metrics", "{}")], run,
                              ["query_metrics"],
                              execute_impl=lambda n, a, run=None: executed.append(n) or "{}")
    finally:
        db.execute = orig
    assert executed == ["query_metrics"], "审计失败导致工具未执行"
    assert any(e["type"] == "answer" for e in evs), "审计失败导致 Agent 未出答案"
    print("  ✓ 审计写入失败被吞掉，Agent 正常完成")


def test_pairing_invariant_on_all_reject_paths():
    """★ 所有拒绝路径都必须补齐 tool_result，否则下一轮 API 400。"""
    orig_t, orig_w = config.APPROVAL_TIMEOUT_S, config.APPROVAL_WARN_BEFORE_S
    config.APPROVAL_TIMEOUT_S, config.APPROVAL_WARN_BEFORE_S = 0.6, 0.3
    scenarios = []
    try:
        # ① readonly 拒绝  ② 用户拒绝  ③ 超时拒绝
        for label, mode, rid, resolver in [
            ("readonly拒绝", "readonly", "p1", None),
            ("用户拒绝", "confirm", "p2", lambda: approvals.resolve("p2", False)),
            ("确认超时", "confirm", "p3", None),
        ]:
            captured = {}
            from app.harness import loop as loop_mod

            def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
                captured["messages"] = messages
                if any(m.get("role") == "tool" for m in messages):
                    return _FakeMsg(content="收尾")
                return _FakeMsg(content="执行",
                                tool_calls=[_FakeToolCall(rid, "patch_deployment",
                                                          '{"name":"s","action":"add_probes"}')])

            if resolver:
                threading.Timer(0.2, resolver).start()
            o = (loop_mod.llm.chat_with_retry, registry.execute, ContextManager._summarize)
            loop_mod.llm.chat_with_retry = chat
            registry.execute = lambda n, a, run=None: "{}"
            ContextManager._summarize = staticmethod(lambda m: "[S]")
            try:
                list(run_agent({"name": "T", "system_prompt": "S",
                                "tools": ["patch_deployment"]},
                               "测试", run=RunContext(session_id="s-pair", mode=mode)))
            finally:
                (loop_mod.llm.chat_with_retry, registry.execute,
                 ContextManager._summarize) = o

            c = ContextManager("S")
            c.messages = captured["messages"]
            c.assert_api_invariants()          # 不抛异常即通过
            n_tool = sum(1 for m in c.messages if m.get("role") == "tool")
            scenarios.append(f"{label}(tool_result={n_tool})")
    finally:
        config.APPROVAL_TIMEOUT_S, config.APPROVAL_WARN_BEFORE_S = orig_t, orig_w
    print(f"  ✓ 三条拒绝路径配对均完好：{' / '.join(scenarios)}")


# ══════════════════════════════════════════════════════

def main():
    db.init_db()
    registry.ensure_loaded()
    groups = [
        ("P0-1 · 权限决策", [
            test_decision_matrix,
            test_auto_mode_uses_whitelist_not_blacklist,
            test_bug4_sql_query_can_reach_numeric_tables,
            test_unknown_tool_denied,
            test_readonly_message_is_actionable,
            test_ingest_data_denied_in_live,
            test_approval_key_granularity,
            test_session_approval_grants,
            test_check_permissions_exception_fails_closed,
            test_all_destructive_tools_tagged,
        ]),
        ("P0-1 · 阻塞式确认（方案 A）", [
            test_blocking_approval_granted,
            test_blocking_approval_rejected,
            test_approval_timeout_denies,
            test_remember_skips_second_confirm,
            test_readonly_mode_blocks_governance_end_to_end,
            test_readonly_allows_risk_scan_end_to_end,
            test_no_env_backdoor_bypasses_confirmation,
            test_abort_cancels_pending_approval,
        ]),
        ("P1-4 · 审计日志", [
            test_cancel_is_session_scoped,
            test_abort_records_aborted_not_rejected,
            test_audit_records_full_chain,
            test_audit_readonly_tools_also_recorded,
            test_audit_destructive_filter_and_summary,
            test_audit_failure_does_not_break_agent,
            test_pairing_invariant_on_all_reject_paths,
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
