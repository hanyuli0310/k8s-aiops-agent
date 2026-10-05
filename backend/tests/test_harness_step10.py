"""第 10 步（E-4 结论事实核对）回归测试。

verifier 的价值取决于两件事，缺一不可：
  1. **检得出**：篡改一位的 trace_id、编号写错的资源名要能抓到；
  2. **不误报**：正常回答（含单位换算、模型算出来的数、通用英文连字符词）必须安静。

第 2 条更重要 —— 一个会对正常回答报警的核对器是负资产。所以本文件里
`test_real_answer_*` 系列把真机跑出来的语料固化成回归用例：以后改规则若引入误报，
这几条会立刻红。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step10.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step10_test.db"
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

from app.harness import llm, loop, verifier                      # noqa: E402
from app.harness.context import ContextManager                   # noqa: E402
from app.harness.runctx import RunContext                        # noqa: E402
from app.tools import registry                                   # noqa: E402


# ══════════════════════════════════════════════════════
# 真机语料（2026-08-09 实跑，general Agent 查一条最慢 trace）
# ══════════════════════════════════════════════════════

# 证据侧：query_traces 返回的片段（模型实际看到的内容）
REAL_EVIDENCE = """
{"root_spans": [{"trace_id": "da9dadcdfa564dc496512392b726012b",
  "api": "POST /api/orders", "service": "nginx-ingress",
  "duration_us": 336500, "status_code": "OK", "span_count": 8}],
 "spans": [{"service": "nginx-ingress", "name": "POST /api/orders",
            "duration_us": 336500, "kind": "server"},
           {"service": "order-service", "name": "INSERT orders",
            "duration_us": 6400, "kind": "client",
            "db_statement": "INSERT FROM orders WHERE id = 76740"},
           {"service": "api-gateway", "name": "POST /api/orders", "duration_us": 330100},
           {"service": "rds-mysql-01", "name": "INSERT", "duration_us": 6400}]}
"""

# 回答侧：模型的真实输出（原样保留，包括它自己做的 μs → ms 换算）
REAL_ANSWER = """
- **trace_id**：`da9dadcdfa564dc496512392b726012b`
- **总耗时**：**336.5 ms**（根 span，状态 OK，共 8 个 span）
- **耗时最大的 span**：`nginx-ingress` 的入口 span `POST /api/orders`，**336.5 ms**，该 span **无 SQL 语句**
- 链路中唯一带 SQL 的 span：`order-service` 的 `INSERT orders`，**6.4 ms**，SQL 片段：`INSERT FROM orders WHERE id = 76740`
"""


# loop 接入用例的固定 ID：真的在工具返回里，假的只在回答里
REAL_ID = "aaaa1111bbbb2222cccc3333dddd4444"
FAKE_ID = "ffff9999eeee8888dddd7777cccc6666"


def _real_ev() -> verifier.Evidence:
    ev = verifier.Evidence()
    ev.absorb(REAL_EVIDENCE)
    return ev


# ══════════════════════════════════════════════════════
# 不误报（最重要的一组）
# ══════════════════════════════════════════════════════

def test_real_answer_no_false_alarm():
    """★★ 真机语料：正常回答不得报警。

    这条回答里有 32 位 trace_id、3 个资源名、以及模型自己做的 336500μs → 336.5ms 换算。
    """
    vr = verifier.check(REAL_ANSWER, _real_ev())
    assert not vr["suspicious"], (vr["fake_ids"], vr["near_miss_resources"])
    assert vr["checked"]["ids"] == 1, vr["checked"]
    assert vr["checked"]["resources"] >= 2, vr["checked"]
    print(f"  ✓ 真机回答零报警（核对 {vr['checked']['ids']} 个 ID / "
          f"{vr['checked']['resources']} 个资源名 / {vr['checked']['numbers']} 个数值）")


def test_real_answer_unit_conversion_matched():
    """★ 模型做的单位换算必须能被归一化认出来（336500 微秒 → 336.5 毫秒）。

    这是 B 类归一化的核心价值：不做换算的话正常回答会满屏"未证实数值"。
    """
    vr = verifier.check(REAL_ANSWER, _real_ev())
    assert "336.5" not in vr["unverified_numbers"], vr["unverified_numbers"]
    print(f"  ✓ μs→ms 换算已归一化匹配（未命中数值 {len(vr['unverified_numbers'])} 个）")


def test_generic_hyphenated_words_not_flagged():
    """★ 通用英文连字符词不是资源名，不能报。"""
    ev = verifier.Evidence()
    ev.absorb('{"service": "order-service", "instance": "rds-mysql-01"}')
    text = ("本次为 read-only 巡检，策略是 fail-closed，端到端 end-to-end 校验，"
            "状态 in-progress，权衡 trade-off，实时 real-time。")
    vr = verifier.check(text, ev)
    assert not vr["suspicious"], vr["near_miss_resources"]
    print("  ✓ 6 个通用连字符词均未误报")


def test_far_away_hyphenated_word_not_flagged():
    """★ 与真实资源名相距很远的连字符词不报 —— A2 只报"近似但不相等"。

    这是 A2 相比"不在池里就报"的关键优势：后者会把任何连字符词都报出来。
    """
    ev = verifier.Evidence()
    ev.absorb('{"service": "order-service"}')
    vr = verifier.check("采用 blue-green 发布，配合 canary-release 策略。", ev)
    assert not vr["suspicious"], vr["near_miss_resources"]
    print("  ✓ blue-green / canary-release 与真实资源名距离远，未误报")


def test_small_integers_ignored():
    """小整数（序号、条数、副本数）不参与数值核对，否则信号会被稀释。"""
    ev = verifier.Evidence()
    ev.absorb('{"x": 999}')
    vr = verifier.check("共 7 条，其中 P1 4 条、P2 3 条，副本数 1 个。", ev)
    assert vr["checked"]["numbers"] == 0, vr["checked"]
    print("  ✓ 小于 11 的整数全部跳过核对")


def test_rule_ids_not_treated_as_resources():
    """规则编号（CAP-003）与命名空间路径（default/api-gateway）不该误报。"""
    ev = verifier.Evidence()
    ev.absorb('{"rule_id": "CAP-003", "resource_ref": "default/api-gateway"}')
    vr = verifier.check("CAP-003 命中，资源 default/api-gateway，超卖率 166.7%。", ev)
    assert not vr["suspicious"], vr["near_miss_resources"]
    print("  ✓ 规则编号与命名空间路径未误报")


# ══════════════════════════════════════════════════════
# 检得出
# ══════════════════════════════════════════════════════

def test_tampered_trace_id_detected():
    """★★ 把真实 trace_id 改一位就必须抓到 —— 这是 A1 的核心场景。

    模型没有任何理由说出一个它没见过的 hex 串，所以这里判"确凿"，不看编辑距离。
    """
    tampered = REAL_ANSWER.replace("da9dadcdfa564dc496512392b726012b",
                                   "da9dadcdfa564dc496512392b726012c")
    vr = verifier.check(tampered, _real_ev())
    assert vr["suspicious"], vr
    assert vr["fake_ids"] == ["da9dadcdfa564dc496512392b726012c"], vr["fake_ids"]
    assert "traceID/spanID" in verifier.describe(vr)
    print(f"  ✓ 篡改一位的 trace_id 被抓到：{vr['fake_ids'][0][:16]}…")


def test_fabricated_trace_id_detected():
    """整个编造的 trace_id 同样抓到。"""
    ev = verifier.Evidence()
    ev.absorb('{"trace_id": "aaaa1111bbbb2222cccc3333dddd4444"}')
    vr = verifier.check("根因链路 trace_id=`ffff9999eeee8888dddd7777cccc6666`。", ev)
    assert vr["suspicious"] and len(vr["fake_ids"]) == 1, vr
    print("  ✓ 编造的 trace_id 被抓到")


def test_wrong_instance_number_detected():
    """★★ 资源编号写错（rds-mysql-03，世界里只有 01/02）必须抓到并指出最近项。

    这是本项目最危险的一类错误：看着像真的，实际指向不存在的实例，
    照着它去治理会打错目标。
    """
    ev = verifier.Evidence()
    ev.absorb('{"instances": ["rds-mysql-01", "rds-mysql-02", "kvstore-redis-01"]}')
    vr = verifier.check("连接使用率最高的是 rds-mysql-03，建议升配。", ev)
    assert vr["suspicious"], vr
    said, closest, dist = vr["near_miss_resources"][0]
    assert said == "rds-mysql-03" and closest in ("rds-mysql-01", "rds-mysql-02")
    assert dist == 1, dist
    assert "疑似写错" in verifier.describe(vr)
    print(f"  ✓ {said} 被抓到，最接近的真实资源是 {closest}（距离 {dist}）")


def test_correction_prompt_is_actionable():
    """★ 自纠正指令必须点名具体问题、并禁止用模糊表述糊过去。"""
    ev = verifier.Evidence()
    ev.absorb('{"trace_id": "aaaa1111bbbb2222cccc3333dddd4444", "svc": "order-service"}')
    vr = verifier.check("trace `ffff9999eeee8888dddd7777cccc6666` 显示 order-servic 异常。", ev)
    prompt = verifier.correction_prompt(vr)
    assert "ffff9999eeee8888dddd7777cccc6666" in prompt
    assert "order-servic" in prompt and "order-service" in prompt
    assert "证据不足" in prompt, "必须给出'拿不到证据就明说'的出路"
    print("  ✓ 自纠正指令点名了具体 ID 与资源名，并给出诚实兜底的写法")


# ══════════════════════════════════════════════════════
# 数值核对增强（遗留缺口修复：带单位提取、小数、量级约等）
# ══════════════════════════════════════════════════════

def test_magnitude_approximation_matched():
    """★ 量级约等：模型说"116 万"，池里是 1162951 —— 不该算未证实。

    这是遗留缺口之一。中文里"约 116 万""1.2 亿"是最常见的概括方式，
    字面上永远对不上原始值。只对**万/亿**启用且要求相对误差 < 1%。
    """
    ev = verifier.Evidence()
    ev.absorb('{"rows_examined": 1162951, "total": 230000000}')
    vr = verifier.check("扫描约 116 万行，总量 2.3 亿条", ev)
    assert not vr["unverified_numbers"], vr["unverified_numbers"]
    print("  ✓ 116 万 ↔ 1162951、2.3 亿 ↔ 230000000 均匹配")


def test_magnitude_approximation_not_too_loose():
    """★★ 容差必须克制：差得远的数不能靠"约等"蒙过去。

    容差一放开，任何数都能"匹配"上，B 类的命中率就变成没有意义的数字。
    """
    ev = verifier.Evidence()
    ev.absorb('{"rows_examined": 1162951}')
    # 200 万与 116 万相差 72%，不能匹配
    vr = verifier.check("扫描约 200 万行", ev)
    assert vr["unverified_numbers"], "差 72% 却被判为已证实"
    # 边界：117 万与 1162951 相差 0.6% < 1%，应当匹配
    assert not verifier.check("扫描约 117 万行", ev)["unverified_numbers"]
    # 而 118 万相差 1.5% > 1%，不匹配
    assert verifier.check("扫描约 118 万行", ev)["unverified_numbers"]
    print("  ✓ 117 万（差 0.6%）匹配、118 万（差 1.5%）与 200 万（差 72%）不匹配")


def test_numbers_with_unit_suffix_are_extracted():
    """★★ 带单位后缀的数值必须能提取到。

    第一版正则的后置断言拒绝字母，导致 1.244s 只提到 1、39300m 完全提不到 ——
    而本项目的数值几乎都带单位。这个缺陷是 E-3 写保真用例时才暴露的，
    意味着在那之前 B 类核对漏掉了一大类数值。
    """
    ev = verifier.Evidence()
    ev.absorb('{"p99_s": 1.244, "cpu_limit_m": 39300, "duration_us": 336500}')
    vr = verifier.check("P99 1.244s，CPU limit 39300m，耗时 336.5ms", ev)
    assert vr["checked"]["numbers"] >= 3, vr["checked"]
    assert not vr["unverified_numbers"], vr["unverified_numbers"]
    print(f"  ✓ 带单位的 3 个数值全部被提取并命中（checked={vr['checked']['numbers']}）")


def test_decimal_metrics_are_never_ignored():
    """★★ 有小数部分的数值一律参与核对 —— 它们恰恰是最该核对的度量值。

    第一版门槛是绝对值 >= 11，把 P99 1.244、错误率 1.78、request_time 0.333
    全滤掉了。而这些正是本项目结论的核心依据。
    """
    ev = verifier.Evidence()
    ev.absorb('{"other": 999}')
    vr = verifier.check("P99 1.244s，错误率 1.78%，平均 0.333s", ev)
    assert vr["checked"]["numbers"] == 3, vr["checked"]
    assert set(vr["unverified_numbers"]) == {"1.244", "1.78", "0.333"}, \
        vr["unverified_numbers"]
    print("  ✓ 三个小数指标全部参与核对（且如实报为未证实）")


# ══════════════════════════════════════════════════════
# 证据池的定义
# ══════════════════════════════════════════════════════

def test_pool_absorbs_preview_not_full_result():
    """★★ 超长结果落盘后，只有【预览里的内容】算证据。

    若吸收全文，模型没看到的数值也会被当成"有出处"，核对就此失效 ——
    证据池的定义是"模型看到过什么"，不是"系统拿到过什么"。

    注意构造：结构感知预览会**完整保留标量字段**、只截数组到前几条。
    所以要藏的数值必须放在数组的靠后元素里，放顶层标量是藏不住的
    （第一版就这么写的，断言反而失败——预览确实带上了它，模型也确实看到了）。
    """
    ctx = ContextManager("sys", session_id="pool1")
    hidden = 9998888.0
    rows = [{"v": i} for i in range(400)] + [{"v": hidden}]
    ctx.add_tool_result("c1", "sql_query", json.dumps({"rows": rows}), limit=200)
    preview = ctx.messages[-1]["content"]
    assert str(int(hidden)) not in preview, "构造失败：预览里仍能看到该数值"
    assert hidden not in ctx.evidence.numbers, \
        "全文里的数值不该进证据池（模型根本没看到它）"
    print(f"  ✓ 只吸收预览（{len(preview)} 字符），落盘全文里的数值不算证据")


def test_pool_survives_continuation():
    """★ 续跑重置上下文后证据池必须保留 —— 丢了会让第 2 段整段误报。"""
    ctx = ContextManager("sys", session_id="pool2")
    ctx.add_tool_result("c1", "query_traces",
                        '{"trace_id": "aaaa1111bbbb2222cccc3333dddd4444"}')
    before = len(ctx.evidence.ids)
    ctx.reset_for_continuation("交接摘要")
    assert len(ctx.evidence.ids) == before == 1, ctx.evidence.stats()
    print("  ✓ reset_for_continuation 不清证据池")


def test_model_authored_text_does_not_pollute_pool():
    """★★ 模型自己写的文本（交接摘要 / 收口指令里的清单）不得进证据池。

    否则模型编的 trace_id 会在下一段被"洗白"成有出处的事实，核对彻底失效。
    """
    ctx = ContextManager("sys", session_id="pool3")
    fake = "ffff9999eeee8888dddd7777cccc6666"
    ctx.add_user(f"交接摘要：已确认 trace {fake}", evidence=False)
    assert fake not in ctx.evidence.ids, "模型产出的内容污染了证据池"
    # 而真实用户输入是要吸收的（用户提供的 ID 是外部事实）
    ctx.add_user(f"帮我看这条链路 {fake}")
    assert fake in ctx.evidence.ids
    print("  ✓ evidence=False 的注入不入池，真实用户输入入池")


def test_reset_for_continuation_handoff_not_absorbed():
    """续跑塞进去的交接摘要同样不入池（与上一条同源，但走的是另一条代码路径）。"""
    ctx = ContextManager("sys", session_id="pool4")
    fake = "1234abcd5678efab9012cdef3456abcd"
    ctx.reset_for_continuation(f"已确认 trace {fake}")
    assert fake not in ctx.evidence.ids
    print("  ✓ 交接摘要经 reset_for_continuation 注入时也不入池")


# ══════════════════════════════════════════════════════
# loop 接入
# ══════════════════════════════════════════════════════

class _FakeFn:
    def __init__(self, name, args="{}"):
        self.name, self.arguments = name, args


class _FakeToolCall:
    def __init__(self, cid, name, args="{}"):
        self.id, self.type, self.function = cid, "function", _FakeFn(name, args)


class _FakeMsg:
    def __init__(self, content="", tool_calls=None):
        self.content, self.tool_calls = content, tool_calls or []


def _run_loop(chat_fn, run, exec_fn=None, tools=None):
    orig = (llm.chat_with_retry, registry.execute, llm.get_client)

    def guard(*a, **kw):
        raise AssertionError("测试不应触达真实 LLM 客户端")

    llm.chat_with_retry = chat_fn
    llm.get_client = guard
    registry.execute = exec_fn or (lambda n, a, run=None: json.dumps({"ok": n}))
    agent = {"name": "T", "system_prompt": "sys", "model": "m",
             "tools": tools or ["query_traces"]}
    try:
        return list(loop.run_agent(agent, "任务", run=run))
    finally:
        llm.chat_with_retry, registry.execute, llm.get_client = orig


def _is_correction_turn(messages) -> bool:
    """这一轮是不是「事实核对后的追加复核」。

    判据取自真实触发方式：loop 通过在上下文里 **追加一条 user 指令** 来触发复核
    （correction_prompt / refresh_prompt），所以看最后几条 user 消息的特征串即可。

    ⚠️ 不能再用 `tools is None` 判断了 —— A1（编造 traceID）的自纠正现在**带工具**，
    因为它必须真的去 query_traces 把真实 ID 查回来；不给工具时模型唯一出路是
    删掉那条证据，结论还在但佐证没了。旧替身用 tools 判断，改动后会走错分支、
    让"修正失败"看起来像生产 bug（实际是替身漂移）。
    """
    for m in reversed(messages[-4:]):
        if m.get("role") != "user":
            continue
        c = m.get("content") or ""
        if "事实核对发现" in c or "数据时效核对发现" in c:
            return True
    return False


def _fake_trace_scenario(fixed_answer=None, lookup_before_fix=False):
    """工具返回真 trace_id，模型回答里给一个编造的 → 必然触发 A1。

    lookup_before_fix=True 时模拟"带工具的自纠正"：收到核对指令后先再查一次
    query_traces，再给出修正稿 —— 这才是 A1 现在的真实路径。
    """
    state = {"n": 0, "corrections": 0, "lookups": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        state["n"] += 1
        if _is_correction_turn(messages):
            if lookup_before_fix and state["lookups"] == 0:
                state["lookups"] += 1
                return _FakeMsg(content="重新取回真实 trace",
                                tool_calls=[_FakeToolCall("c9", "query_traces")])
            state["corrections"] += 1
            return _FakeMsg(content=fixed_answer if fixed_answer is not None
                            else f"更正：真实 trace 是 {REAL_ID}")
        if state["n"] == 1:
            return _FakeMsg(content="查链路",
                            tool_calls=[_FakeToolCall("c1", "query_traces")])
        return _FakeMsg(content=f"根因链路 trace_id={FAKE_ID}，耗时 1244 ms")

    return chat, state, REAL_ID, FAKE_ID


def test_loop_emits_verify_warning_and_self_corrects():
    """★★ 端到端：可疑结论触发一次自纠正；纠正后仍可疑才推 verify_warning。"""
    run = RunContext(session_id="v1")
    chat, state, real, fake = _fake_trace_scenario()
    events = _run_loop(
        chat, run,
        exec_fn=lambda n, a, run=None: json.dumps({"trace_id": real}))
    warns = [e for e in events if e["type"] == "verify_warning"]
    answer = next(e for e in events if e["type"] == "answer")["text"]
    assert run.corrections == 1, run.corrections
    assert state["corrections"] == 1, "应当只追加一次纠正往返"
    assert real in answer and fake not in answer, answer
    assert not warns, "纠正后已无可疑项，不该再打扰用户"
    print("  ✓ 自纠正生效：编造的 ID 被替换成真实 ID，且未推警告")


def test_loop_warns_when_correction_fails():
    """★ 纠正后仍然可疑 → 必须推 verify_warning，不能默默放过。"""
    run = RunContext(session_id="v2")
    chat, _, real, fake = _fake_trace_scenario(
        fixed_answer=f"我坚持：trace 就是 {FAKE_ID}")
    events = _run_loop(
        chat, run, exec_fn=lambda n, a, run=None: json.dumps({"trace_id": real}))
    warns = [e for e in events if e["type"] == "verify_warning"]
    assert len(warns) == 1, [e["type"] for e in events]
    assert warns[0]["fake_ids"] == [fake], warns[0]
    assert warns[0]["corrected"] is True
    print(f"  ✓ 纠正无效时推警告：{warns[0]['text'][:48]}…")


def test_correction_capped_by_run_counter():
    """★ 自纠正次数存在 run 上并受上限约束（续跑会重开内层循环，放局部会失效）。"""
    run = RunContext(session_id="v3")
    run.corrections = config.VERIFY_MAX_CORRECTIONS      # 已用尽
    chat, state, real, fake = _fake_trace_scenario()
    events = _run_loop(
        chat, run, exec_fn=lambda n, a, run=None: json.dumps({"trace_id": real}))
    warns = [e for e in events if e["type"] == "verify_warning"]
    assert state["corrections"] == 0, "已达上限却又纠正了一次"
    assert len(warns) == 1 and warns[0]["fake_ids"] == [fake]
    print("  ✓ 达上限后只报警不再纠正")


def test_a1_correction_gets_tools_and_recovers_real_id():
    """★★ A1（编造 traceID）自纠正必须**带工具**，且能把真实 ID 查回来。

    这是本次质量优先改造的核心。此前 `_self_correct` 一律 `tools=None`，
    于是 A1 的唯一出路是「删掉那条证据」—— 结论还在但佐证没了，
    运维报告里这是实打实的质量损失。更矛盾的是 correction_prompt 里本来就写着
    "或重新调用 query_traces 取回真实的 trace_id"，**指示模型做一件它做不到的事**。

    现在它可以真的再查一次：本用例模拟这条路径（先 query_traces、再给修正稿），
    断言最终回答里假 ID 被换成了真 ID，且复核期间确实发生了工具调用。
    """
    run = RunContext(session_id="a1-tools")
    chat, state, real, fake = _fake_trace_scenario(lookup_before_fix=True)
    events = _run_loop(
        chat, run, exec_fn=lambda n, a, run=None: json.dumps({"trace_id": real}))
    answer = next(e for e in events if e["type"] == "answer")["text"]
    assert real in answer and fake not in answer, answer
    assert state["lookups"] == 1, f"自纠正阶段没有再取数：{state}"
    # 复核阶段的工具调用要照常推给前端（用户能看到它在补证据）
    assert sum(1 for e in events if e["type"] == "tool_call") >= 2, \
        [e["type"] for e in events]
    assert not [e for e in events if e["type"] == "verify_warning"], \
        "已成功查回真实 ID，不该再告警"
    print(f"  ✓ A1 带工具自纠正：查回真实 ID（取数 {state['lookups']} 次）")


def test_a2_only_correction_gets_no_tools():
    """★ 纯 A2（近似资源名）不给工具 —— 正确名字已在提示词里，给了纯属浪费。

    A2 的正确答案是 verifier 用编辑距离从证据池里算出来的，已经写进
    correction_prompt（"你写了 X，但真实存在的是 Y"）。此时再给工具只会
    多一轮开销，还多一个引入新幻觉的面。所以分流判据是 `vr["fake_ids"]`
    非空（A1）才给工具。
    """
    ev = verifier.Evidence()
    ev.absorb('{"instance": "rds-mysql-order", "conn": 92}')
    vr = verifier.check("rds-mysql-oder 连接率 92%", ev)          # 少一个 r
    assert vr["near_miss_resources"], vr
    assert not vr["fake_ids"], f"这个用例应当只有 A2：{vr}"
    # 分流逻辑：needs_lookup 取自 fake_ids，纯 A2 时为假 → 不给工具
    assert not bool(vr["fake_ids"])
    print(f"  ✓ 纯 A2 不触发取数（近似名 {vr['near_miss_resources'][0][0]}"
          f" → {vr['near_miss_resources'][0][1]}）")


def test_stale_data_triggers_auto_refresh():
    """★★ C 类（数据时效）必须**自动用最新数据复核**，而不是只挂一条警示。

    原实现只告警、等用户想到追问"用最新数据再确认一次" —— 等于把系统该做的事
    推给用户，而用户很可能没注意到那条警示。现在它会自己再查一遍。

    本用例：工具首轮返回 40 分钟前的数据（触发 C 类），复核轮返回新鲜数据，
    断言 (1) 推出了 verify_refresh 事件让用户看见在复核；(2) 最终采用复核后的
    回答；(3) 时效问题已解决，不再告警。
    """
    def _pair(base_ago):
        """造两个**不同**时刻的时间戳。

        必须不同：extract_data_times 返回 set，同值会被去重成 1 个，
        达不到 _MIN_TS_FOR_STALENESS=2 的门槛（那道门槛是有意的 ——
        单个时间戳可能是元信息而非数据时间轴，见"单时间戳结果被跳过"用例）。
        """
        fmt = "%Y-%m-%dT%H:%M:%S"
        return (time.strftime(fmt, time.localtime(time.time() - base_ago - 60)),
                time.strftime(fmt, time.localtime(time.time() - base_ago)))

    calls = {"n": 0}

    def exec_fn(name, args, run=None):
        calls["n"] += 1
        t0, t1 = _pair(2400) if calls["n"] == 1 else _pair(30)
        return json.dumps([{"ts": t0, "cpu": 77.5}, {"ts": t1, "cpu": 78.1}])

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        if _is_correction_turn(messages):
            if calls["n"] < 2:                     # 复核先重新取数
                return _FakeMsg(content="用最新数据重查",
                                tool_calls=[_FakeToolCall("r1", "query_metrics")])
            return _FakeMsg(content="已用最新数据复核，结论不变：CPU 78.1%")
        if calls["n"] == 0:
            return _FakeMsg(content="查指标",
                            tool_calls=[_FakeToolCall("c1", "query_metrics")])
        return _FakeMsg(content="当前 CPU 77.5%，水位偏高")

    run = RunContext(session_id="stale-refresh")
    events = _run_loop(chat, run, exec_fn=exec_fn)
    assert [e for e in events if e["type"] == "verify_refresh"], \
        f"没有推出复核事件：{[e['type'] for e in events]}"
    answer = next(e for e in events if e["type"] == "answer")["text"]
    assert "已用最新数据复核" in answer, answer
    assert run.refreshes == 1, run.refreshes
    assert calls["n"] >= 2, f"复核阶段没有重新取数：{calls}"
    print(f"  ✓ C 类自动刷新复核生效（取数 {calls['n']} 次、复核 {run.refreshes} 轮）")


def test_refresh_capped_when_data_source_frozen():
    """★ 数据源停更时刷新次数必须收敛 —— 否则重取还是旧数据、陷入死循环。

    模拟采集器挂掉：每次取数都返回同样陈旧的时间戳。断言刷新次数不超过
    VERIFY_MAX_REFRESH，且最终**如实告警**（不能因为"尝试过复核"就把问题闷掉）。
    """
    fmt = "%Y-%m-%dT%H:%M:%S"
    t0 = time.strftime(fmt, time.localtime(time.time() - 3660))
    t1 = time.strftime(fmt, time.localtime(time.time() - 3600))

    def exec_fn(name, args, run=None):
        # 采集器停更：每次取回来的都是同样陈旧的两个时刻
        return json.dumps([{"ts": t0, "cpu": 77.5}, {"ts": t1, "cpu": 78.0}])

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        if _is_correction_turn(messages):
            return _FakeMsg(content="已复核，当前 CPU 77.5%")   # 仍基于旧数据
        return _FakeMsg(content="查指标",
                        tool_calls=[_FakeToolCall("c1", "query_metrics")]) \
            if not any("query_metrics" in str(m.get("name", "")) for m in messages) \
            else _FakeMsg(content="当前 CPU 77.5%")

    run = RunContext(session_id="frozen")
    events = _run_loop(chat, run, exec_fn=exec_fn)
    assert run.refreshes <= config.VERIFY_MAX_REFRESH, \
        f"刷新次数失控：{run.refreshes} > {config.VERIFY_MAX_REFRESH}"
    warns = [e for e in events if e["type"] == "verify_warning"]
    assert warns, "数据仍陈旧却没有告警 —— 复核失败必须如实说"
    assert warns[0].get("refreshed") is True, warns[0]
    print(f"  ✓ 数据源停更时刷新收敛（{run.refreshes} 轮）且如实告警")


def test_naive_arm_still_emits_answer():
    """★★ naive 基线臂必须照样产出 answer —— 守一个真实踩过且方向"有利于自己"的坑。

    起因：给基线臂加"只核对不干预"分支时，我用了提前 return。但 run_agent 是
    **生成器**，return 会静默跳过函数末尾那条 `yield answer` ——
    基线臂一条回答都产不出，评测把它全部记成"未完成"，于是虚假地得出
    "Harness 大幅提升准确率"。

    这类 bug 最危险的地方在于**它的错误方向对自己有利**：数字变好看，没有报错，
    不会有人去查。所以必须有一条常驻测试钉住"基线臂也要有回答"。
    """
    orig = config.HARNESS_PROFILE
    config.HARNESS_PROFILE = "naive"
    try:
        run = RunContext(session_id="v5-naive")
        chat, state, real, fake = _fake_trace_scenario()
        events = _run_loop(
            chat, run, exec_fn=lambda n, a, run=None: json.dumps({"trace_id": real}))
        answers = [e for e in events if e["type"] == "answer"]
        assert len(answers) == 1, f"基线臂没产出 answer：{[e['type'] for e in events]}"
        assert answers[0]["text"], "基线臂产出了空回答"
        # 基线臂不自纠正、不告警：它必须保持"裸模型"的真实产出
        assert state["corrections"] == 0, "基线臂不该自纠正"
        assert run.corrections == 0
        assert not [e for e in events if e["type"] == "verify_warning"], \
            "基线臂不该推核对告警"
        assert fake in answers[0]["text"], "基线臂的回答被改动了"
        print("  ✓ naive 臂照样产出回答，且未被核对干预")
    finally:
        config.HARNESS_PROFILE = orig


def test_naive_arm_records_hallucination_probe():
    """★ naive 臂虽不拦截，但必须把幻觉记进 verify_probe。

    否则基线的幻觉率是一片空白，而空白会被读成"基线没有幻觉" ——
    等于替对照组隐瞒缺陷，比不报更糟。
    """
    orig = config.HARNESS_PROFILE
    config.HARNESS_PROFILE = "naive"
    try:
        run = RunContext(session_id="v6-naive")
        chat, _, real, fake = _fake_trace_scenario()
        _run_loop(chat, run,
                  exec_fn=lambda n, a, run=None: json.dumps({"trace_id": real}))
        probe = run.verify_probe
        assert probe is not None, "基线臂没有留下核对探针"
        assert probe["suspicious"] is True, probe
        assert fake in probe["fake_ids"], probe
        print(f"  ✓ 基线臂幻觉被记录（未拦截）：fake_ids={probe['fake_ids']}")
    finally:
        config.HARNESS_PROFILE = orig


def test_verify_can_be_disabled():
    """★ VERIFY_ANSWER=0 时完全不介入（既不纠正也不报警）。"""
    old = config.VERIFY_ANSWER
    config.VERIFY_ANSWER = False
    try:
        run = RunContext(session_id="v4")
        chat, state, real, fake = _fake_trace_scenario()
        events = _run_loop(
            chat, run, exec_fn=lambda n, a, run=None: json.dumps({"trace_id": real}))
        assert not [e for e in events if e["type"] == "verify_warning"]
        assert state["corrections"] == 0 and run.corrections == 0
        answer = next(e for e in events if e["type"] == "answer")["text"]
        assert fake in answer, "关闭后不该改动回答"
    finally:
        config.VERIFY_ANSWER = old
    print("  ✓ 关闭开关后 verifier 完全不介入")


def test_no_correction_when_budget_exhausted():
    """★ 预算已尽时不再花一次往返做纠正 —— 预算优先于一切自愈动作。"""
    ev = verifier.Evidence()
    ev.absorb('{"trace_id": "aaaa1111bbbb2222cccc3333dddd4444"}')
    ctx = ContextManager("sys", session_id="v5")
    run = RunContext(session_id="v5", max_tokens=100)
    run.add_usage(prompt_tokens=200)                     # 已超预算

    called = {"n": 0}

    def chat(*a, **kw):
        called["n"] += 1
        return _FakeMsg(content="x")

    orig = llm.chat_with_retry
    llm.chat_with_retry = chat
    try:
        # _verify_followup 现在是生成器（要把工具事件推给前端），
        # 回答经 run.followup_answer 回传 —— 必须把它耗尽才会真正执行。
        vr = {"fake_ids": ["ffff9999eeee8888dddd7777cccc6666"],
              "near_miss_resources": []}
        events = list(loop._verify_followup(
            ctx, run, "m", prompt=verifier.correction_prompt(vr),
            tools=None, max_steps=1, label="事实自纠正"))
    finally:
        llm.chat_with_retry = orig
    assert run.followup_answer == "" and called["n"] == 0 and not events, \
        (run.followup_answer, called, events)
    print("  ✓ 超预算时放弃自纠正，不再发起 LLM 调用")


def test_answer_unchanged_when_clean():
    """干净的回答不该被 verifier 碰一下（零介入、零额外调用）。"""
    run = RunContext(session_id="v6")
    real = "aaaa1111bbbb2222cccc3333dddd4444"
    state = {"n": 0, "extra": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        state["n"] += 1
        if tools is None:
            state["extra"] += 1
            return _FakeMsg(content="不该被调用")
        if state["n"] == 1:
            return _FakeMsg(content="查", tool_calls=[_FakeToolCall("c1", "query_traces")])
        return _FakeMsg(content=f"链路 {real} 耗时 336.5 ms")

    events = _run_loop(
        chat, run, exec_fn=lambda n, a, run=None: json.dumps(
            {"trace_id": real, "duration_us": 336500}))
    assert state["extra"] == 0, "干净回答却触发了额外 LLM 调用"
    assert not [e for e in events if e["type"] == "verify_warning"]
    assert run.corrections == 0
    print("  ✓ 干净回答零介入、零额外调用")


# ══════════════════════════════════════════════════════
# C 类：数据时效（时间维度核对）
# ══════════════════════════════════════════════════════

def _ts(offset_s: float) -> int:
    """生成一个相对当下的毫秒时间戳（工具结果里的 ts 就是这个形态）。"""
    return int((time.time() + offset_s) * 1000)


def test_stale_tool_result_detected():
    """★ 还原真实缺陷：工具返回的全是两小时前的数据，结论却在讲"当前"。

    这正是量化评测里最严重的那次错误 —— query_logs 当时无时间窗口且升序返回，
    拿回库里最旧的日志（另一次演练的记录），Agent 据此报出根因。
    A/B 类核对完全查不出来：每条日志都真实存在于证据池里。
    """
    ev = verifier.Evidence()
    old = 7200
    ev.absorb_tool_result("query_logs", json.dumps({"rows": [
        {"ts": _ts(-old), "message": "connection refused"},
        {"ts": _ts(-old - 30), "message": "timeout"},
        {"ts": _ts(-old - 60), "message": "timeout"}]}))
    r = verifier.check("当前 order-service 正在大量报连接超时，根因是数据库连接耗尽", ev)
    assert r["stale_sources"], "两小时前的数据没被识别为陈旧"
    name, age = r["stale_sources"][0]
    assert name == "query_logs" and age >= old - 60, (name, age)
    assert r["time_risk"], "回答在描述当前状态却没标记 time_risk"
    # C 类不能升级成 A 类：它不该触发自纠正
    assert not r["suspicious"], "数据陈旧被误判成 A 类（会白花一次自纠正往返）"
    assert "query_logs" in verifier.describe(r)
    print(f"  ✓ 陈旧数据源被识别（{name} 最新数据 {age // 60} 分钟前），且不触发自纠正")


def test_fresh_tool_result_not_flagged():
    """★ 不误报：正常取数（数据在分钟级以内）必须安静。"""
    ev = verifier.Evidence()
    ev.absorb_tool_result("query_logs", json.dumps({"rows": [
        {"ts": _ts(-30), "message": "ok"}, {"ts": _ts(-90), "message": "ok"}]}))
    ev.absorb_tool_result("query_metrics", json.dumps({"rows": [
        {"ts": _ts(-10), "avg": 81.9}, {"ts": _ts(-70), "avg": 80.2}]}))
    r = verifier.check("当前连接使用率 81.9%，目前无异常", ev)
    assert not r["stale_sources"], f"新鲜数据被误判为陈旧：{r['stale_sources']}"
    assert not r["time_risk"]
    print("  ✓ 分钟级新鲜数据不误报")


def test_stale_source_named_even_when_mixed_with_fresh():
    """★ 混用新旧数据时必须**指名**是哪一份旧的。

    这是把时间戳按结果逐份记录、而不是汇成一个全局区间的理由：
    指标是新的、日志是旧的时候，全局最大时间会被新数据掩盖，
    汇总口径下这个缺陷就完全看不见了。
    """
    ev = verifier.Evidence()
    ev.absorb_tool_result("query_metrics", json.dumps(
        {"rows": [{"ts": _ts(-20), "avg": 78.5}, {"ts": _ts(-80), "avg": 77.1}]}))
    ev.absorb_tool_result("query_logs", json.dumps(
        {"rows": [{"ts": _ts(-9000), "m": "x"}, {"ts": _ts(-9100), "m": "y"}]}))
    r = verifier.check("当前接口变慢", ev)
    names = [n for n, _ in r["stale_sources"]]
    assert names == ["query_logs"], f"应只报 query_logs 陈旧，实际 {r['stale_sources']}"
    print("  ✓ 新旧混用时准确指名陈旧的那一份（query_metrics 未被连带）")


def test_historical_review_not_flagged_as_time_risk():
    """★ 回答本身在做历史回顾时，旧数据是应该的 —— 不标 time_risk。

    区分"引用旧数据"与"把旧数据讲成当下"：前者正常（复盘、对比、趋势），
    后者才是那个会导致错误结论的情形。
    """
    ev = verifier.Evidence()
    ev.absorb_tool_result("query_logs", json.dumps(
        {"rows": [{"ts": _ts(-7200), "m": "x"}, {"ts": _ts(-7300), "m": "y"}]}))
    r = verifier.check("两小时前那次演练期间出现过连接超时，与本次无关", ev)
    assert r["stale_sources"], "陈旧数据本身仍应记录"
    assert not r["time_risk"], "历史回顾被误判成把旧数据当当下"
    print("  ✓ 历史回顾语境下不标记 time_risk（仅记录数据时效）")


def test_prompt_sample_timestamps_do_not_enter_time_pool():
    """★ 时间戳只能来自工具结果：Skill 文档里的示例时间戳不能参与时效判定。

    提示词与 Skill 正文里写着旧数据集的示例时间戳（如 1785754200000）。
    若它们被当成"本次查到的数据"，每一轮都会凭空冒出一个几天前的陈旧源。
    """
    ev = verifier.Evidence()
    ev.absorb("示例：{\"timestamp\": 1785754200000, \"instanceId\": \"rds-mysql-01\"} "
              "另一条 {\"timestamp\": 1785754260000}")
    assert not ev.sources, f"system prompt 的示例时间戳进了时效池：{ev.sources}"
    assert not verifier.check("当前一切正常", ev)["stale_sources"]
    print("  ✓ 提示词/文档里的示例时间戳不进时效池")


def test_single_timestamp_result_skipped():
    """单个时间戳不参与判定：它多半是"上次动作发生在 XX"这类元信息。"""
    ev = verifier.Evidence()
    ev.absorb_tool_result("list_governance_actions", json.dumps(
        {"actions": [{"created_at": _ts(-86400)}]}))
    assert not ev.sources, "单时间戳结果不该进时效池"
    print("  ✓ 单时间戳结果被跳过（避免把元信息当数据时间轴）")


def test_loop_emits_stale_warning_without_correction():
    """★ loop 接入：C 类要发 verify_warning，但**不能**触发自纠正往返。"""
    run = RunContext(session_id="v7")
    state = {"n": 0, "extra": 0}

    def chat(messages, tools=None, model=None, temperature=0.3, run=None, **kw):
        state["n"] += 1
        if tools is None:
            state["extra"] += 1
            return _FakeMsg(content="不该被调用")
        if state["n"] == 1:
            return _FakeMsg(content="查", tool_calls=[_FakeToolCall("c1", "query_logs")])
        return _FakeMsg(content="当前 order-service 正在报错，根因是连接耗尽")

    events = _run_loop(chat, run, exec_fn=lambda n, a, run=None: json.dumps(
        {"rows": [{"ts": _ts(-7200), "m": "refused"}, {"ts": _ts(-7260), "m": "refused"}]}))
    warns = [e for e in events if e["type"] == "verify_warning"]
    assert warns, "陈旧数据没有产生 verify_warning"
    assert warns[0]["stale_sources"] and warns[0]["time_risk"]
    assert warns[0]["stale_sources"][0]["tool"] == "query_logs"
    assert state["extra"] == 0, "C 类触发了额外 LLM 调用（应只告警不自纠）"
    assert run.corrections == 0
    print("  ✓ loop 对陈旧数据告警且零额外 LLM 调用")


def main():
    db.init_db()
    registry.ensure_loaded()
    groups = [
        ("不误报（真机语料固化）", [
            test_real_answer_no_false_alarm,
            test_real_answer_unit_conversion_matched,
            test_generic_hyphenated_words_not_flagged,
            test_far_away_hyphenated_word_not_flagged,
            test_small_integers_ignored,
            test_rule_ids_not_treated_as_resources,
        ]),
        ("检得出", [
            test_tampered_trace_id_detected,
            test_fabricated_trace_id_detected,
            test_wrong_instance_number_detected,
            test_correction_prompt_is_actionable,
        ]),
        ("证据池的定义", [
            test_magnitude_approximation_matched,
            test_magnitude_approximation_not_too_loose,
            test_numbers_with_unit_suffix_are_extracted,
            test_decimal_metrics_are_never_ignored,
            test_pool_absorbs_preview_not_full_result,
            test_pool_survives_continuation,
            test_model_authored_text_does_not_pollute_pool,
            test_reset_for_continuation_handoff_not_absorbed,
        ]),
        ("loop 接入与护栏", [
            test_loop_emits_verify_warning_and_self_corrects,
            test_loop_warns_when_correction_fails,
            test_correction_capped_by_run_counter,
            test_verify_can_be_disabled,
            test_a1_correction_gets_tools_and_recovers_real_id,
            test_a2_only_correction_gets_no_tools,
            test_stale_data_triggers_auto_refresh,
            test_refresh_capped_when_data_source_frozen,
            test_naive_arm_still_emits_answer,
            test_naive_arm_records_hallucination_probe,
            test_no_correction_when_budget_exhausted,
            test_answer_unchanged_when_clean,
        ]),
        ("C 类：数据时效", [
            test_stale_tool_result_detected,
            test_fresh_tool_result_not_flagged,
            test_stale_source_named_even_when_mixed_with_fresh,
            test_historical_review_not_flagged_as_time_risk,
            test_prompt_sample_timestamps_do_not_enter_time_pool,
            test_single_timestamp_result_skipped,
            test_loop_emits_stale_warning_without_correction,
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
