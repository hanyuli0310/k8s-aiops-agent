"""第 4 步（P0-4 工具结果落盘 / P1-1 只读工具并行）回归测试。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step4.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step4_test.db"
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
from app.harness import audit, tool_results                      # noqa: E402
from app.harness.context import ContextManager                   # noqa: E402
from app.harness.loop import _partition, run_agent               # noqa: E402
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
    """跑 run_agent：第 1 步发出给定 tool_calls，第 2 步收尾。

    返回 (events, ctx)。ctx 是 run_agent 内部真实创建的那个实例 —— 中断路径下
    不会再调用 chat，靠 chat 的入参捕获拿不到最终上下文，必须直接拿实例。
    """
    from app.harness import loop as loop_mod
    step = {"n": 0}
    made = {}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        step["n"] += 1
        if step["n"] == 1:
            return _FakeMsg(content="开始", tool_calls=tool_calls_seq)
        return _FakeMsg(content="完毕")

    orig_init = ContextManager.__init__

    def spy_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        made["ctx"] = self

    orig = (loop_mod.llm.chat_with_retry, registry.execute, ContextManager._summarize)
    loop_mod.llm.chat_with_retry = chat
    if execute_impl:
        registry.execute = execute_impl
    ContextManager._summarize = staticmethod(lambda m: "[STUB]")
    ContextManager.__init__ = spy_init
    try:
        agent = {"name": "T", "system_prompt": "SYS", "tools": list(tools)}
        evs = list(run_agent(agent, "测试", run=run))
        return evs, made["ctx"]
    finally:
        ContextManager.__init__ = orig_init
        (loop_mod.llm.chat_with_retry, registry.execute,
         ContextManager._summarize) = orig


def _big_result(n=400):
    logs = [{"ts": 1700000000 + i, "level": "ERROR", "svc": "order-service",
             "msg": f"upstream timeout #{i}", "trace_id": f"tr{i:06d}"} for i in range(n)]
    return json.dumps({"row_count": n, "logs": logs}, ensure_ascii=False)


# ══════════════════════════════════════════════════════
# P0-4 结构感知预览
# ══════════════════════════════════════════════════════

def test_preview_keeps_structure_and_total():
    """核心价值：预览必须让模型知道【总数】和【完整字段结构】。"""
    raw = _big_result(487)
    out = tool_results.persist_and_preview("t1", "callA", "query_logs", raw, 2000)

    assert len(out) < len(raw) / 10, f"未有效压缩：{len(out)} vs {len(raw)}"
    head = out.split("<persisted-output>")[0].strip()
    obj = json.loads(head)          # 预览必须是【合法 JSON】，不能是被切坏的半段
    assert obj["logs"]["_total"] == 487, "丢了总数，模型无法判断数据规模"
    sample = obj["logs"]["_sample"]
    assert len(sample) == tool_results.SAMPLE_N
    assert set(sample[0]) == {"ts", "level", "svc", "msg", "trace_id"}, \
        "字段结构不完整，模型写不出精确的 sql_query"
    assert "read_tool_result" in out, "未告知取回方式，落盘等于丢失"
    print(f"  ✓ {len(raw)}→{len(out)} 字符，保住 _total=487 与全部 5 个字段，且为合法 JSON")


def test_preview_shrinks_sample_to_stay_valid_json():
    """样本本身太大时应逐级降样本数，而不是把 JSON 切坏。"""
    huge = [{"blob": "x" * 900, "id": i} for i in range(50)]
    raw = json.dumps({"items": huge}, ensure_ascii=False)
    out = tool_results.persist_and_preview("t1", "callB", "big", raw, 1200)
    head = out.split("<persisted-output>")[0].strip()
    obj = json.loads(head)          # 关键：仍是合法 JSON
    assert obj["items"]["_total"] == 50
    assert len(obj["items"].get("_sample", [])) < tool_results.SAMPLE_N, \
        "未降级样本数"
    print(f"  ✓ 样本过大时降到 {len(obj['items'].get('_sample', []))} 条以保住 JSON 合法性")


def test_non_json_falls_back_to_truncation():
    """纯文本结果无法做结构预览，退回截断但不应崩。"""
    raw = "plain text " * 5000
    out = tool_results.persist_and_preview("t1", "callC", "raw", raw, 500)
    assert "<persisted-output>" in out, "纯文本也应给出取回路径"
    assert len(out) < len(raw)
    print("  ✓ 非 JSON 结果退回截断 + 仍可取回")


def test_idempotent_write():
    """同一 tool_call_id 重试时不重复写盘。"""
    raw = _big_result(200)
    tool_results.persist_and_preview("t-idem", "callD", "query_logs", raw, 1000)
    p = tool_results.STORE / "t-idem" / "query_logs-callD.json"
    assert p.is_file()
    mtime1 = p.stat().st_mtime
    time.sleep(0.05)
    tool_results.persist_and_preview("t-idem", "callD", "query_logs", raw, 1000)
    assert p.stat().st_mtime == mtime1, "重复写盘了"
    tool_results.cleanup("t-idem")
    print("  ✓ 幂等：同 tool_call_id 不重复写盘")


def test_under_limit_returns_unchanged():
    """未超限必须原样返回，不落盘、不加噪声。"""
    small = json.dumps({"ok": True})
    out = tool_results.persist_and_preview("t-small", "callE", "x", small, 2000)
    assert out == small
    assert not (tool_results.STORE / "t-small").exists()
    print("  ✓ 未超限原样返回，不产生文件")


# ══════════════════════════════════════════════════════
# P0-4 取回工具与路径安全
# ══════════════════════════════════════════════════════

def test_read_tool_result_roundtrip():
    """落盘 → 取回：分页、总行数、has_more 都要对。"""
    registry.ensure_loaded()
    lines = "\n".join(f"line-{i}" for i in range(1000))
    d = tool_results.STORE / "t-rt"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "x-callF.json"
    p.write_text(lines, encoding="utf-8")

    r = json.loads(registry.execute("read_tool_result",
                                    {"path": str(p), "offset": 10, "limit": 5}))
    assert r["total_lines"] == 1000
    assert r["returned"] == 5
    assert r["lines"][0] == "line-10", r["lines"][:2]
    assert r["has_more"] is True

    tail = json.loads(registry.execute("read_tool_result",
                                       {"path": str(p), "offset": 998, "limit": 200}))
    assert tail["returned"] == 2 and tail["has_more"] is False
    tool_results.cleanup("t-rt")
    print("  ✓ 取回分页正确（offset/limit/has_more）")


def test_read_tool_result_blocks_path_escape():
    """★ 安全：path 来自模型输出，必须挡住目录穿越。"""
    registry.ensure_loaded()
    for evil in ["/etc/passwd",
                 str(tool_results.STORE / ".." / ".." / "etc" / "passwd"),
                 str(config.BACKEND_DIR / ".env"),
                 str(config.BACKEND_DIR / "app" / "config.py")]:
        out = json.loads(registry.execute("read_tool_result", {"path": evil}))
        assert "error" in out and "不在允许范围" in out["error"], \
            f"路径逃逸未被拦截：{evil} → {out}"
    print("  ✓ 目录穿越被拦（/etc/passwd、../、.env、源码文件）")


def test_read_missing_file_guides_to_sql():
    """文件被清理后要给出可行的替代路径，而不是干巴巴的报错。"""
    registry.ensure_loaded()
    p = tool_results.STORE / "nope" / "gone.json"
    out = json.loads(registry.execute("read_tool_result", {"path": str(p)}))
    assert "error" in out and "sql_query" in out["error"], out
    print("  ✓ 文件不存在时引导改用 sql_query")


def test_prune_old_removes_stale_only():
    """清理只删过期文件，新文件必须保留。"""
    d = tool_results.STORE / "t-prune"
    d.mkdir(parents=True, exist_ok=True)
    old, new = d / "old.json", d / "new.json"
    old.write_text("x"), new.write_text("y")
    os.utime(old, (time.time() - 99999, time.time() - 99999))
    removed = tool_results.prune_old(max_age_s=3600)
    assert removed >= 1
    assert not old.exists(), "过期文件未删"
    assert new.exists(), "误删了新文件"
    tool_results.cleanup("t-prune")
    print("  ✓ prune 只删过期文件")


# ══════════════════════════════════════════════════════
# P0-4 接线：max_result_chars 必须真正生效
# ══════════════════════════════════════════════════════

def test_per_tool_limit_is_honored():
    """★ 每个工具标注的 max_result_chars 必须被真正使用。

    第 3 步给 18 个工具标了 max_result_chars（get_k8s_resource 8000、
    query_logs 6000…），但 add_tool_result 一直用全局 TOOL_RESULT_MAX_CHARS=2000
    —— 标了却没接线，等于所有标注失效。

    判据设计：造一个大小【介于全局默认与工具上限之间】的结果。
      · 接线正确（limit=8000）→ 未超限 → 原样返回、不落盘
      · 未接线（limit=2000）  → 超限   → 变成预览 + 落盘文件
    用"有没有落盘"来判定比用长度判定可靠 —— 结构预览会把内容压得远小于
    任何 limit，长度反映不出 limit 究竟取了哪个值。
    """
    registry.ensure_loaded()
    spec = registry.get_spec("get_k8s_resource")   # 标注 8000，只读
    assert spec.max_result_chars > config.TOOL_RESULT_MAX_CHARS, \
        "前提变了：该工具的上限不再大于全局默认值，需换一个工具做本用例"

    mid = json.dumps({"pad": "y" * 4000}, ensure_ascii=False)
    assert config.TOOL_RESULT_MAX_CHARS < len(mid) < spec.max_result_chars

    run = RunContext(session_id="s-limit", mode="readonly")
    tc = _FakeToolCall("call-lim", "get_k8s_resource")
    _, ctx = _run_agent_with([tc], run, ["get_k8s_resource"],
                             execute_impl=lambda n, a, run=None: mid)
    content = next(m for m in ctx.messages if m.get("role") == "tool")["content"]

    assert content == mid, (
        f"内容被改写（长度 {len(content)}，原始 {len(mid)}），"
        f"说明仍按全局 {config.TOOL_RESULT_MAX_CHARS} 裁剪而非工具级 "
        f"{spec.max_result_chars}")
    assert not (tool_results.STORE / "s-limit").exists(), \
        "未超工具级上限却落盘了，说明用的是全局默认值"
    print(f"  ✓ {len(mid)} 字符结果在工具级上限 {spec.max_result_chars} 内，"
          f"原样保留（若按全局 2000 会被压成预览）")


def test_oversized_result_persisted_during_run():
    """跑完一轮后，超长结果应真的在盘上，且上下文里只有预览。"""
    registry.ensure_loaded()
    run = RunContext(session_id="s-persist", mode="readonly")
    big = _big_result(500)
    tc = _FakeToolCall("call-per", "query_logs")
    _, ctx = _run_agent_with([tc], run, ["query_logs"],
                                  execute_impl=lambda n, a, run=None: big)
    tool_msg = next(m for m in ctx.messages if m.get("role") == "tool")
    assert len(tool_msg["content"]) < len(big), "上下文里塞了全量结果"
    p = tool_results.STORE / "s-persist" / "query_logs-call-per.json"
    assert p.is_file(), f"未落盘：{p}"
    assert p.read_text(encoding="utf-8") == big, "落盘内容不是原始全量"
    tool_results.cleanup("s-persist")
    print("  ✓ 全量在盘上、上下文只有预览（信息可取回而非丢失）")


# ══════════════════════════════════════════════════════
# P1-1 并行分批
# ══════════════════════════════════════════════════════

def test_partition_keeps_order_and_isolates_writes():
    """连续只读合批；写操作独占一批；整体顺序不变。"""
    registry.ensure_loaded()
    names = ["query_metrics", "query_logs", "patch_deployment",
             "get_topology", "get_risk_report", "create_pdb"]
    batches = _partition([_FakeToolCall(f"c{i}", n) for i, n in enumerate(names)])
    assert [[t.function.name for t in b] for b in batches] == [
        ["query_metrics", "query_logs"],
        ["patch_deployment"],
        ["get_topology", "get_risk_report"],
        ["create_pdb"],
    ], [[t.function.name for t in b] for b in batches]
    flat = [t.function.name for b in batches for t in b]
    assert flat == names, "分批改变了顺序"
    print("  ✓ 分批保序：[读,读][写][读,读][写]")


def test_write_tool_never_batched():
    """★ 破坏性/写类工具绝不能进并行批。"""
    registry.ensure_loaded()
    writers = [t["name"] for t in registry.describe_tools()
               if not t["read_only"]]
    assert writers, "没有非只读工具？前提变了"
    batches = _partition([_FakeToolCall(f"w{i}", n) for i, n in enumerate(writers)])
    assert all(len(b) == 1 for b in batches), \
        f"写类工具被合批：{[[t.function.name for t in b] for b in batches]}"
    print(f"  ✓ {len(writers)} 个写类工具全部单独成批")


def test_unknown_tool_not_batched():
    """未注册工具（拿不到 spec）必须保守处理：不并行。"""
    batches = _partition([_FakeToolCall("u1", "query_logs"),
                          _FakeToolCall("u2", "no_such_tool"),
                          _FakeToolCall("u3", "query_metrics")])
    assert [len(b) for b in batches] == [1, 1, 1], \
        f"未注册工具被合批：{[len(b) for b in batches]}"
    print("  ✓ 未注册工具 fail-closed：不并行")


def test_parallel_actually_concurrent():
    """并行批必须真的并发（总耗时接近单个而非累加）。"""
    registry.ensure_loaded()
    run = RunContext(session_id="s-par", mode="readonly")
    names = ["query_metrics", "query_logs", "get_topology", "get_risk_report"]
    tcs = [_FakeToolCall(f"p{i}", n) for i, n in enumerate(names)]
    inflight = {"max": 0, "cur": 0}
    lock = threading.Lock()

    def slow(name, args, run=None):
        with lock:
            inflight["cur"] += 1
            inflight["max"] = max(inflight["max"], inflight["cur"])
        time.sleep(0.3)
        with lock:
            inflight["cur"] -= 1
        return json.dumps({"tool": name})

    t0 = time.perf_counter()
    _run_agent_with(tcs, run, names, execute_impl=slow)
    elapsed = time.perf_counter() - t0

    assert inflight["max"] >= 2, f"并发度只有 {inflight['max']}，未真正并行"
    assert elapsed < 0.3 * len(names) * 0.8, \
        f"耗时 {elapsed:.2f}s 接近串行（{0.3 * len(names):.1f}s）"
    print(f"  ✓ 4 个只读工具并发度 {inflight['max']}，"
          f"{elapsed:.2f}s（串行需 {0.3 * len(names):.1f}s）")


def test_parallel_results_written_in_original_order():
    """★ 并发执行但写回顺序必须与 tool_calls 一致（可复现性）。

    故意让【后面】的工具先返回：如果按完成顺序写回，上下文顺序就乱了，
    同样的输入会产生不同的上下文，问题无法复现。
    """
    registry.ensure_loaded()
    run = RunContext(session_id="s-order", mode="readonly")
    names = ["query_metrics", "query_logs", "get_topology", "get_risk_report"]
    tcs = [_FakeToolCall(f"o{i}", n) for i, n in enumerate(names)]
    delay = {"query_metrics": 0.35, "query_logs": 0.25,
             "get_topology": 0.15, "get_risk_report": 0.02}

    def staggered(name, args, run=None):
        time.sleep(delay[name])                 # 顺序完全反过来完成
        return json.dumps({"tool": name})

    evs, ctx = _run_agent_with(tcs, run, names, execute_impl=staggered)

    tool_msgs = [m for m in ctx.messages if m.get("role") == "tool"]
    got = [m["name"] for m in tool_msgs]
    assert got == names, f"写回顺序被打乱：{got}"
    # 事件顺序也必须一致
    ev_order = [e["tool"] for e in evs if e["type"] == "tool_result"]
    assert ev_order == names, f"事件顺序被打乱：{ev_order}"
    print("  ✓ 完成顺序完全反转，写回与事件顺序仍与 tool_calls 一致")


def test_parallel_pairing_invariant():
    """并行批后 tool_call/tool_result 配对不变量必须成立。"""
    registry.ensure_loaded()
    run = RunContext(session_id="s-pair", mode="readonly")
    names = ["query_metrics", "query_logs", "get_topology"]
    tcs = [_FakeToolCall(f"q{i}", n) for i, n in enumerate(names)]
    _, ctx = _run_agent_with(tcs, run, names,
                             execute_impl=lambda n, a, run=None: '{"ok":1}')
    ctx.assert_api_invariants()                 # 不抛即通过
    assert len([m for m in ctx.messages if m.get("role") == "tool"]) == 3
    print("  ✓ 并行后 3 个 tool_call 全部配对，API 不变量通过")


def test_parallel_error_isolated():
    """一个并行成员抛异常不应带崩其他成员，且配对仍完整。"""
    registry.ensure_loaded()
    run = RunContext(session_id="s-err", mode="readonly")
    names = ["query_metrics", "query_logs", "get_topology"]
    tcs = [_FakeToolCall(f"e{i}", n) for i, n in enumerate(names)]

    def flaky(name, args, run=None):
        if name == "query_logs":
            return json.dumps({"error": "boom"})
        return json.dumps({"ok": name})

    evs, ctx = _run_agent_with(tcs, run, names, execute_impl=flaky)
    tool_msgs = [m for m in ctx.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 3, f"配对被破坏：{len(tool_msgs)}"
    assert any("boom" in str(m["content"]) for m in tool_msgs)
    assert any('"ok"' in str(m["content"]) for m in tool_msgs), "其他成员被带崩"
    print("  ✓ 单个成员失败被隔离，其余正常，配对完整")


def test_abort_stops_parallel_batch():
    """★ 中断后并行批里尚未动手的成员必须停下。

    并行改造引入的回归：中断检查原本在每个工具前，改成每批前之后，
    一批 N 个工具会在中断后全部跑完。修法是在 worker 真正执行前再查一次。
    """
    registry.ensure_loaded()
    run = RunContext(session_id="s-abort-par", mode="readonly")
    names = ["query_metrics", "query_logs", "get_topology", "get_risk_report"]
    tcs = [_FakeToolCall(f"a{i}", n) for i, n in enumerate(names)]
    ran = []
    lock = threading.Lock()

    def abort_on_first(name, args, run=None):
        with lock:
            ran.append(name)
        run.abort.set()                         # 第一个执行时立刻置位中断
        time.sleep(0.15)
        return json.dumps({"ok": name})

    _, ctx = _run_agent_with(tcs, run, names, execute_impl=abort_on_first)

    assert len(ran) < len(names), f"中断后仍全部执行：{ran}"
    tool_msgs = [m for m in ctx.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == len(names), f"配对被破坏：{len(tool_msgs)}"
    aborted = [m for m in tool_msgs if "中断" in str(m["content"])]
    assert aborted, "未标记中断"
    print(f"  ✓ 中断生效：{len(names)} 个里只跑了 {len(ran)} 个，"
          f"{len(aborted)} 个标记中断，配对完整")


def test_parallel_audit_all_recorded():
    """★ 并行批的审计不能丢：N 个工具就该有 N 条记录。

    这是把审计写入延后到串行阶段的原因 —— 多线程同时写 SQLite 会锁竞争，
    而审计是合规功能，丢记录不可接受。
    """
    registry.ensure_loaded()
    db.execute("DELETE FROM agent_audit")
    run = RunContext(session_id="s-audit-par", mode="readonly")
    names = ["query_metrics", "query_logs", "get_topology", "get_risk_report"]
    tcs = [_FakeToolCall(f"d{i}", n) for i, n in enumerate(names)]
    _run_agent_with(tcs, run, names, execute_impl=lambda n, a, run=None: '{"ok":1}')

    rows = audit.query(session_id="s-audit-par", limit=50)
    got = [r["tool_name"] for r in rows]
    assert len(rows) == len(names), f"审计丢记录：期望 {len(names)} 实际 {len(rows)} → {got}"
    assert all(r["result_status"] == audit.OK for r in rows), got
    print(f"  ✓ 并行 {len(names)} 个工具，审计 {len(rows)} 条齐全（无锁竞争丢失）")


def main():
    db.init_db()
    registry.ensure_loaded()
    groups = [
        ("P0-4 · 结构感知预览", [
            test_preview_keeps_structure_and_total,
            test_preview_shrinks_sample_to_stay_valid_json,
            test_non_json_falls_back_to_truncation,
            test_idempotent_write,
            test_under_limit_returns_unchanged,
        ]),
        ("P0-4 · 取回与路径安全", [
            test_read_tool_result_roundtrip,
            test_read_tool_result_blocks_path_escape,
            test_read_missing_file_guides_to_sql,
            test_prune_old_removes_stale_only,
        ]),
        ("P0-4 · 工具级上限接线", [
            test_per_tool_limit_is_honored,
            test_oversized_result_persisted_during_run,
        ]),
        ("P1-1 · 并行分批", [
            test_partition_keeps_order_and_isolates_writes,
            test_write_tool_never_batched,
            test_unknown_tool_not_batched,
        ]),
        ("P1-1 · 并行执行语义", [
            test_parallel_actually_concurrent,
            test_parallel_results_written_in_original_order,
            test_parallel_pairing_invariant,
            test_parallel_error_isolated,
            test_abort_stops_parallel_batch,
            test_parallel_audit_all_recorded,
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
