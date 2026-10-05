"""第 6 步（Part D · P2 打磨项）回归测试。

覆盖 P2-3 volatile 后置 / P2-5 跨轮工具轨迹 / P2-6 结构化意图 /
P2-7 自主预诊断 / P2-8 注册注释，以及顺带修的两个缺陷。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step6.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step6_test.db"
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
from app.harness import background, intent, memory               # noqa: E402
from app.harness.context import ContextManager                   # noqa: E402
from app.tools import registry                                   # noqa: E402


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for c1, c2 in zip(a, b):
        if c1 != c2:
            break
        n += 1
    return n


# ══════════════════════════════════════════════════════
# P2-3 volatile 内容后置
# ══════════════════════════════════════════════════════

def test_memory_is_last_in_prompt():
    """★ 每次都变的记忆必须在提示词【末尾】，否则它后面的内容全失去缓存资格。"""
    db.execute("DELETE FROM agent_memory")
    memory.remember("conclusion", "order-service", "order-service 慢因 RDS 慢查询")
    p = build_agent("risk", "扫描风险")["system_prompt"]

    mem_frag = "order-service 慢因 RDS 慢查询"
    assert mem_frag in p, "记忆未注入"
    tail = p[p.index(mem_frag) + len(mem_frag):]
    # 记忆之后只允许剩空白，不能再有 live_note 之类的固定内容
    assert tail.strip() == "", f"记忆后面还有内容（会破坏前缀稳定性）：{tail[:120]!r}"
    print("  ✓ 记忆位于提示词末尾，其后无任何内容")


def test_prompt_prefix_stable_across_queries():
    """同一 Agent 不同 query：差异必须只在尾部，前缀保持稳定。"""
    db.execute("DELETE FROM agent_memory")
    memory.remember("conclusion", "svc-a", "A" * 100)
    memory.remember("conclusion", "svc-b", "B" * 100)
    p1 = build_agent("risk", "svc-a 的结论")["system_prompt"]
    p2 = build_agent("risk", "svc-b 的结论")["system_prompt"]

    cp = _common_prefix_len(p1, p2)
    stable_part = min(len(p1), len(p2))
    # 稳定前缀应覆盖绝大部分提示词（含整篇 Skill 文档）
    assert cp > stable_part * 0.7, \
        f"公共前缀仅 {cp}/{stable_part}，volatile 内容仍插在中间"
    print(f"  ✓ 公共前缀 {cp} 字符，占提示词 {cp * 100 // stable_part}%")


def test_live_note_before_memory():
    """live_note 是进程内固定的，必须排在 volatile 记忆之前。"""
    orig = config.DATA_SOURCE
    config.DATA_SOURCE = "live"
    try:
        db.execute("DELETE FROM agent_memory")
        memory.remember("conclusion", "k", "MEMORY_MARKER")
        p = build_agent("risk", "x")["system_prompt"]
        assert "live 模式" in p, "live_note 未注入"
        assert p.index("live 模式") < p.index("MEMORY_MARKER"), \
            "live_note 排在记忆之后，会被 volatile 内容带得失去缓存资格"
    finally:
        config.DATA_SOURCE = orig
    print("  ✓ live_note 在记忆之前（稳定内容在前）")


# ══════════════════════════════════════════════════════
# P2-5 跨轮工具轨迹
# ══════════════════════════════════════════════════════

def test_tool_trace_persisted_and_recalled():
    """★ 上轮调用过什么，下一轮要能看见（跨轮追问的前提）。"""
    db.execute("DELETE FROM chat_messages")
    memory.save_chat("s-trace", "user", "下单接口很慢")
    memory.save_chat("s-trace", "assistant", "根因是 RDS 慢查询", tool_calls=[
        {"tool": "query_traces", "args": {"trace_id": "tr000123"}},
        {"tool": "get_topology", "args": {}},
    ])
    h = memory.recent_chat("s-trace")
    asst = next(m for m in h if m["role"] == "assistant")
    assert "tr000123" in asst["content"], \
        f"上轮的 trace_id 没带过来，无法支持「刚才那个 trace 再看看」：{asst['content']}"
    assert "query_traces" in asst["content"] and "get_topology" in asst["content"]
    print(f"  ✓ 跨轮可见：{asst['content'].splitlines()[-1][:70]}")


def test_history_never_carries_structured_tool_calls():
    """★ 历史消息绝不能带结构化 tool_calls —— 会制造孤立 tool_call 导致 API 400。

    历史表里没有存工具【结果】，把 tool_calls 结构化还原回 assistant 消息，
    就会出现"有 tool_calls 但没有对应 role=tool 响应"的非法序列（Bug 1 同类）。
    """
    db.execute("DELETE FROM chat_messages")
    memory.save_chat("s-inv", "user", "q")
    memory.save_chat("s-inv", "assistant", "a", tool_calls=[
        {"tool": "query_logs", "args": {"service": "order"}}])
    h = memory.recent_chat("s-inv")
    for m in h:
        assert "tool_calls" not in m, f"历史消息带了结构化 tool_calls：{m}"
        assert set(m.keys()) == {"role", "content"}, f"多了字段：{m.keys()}"

    # 把历史拼进上下文后，API 不变量必须成立
    c = ContextManager("SYS")
    for m in h:
        c.messages.append(dict(m))
    c.add_user("追问")
    c.assert_api_invariants()               # 不抛即通过
    print("  ✓ 历史仅含 role/content，拼进上下文后 API 不变量通过")


def test_tool_hint_robust_and_bounded():
    """摘要要防御坏数据，且不能反过来挤爆上下文。"""
    assert memory._tool_hint(None) == ""
    assert memory._tool_hint("[]") == ""
    assert memory._tool_hint("{bad json") == ""
    assert memory._tool_hint('["not a dict"]') == ""

    many = [{"tool": f"t{i}", "args": {"k": "v"}} for i in range(20)]
    hint = memory._tool_hint(json.dumps(many))
    assert hint.count("(") <= memory._HINT_MAX_CALLS, f"未限制条数：{hint}"
    assert "等 20 次" in hint, f"未提示总数：{hint}"

    long_args = [{"tool": "sql_query", "args": {"sql": "x" * 500}}]
    hint2 = memory._tool_hint(json.dumps(long_args))
    assert len(hint2) < 200, f"单条参数未截断：{len(hint2)} 字符"
    print(f"  ✓ 坏数据返回空串；20 次调用压到 {len(hint)} 字符，超长参数被截断")


# ══════════════════════════════════════════════════════
# P2-6 结构化意图识别
# ══════════════════════════════════════════════════════

def test_intent_parse_variants():
    """结构化输出仍需容错：多包一层 / 非纯 JSON / 非法 intent / 空实体。"""
    cases = [
        ('{"intent":"risk_scan","entities":{}}', "risk_scan"),
        ('{"result":{"intent":"topology","entities":{"service":"a"}}}', "topology"),
        ('{"data":{"intent":"chat"}}', "chat"),
        ('前缀 {"intent":"data_query","entities":null} 后缀', "data_query"),
    ]
    for raw, want in cases:
        got = intent._parse_intent(raw)
        assert got and got["intent"] == want, f"{raw} → {got}"
        assert isinstance(got["entities"], dict)

    for bad in ['{"intent":"不存在"}', "不是 JSON", "", "[]", '{"no_intent":1}']:
        assert intent._parse_intent(bad) is None, f"非法输入未被拒：{bad}"
    print("  ✓ 4 种合法变体解析成功，5 种非法输入全部拒绝")


def test_intent_entities_normalized():
    """实体值统一成字符串并丢空值，避免 None/嵌套流到下游。"""
    got = intent._parse_intent(
        '{"intent":"chat","entities":{"a":null,"b":"","c":[],"d":{},"e":123,"f":"x"}}')
    assert got["entities"] == {"e": "123", "f": "x"}, got["entities"]
    print(f"  ✓ 实体归一化：只留非空值并转字符串 → {got['entities']}")


def test_intent_falls_back_to_keyword():
    """LLM 不可用/输出不可解析时，必须降级关键词而不是整个哑掉。"""
    orig_avail, orig_text = intent.llm.available, intent.llm.chat_text
    try:
        intent.llm.available = lambda: True
        intent.llm.chat_text = lambda *a, **kw: "彻底不是 JSON"
        r = intent.classify("帮我做一次风险扫描")
        assert r["router"] == "keyword" and r["intent"] == "risk_scan", r

        def boom(*a, **kw):
            raise RuntimeError("LLM down")

        intent.llm.chat_text = boom
        r2 = intent.classify("梳理服务拓扑")
        assert r2["router"] == "keyword" and r2["intent"] == "topology", r2
    finally:
        intent.llm.available, intent.llm.chat_text = orig_avail, orig_text
    print("  ✓ 输出不可解析与 LLM 抛异常两种情况都降级到关键词路由")


def test_json_mode_plumbed_through():
    """json_mode 必须真的传到 API 参数，而不是只写在签名里。"""
    from app.harness import llm
    seen = {}

    class _FakeResp:
        class _C:
            message = type("M", (), {"content": '{"intent":"chat"}', "tool_calls": []})()
        choices = [_C()]

    class _FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    seen.update(kw)
                    return _FakeResp()

    orig = llm.get_client
    llm.get_client = lambda: _FakeClient()
    try:
        llm.chat_text([{"role": "user", "content": "x"}], json_mode=True)
        assert seen.get("response_format") == {"type": "json_object"}, seen.get("response_format")
        assert seen.get("model") == config.LLM_MODEL_FAST, "意图识别应走快模型"
        seen.clear()
        llm.chat_text([{"role": "user", "content": "x"}])
        assert "response_format" not in seen, "未开 json_mode 时不该带该参数"
    finally:
        llm.get_client = orig
    print(f"  ✓ json_mode→response_format 已接线，且默认走 {config.LLM_MODEL_FAST}")


# ══════════════════════════════════════════════════════
# P2-7 自主预诊断
# ══════════════════════════════════════════════════════

def _finding(rule_id, ref, severity="P1"):
    return {"rule_id": rule_id, "resource_ref": ref, "severity": severity,
            "title": f"{rule_id} 标题", "suggestion": "建议", "evidence": {"k": 1}}


def _report(findings):
    return {"open_findings": findings,
            "summary": {"open": len(findings), "P1": 0, "P2": 0}}


class _StubDispatch:
    """替掉 registry.execute，避免真实起子 Agent（会调线上 LLM）。"""

    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {"subagent": "DiagnoseAgent", "status": "ok",
                                   "conclusion": "根因是 X", "tool_calls": 3,
                                   "tokens": 1000}

    def __enter__(self):
        self._orig = registry.execute

        def fake(name, args, run=None):
            self.calls.append((name, args))
            assert name == "dispatch_agent", f"预诊断不应调用 {name}"
            return json.dumps(self.payload, ensure_ascii=False)

        registry.execute = fake
        return self

    def __exit__(self, *exc):
        registry.execute = self._orig
        return False


def test_prediagnose_only_new_p1():
    """★ 只对【新增】且【P1】触发 —— P2 与旧有风险不该反复烧子 Agent。"""
    db.execute("DELETE FROM prediagnosis")
    findings = [_finding("API-001", "POST /api/orders"),
                _finding("CAP-003", "default", "P2"),
                _finding("HA-001", "old-svc")]
    new_keys = {("API-001", "POST /api/orders"), ("CAP-003", "default")}
    with _StubDispatch() as s:
        n = background._prediagnose_new_p1(_report(findings), new_keys)
    assert n == 1, f"应只诊断 1 个（新增的 P1），实际 {n}"
    assert "API-001" in s.calls[0][1]["description"], s.calls
    print("  ✓ 3 条风险里只诊断「新增 P1」那 1 条（P2 与旧风险都跳过）")


def test_prediagnose_rate_limited():
    """★ 单轮上限：一次扫出多个新 P1 也不能同时起多个子 Agent。"""
    db.execute("DELETE FROM prediagnosis")
    findings = [_finding(f"R-{i}", f"svc-{i}") for i in range(6)]
    new_keys = {(f["rule_id"], f["resource_ref"]) for f in findings}
    with _StubDispatch() as s:
        n = background._prediagnose_new_p1(_report(findings), new_keys)
    assert n == config.PREDIAG_MAX_PER_SCAN, \
        f"限流失效：{len(new_keys)} 个新 P1 起了 {n} 个（上限 {config.PREDIAG_MAX_PER_SCAN}）"
    assert len(s.calls) == n
    print(f"  ✓ {len(new_keys)} 个新增 P1 只发起 {n} 次（上限 {config.PREDIAG_MAX_PER_SCAN}）")


def test_prediagnose_dedup_by_finding_key():
    """同一 finding 不重复诊断；剩余的留到下一轮。"""
    db.execute("DELETE FROM prediagnosis")
    findings = [_finding(f"R-{i}", f"svc-{i}") for i in range(4)]
    new_keys = {(f["rule_id"], f["resource_ref"]) for f in findings}
    with _StubDispatch():
        background._prediagnose_new_p1(_report(findings), new_keys)
        background._prediagnose_new_p1(_report(findings), new_keys)

    rows = db.fetch_all("SELECT finding_key FROM prediagnosis")
    keys = [r["finding_key"] for r in rows]
    assert len(keys) == len(set(keys)), f"出现重复诊断：{keys}"
    assert len(keys) == config.PREDIAG_MAX_PER_SCAN * 2, \
        f"两轮应共诊断 {config.PREDIAG_MAX_PER_SCAN * 2} 个，实际 {len(keys)}"
    print(f"  ✓ 两轮共 {len(keys)} 条且无重复（每轮受限流，剩余留待下轮）")


def test_prediagnose_records_real_status():
    """★ 子 Agent 超时被中断时不能记成 ok —— 那种结论是残缺的。"""
    db.execute("DELETE FROM prediagnosis")
    f = _finding("CAP-001", "default/api-gateway")
    aborted = {"subagent": "DiagnoseAgent", "status": "aborted",
               "conclusion": "[子 Agent aborted] 超出时间预算（157s / 120s）",
               "tool_calls": 18, "tokens": 38597}
    with _StubDispatch(aborted):
        background._prediagnose_new_p1(_report([f]), {("CAP-001", "default/api-gateway")})
    row = db.fetch_one("SELECT status, conclusion, tokens FROM prediagnosis")
    assert row["status"] == "aborted", \
        f"超时被中断却记成 {row['status']} —— 会让人误以为已有可用的根因分析"
    assert row["tokens"] == 38597, "用量未记录"
    print(f"  ✓ 中断如实记为 aborted（曾误记 ok，因为只看有没有 error 字段）")


def test_prediagnose_failure_isolated():
    """预诊断炸了不能带崩定时扫描主链路。"""
    db.execute("DELETE FROM prediagnosis")
    orig = registry.execute

    def boom(name, args, run=None):
        raise RuntimeError("子 Agent 崩了")

    registry.execute = boom
    try:
        n = background._prediagnose_new_p1(_report([_finding("R-1", "s")]),
                                           {("R-1", "s")})
    finally:
        registry.execute = orig
    assert n == 0, "异常路径不该计数"
    print("  ✓ 预诊断异常被吞掉，扫描主链路不受影响")


def test_prediagnose_disabled_switch():
    """开关关掉时一次都不派（省 token）。"""
    db.execute("DELETE FROM prediagnosis")
    orig = config.PREDIAGNOSIS_ENABLED
    config.PREDIAGNOSIS_ENABLED = False
    try:
        with _StubDispatch() as s:
            n = background._prediagnose_new_p1(_report([_finding("R-1", "s")]),
                                               {("R-1", "s")})
        assert n == 0 and not s.calls
    finally:
        config.PREDIAGNOSIS_ENABLED = orig
    print("  ✓ PREDIAGNOSIS_ENABLED=False 时完全不派子 Agent")


def test_subagent_returns_explicit_status():
    """dispatch_agent 必须显式回报 status，调用方不该猜。"""
    spec = registry.get_spec("dispatch_agent")
    assert spec is not None
    src = Path("app/tools/agent_tools.py").read_text(encoding="utf-8")
    assert '"status": status' in src, "dispatch_agent 未返回 status 字段"
    print("  ✓ dispatch_agent 返回 status（ok/error/aborted）")


# ══════════════════════════════════════════════════════
# 顺带修的缺陷 + P2-8
# ══════════════════════════════════════════════════════

def test_sql_error_includes_column_hint():
    """★ SQL 报错要附真实列名，否则模型只能反复猜。

    实测中模型猜了 avg_value（真实列是 avg/max/min），光回一句
    "no such column" 它下一步还是猜。附上列名才有可操作性。
    """
    registry.ensure_loaded()
    out = json.loads(registry.execute(
        "sql_query", {"sql": "SELECT no_such_col FROM metrics"}))
    assert "error" in out, out
    cols = out.get("table_columns", {})
    assert "metrics" in cols, f"未附带表结构：{out}"
    assert "metric_name" in cols["metrics"], cols["metrics"]
    assert out.get("hint"), "缺少可操作提示"
    print(f"  ✓ 报错附真实列名：metrics → {cols['metrics'][:6]}")


def test_sql_error_does_not_leak_other_tables():
    """列名提示只给本次查询涉及的表，不泄露无关表结构（也避免噪声）。"""
    out = json.loads(registry.execute(
        "sql_query", {"sql": "SELECT bad FROM risk_findings"}))
    cols = out.get("table_columns", {})
    assert set(cols) == {"risk_findings"}, f"提示了无关表：{set(cols)}"
    print("  ✓ 只提示涉及表的列名")


def test_registry_documents_governance_exclusion():
    """P2-8：governance_tools 刻意不注册为 LLM 工具，需有注释避免后人误加。"""
    src = Path("app/tools/registry.py").read_text(encoding="utf-8")
    idx = src.index("def ensure_loaded")
    doc = src[idx:idx + 600]
    assert "governance_tools" in doc and "刻意" in doc, "缺少说明注释"
    assert "governance_tools" not in doc.split('"""')[2], \
        "governance_tools 被误加进 import"
    assert "governance_tools" not in registry.list_tools()
    print("  ✓ 注释已说明刻意排除，且确未注册为 LLM 工具")


def test_scripted_mode_is_explicit_offline_path():
    """P2-4（D3 决策）：_run_scripted 保留为显式离线路径，且不混进 LLM 可用时的流程。"""
    src = Path("app/harness/scheduler.py").read_text(encoding="utf-8")
    assert "_run_scripted" in src
    assert "离线" in src, "缺少离线定位说明"
    # 只在 llm 不可用时走脚本路径
    assert "elif llm.available():" in src, "脚本路径的触发条件被改动"
    print("  ✓ _run_scripted 仅在 LLM 不可用时触发，定位为显式离线降级")


def main():
    db.init_db()
    registry.ensure_loaded()
    groups = [
        ("P2-3 · volatile 内容后置", [
            test_memory_is_last_in_prompt,
            test_prompt_prefix_stable_across_queries,
            test_live_note_before_memory,
        ]),
        ("P2-5 · 跨轮工具轨迹", [
            test_tool_trace_persisted_and_recalled,
            test_history_never_carries_structured_tool_calls,
            test_tool_hint_robust_and_bounded,
        ]),
        ("P2-6 · 结构化意图识别", [
            test_intent_parse_variants,
            test_intent_entities_normalized,
            test_intent_falls_back_to_keyword,
            test_json_mode_plumbed_through,
        ]),
        ("P2-7 · 自主预诊断", [
            test_prediagnose_only_new_p1,
            test_prediagnose_rate_limited,
            test_prediagnose_dedup_by_finding_key,
            test_prediagnose_records_real_status,
            test_prediagnose_failure_isolated,
            test_prediagnose_disabled_switch,
            test_subagent_returns_explicit_status,
        ]),
        ("顺带修的缺陷 + P2-8/P2-4", [
            test_sql_error_includes_column_hint,
            test_sql_error_does_not_leak_other_tables,
            test_registry_documents_governance_exclusion,
            test_scripted_mode_is_explicit_offline_path,
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
