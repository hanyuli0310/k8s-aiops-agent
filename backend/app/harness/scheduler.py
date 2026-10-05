"""调度器：顶层入口 handle_message() —— 把一条用户消息变成事件流。

## 两种调度方式（config.AGENT_ROUTING）

**model（默认）**：不做意图分类，用户请求直接交给编排 Agent（持有全部工具
+ dispatch_agent），由模型自己决定查什么、派几个子 Agent、并行还是串行。

**intent（旧路径，仍可切回）**：先把请求对齐到 8 个预制意图，再按 INTENT_AGENT
硬映射选一个 Agent，full_checkup 走写死的三阶段串行。

为何把默认改成 model：预制意图覆盖不住真实请求的分布（"先看拓扑再对比两个库
的水位" 归不到任何一个）；而即使分对了，"派几个 Agent、串行还是并行" 也是人写死的 ——
模型本来有能力判断这两件事。对齐 Claude Code：它没有意图分类层，
用户输入 + 全部工具 + 提示词直接交给主模型编排。

model 模式下意图并未完全下岗，而是**降级为纯关键词、只做两件事**：
前端展示标签、判定要不要写长期记忆。这正好省掉一次快模型往返。

无 LLM 时无论哪种模式都走关键词意图 + 脚本化流程（保证离线可演示）。
"""
from __future__ import annotations

import json
import logging

from .. import config
from ..agents.base import build_agent
from ..tools import registry
from . import intent as intent_mod
from . import llm, memory

logger = logging.getLogger(__name__)

# 仅 AGENT_ROUTING=intent 时使用。model 模式下调度权在模型手里，
# 这张表不参与决策。
INTENT_AGENT = {
    "data_ingest": "data",
    "data_query": "general",
    "topology": "topology",
    "risk_scan": "risk",
    "rule_create": "risk",
    "remediation": "risk",
    "fault_diagnose": "diagnose",
    "chat": "general",
}


# 单轮落库的工具调用条数上限 —— 一次体检可能几十次调用，全存会让历史膨胀
_TRACE_MAX_CALLS = 12


def handle_message(session_id: str, text: str, run=None):
    """处理一条用户消息，generator 产出事件流（loop 事件 + 调度层事件）。

    run: 运行上下文（中断信号 / 模式 / 预算）。未传则自建默认的，保证
         既有调用点（如测试、脚本）不改也能跑。
    """
    from .runctx import RunContext
    registry.ensure_loaded()
    if run is None:
        run = RunContext(session_id=session_id)
    memory.save_chat(session_id, "user", text)

    model_routing = config.routing_is_model() and llm.available()
    # model 模式下用零成本的关键词分类（仅供展示与记忆键），
    # 不再为了选 Agent 而多花一次快模型往返。
    result = (intent_mod.classify_keyword(text) if model_routing
              else intent_mod.classify(text))
    yield {"type": "intent", "intent": result["intent"], "router": result["router"],
           "entities": result.get("entities", {}),
           # 告知前端：这个意图是否真的决定了调度。model 模式下它只是标签，
           # 把它当成"系统选了哪个 Agent"展示会误导使用者。
           "decides_routing": not model_routing}

    if model_routing:
        events = _run_single(session_id, text, config.ORCHESTRATOR_AGENT, run)
    elif result["intent"] == "full_checkup":
        events = _run_checkup(session_id, text, run)
    elif llm.available():
        agent_key = INTENT_AGENT.get(result["intent"], "general")
        events = _run_single(session_id, text, agent_key, run)
    else:
        events = _run_scripted(session_id, text, result["intent"])

    final_answer = ""
    aborted = False
    tool_calls = []                      # 本轮调用轨迹，随回答一起落库（P2-5）
    for ev in events:
        etype = ev.get("type")
        if etype == "answer":
            final_answer = ev.get("text", "")
        elif etype == "aborted":
            aborted = True
        elif etype == "tool_call" and len(tool_calls) < _TRACE_MAX_CALLS:
            # 只记名字与参数，不记结果 —— 结果可能极大，且下一轮只需知道"查过什么"
            tool_calls.append({"tool": ev.get("tool"), "args": ev.get("args") or {}})
        yield ev

    # 中断时不写记忆：半截的结论没有沉淀价值，还会污染后续会话
    if final_answer and not aborted:
        memory.save_chat(session_id, "assistant", final_answer, tool_calls=tool_calls)
        _maybe_remember(result["intent"], final_answer)
    yield {"type": "done"}


def _run_single(session_id: str, text: str, agent_key: str, run=None):
    from .loop import run_agent
    agent = build_agent(agent_key, text)
    history = memory.recent_chat(session_id, limit=8)[:-1]  # 去掉刚存的本条
    yield from run_agent(agent, text, history=history, run=run)


def _run_checkup(session_id: str, text: str, run=None):
    """全面体检：采集 → 拓扑 → 风险扫描 多 Agent 顺序编排，scratchpad 逐级传递。

    ⚠️ 仅 AGENT_ROUTING=intent 时走这里。这正是"调度写死在代码里"的典型样本：
    阶段、顺序、任务描述全是人定的，而拓扑梳理与风险扫描其实互不依赖、完全可以并行。
    model 模式下编排 Agent 会自己判断这一点。保留本函数作为可对比的旧基准。
    """
    if not llm.available():
        yield from _run_scripted(session_id, text, "full_checkup")
        return
    from .loop import run_agent
    scratchpad = {}
    steps = [
        ("data", "执行全量数据采集，汇报各表入库行数"),
        ("topology", "梳理服务拓扑，识别异常边"),
        ("risk", "执行全量风险扫描，输出按严重度分组的风险报告与治理建议（先不执行治理）"),
    ]
    summaries = []
    for agent_key, task in steps:
        # 任一阶段被中断/超预算，立即停止后续阶段
        if run is not None:
            stop, why = run.should_stop()
            if stop:
                yield {"type": "aborted", "reason": why}
                return
        agent = build_agent(agent_key, text)
        yield {"type": "phase", "phase": agent["name"], "task": task}
        for ev in run_agent(agent, task, scratchpad=scratchpad, run=run):
            if ev["type"] == "answer":
                scratchpad[f"{agent['name']}_结论"] = ev["text"][:800]
                summaries.append(f"### {agent['name']}\n{ev['text']}")
                # 中间 Agent 的 answer 降级为阶段小结事件，避免多个最终回答
                yield {"type": "phase_result", "phase": agent["name"], "text": ev["text"]}
            elif ev["type"] == "aborted":
                yield ev
                return
            else:
                yield ev
    yield {"type": "answer", "text": "# 集群全面体检报告\n\n" + "\n\n".join(summaries)}


def _maybe_remember(intent_name: str, answer: str):
    """把有沉淀价值的结论写入长期记忆。"""
    keys = {
        "fault_diagnose": ("conclusion", "最近一次故障定位结论"),
        "risk_scan": ("conclusion", "最近一次风险扫描摘要"),
        "full_checkup": ("conclusion", "最近一次全面体检摘要"),
    }
    if intent_name in keys:
        scope, key = keys[intent_name]
        memory.remember(scope, key, answer[:600])


# --- 离线降级：无 LLM 时的脚本化流程（同一事件流协议，保证演示/测试不断电） ---

def _run_scripted(session_id: str, text: str, intent_name: str):
    yield {"type": "agent_start", "agent": "ScriptedAgent(离线降级)"}

    def call(tool, args=None):
        yield_ev = [{"type": "tool_call", "tool": tool, "args": args or {}}]
        result = registry.execute(tool, args or {})
        data = json.loads(result)
        yield_ev.append({"type": "tool_result", "tool": tool, "result": data})
        return data, yield_ev

    if intent_name == "data_ingest":
        data, evs = call("ingest_data")
        yield from evs
        lines = "\n".join(f"- {k}: **{v}** 行" for k, v in data["ingested"].items())
        yield {"type": "answer", "text": f"数据采集完成，入库统计：\n{lines}"}
    elif intent_name == "topology":
        data, evs = call("build_topology")
        yield from evs
        rows = "\n".join(
            f"- {e['source']} → {e['target']}: {e['call_count']} 次, 错误率 {e['error_rate'] * 100:.2f}%, P99 {e['p99_ms']}ms"
            for e in data["edges"])
        bad = [e for e in data["edges"] if e["error_rate"] > 0.01]
        warn = "\n\n⚠️ 异常边: " + ", ".join(f"{e['source']}→{e['target']}(错误率{e['error_rate'] * 100:.1f}%)" for e in bad) if bad else ""
        yield {"type": "answer", "text": f"服务拓扑（{len(data['nodes'])} 节点 / {data['edge_count']} 边）：\n{rows}{warn}"}
    elif intent_name in ("risk_scan", "full_checkup"):
        if intent_name == "full_checkup":
            _, evs = call("ingest_data")
            yield from evs
            _, evs = call("build_topology")
            yield from evs
        data, evs = call("run_risk_scan")
        yield from evs
        s = data["summary"]
        rows = "\n".join(
            f"- [{f['severity']}] **{f['rule_id']}** {f['title']}\n  - 建议：{f['suggestion']}"
            for f in data["open_findings"])
        yield {"type": "answer",
               "text": f"风险扫描完成：**{s['open']}** 个未治理（P1: {s['P1']} / P2: {s['P2']}），{s['resolved']} 个已治理。\n\n{rows}"}
    elif intent_name == "fault_diagnose":
        data, evs = call("api_perf_stats")
        yield from evs
        worst = data["api_stats"][0]
        t, evs = call("query_traces", {"api_name": worst["api"], "only_error": True, "limit": 1})
        yield from evs
        detail_text = ""
        if t["root_spans"]:
            tid = t["root_spans"][0]["trace_id"]
            detail, evs = call("query_traces", {"trace_id": tid})
            yield from evs
            spans = sorted(detail["spans"], key=lambda s: -s["duration_ms"])
            top = next((s for s in spans if s.get("db_statement")), spans[0])
            slow, evs = call("query_logs", {"logstore": "slow", "limit": 3})
            yield from evs
            detail_text = (f"\n\n【根因链】trace `{tid}` 中 **{top['service']}** 的 span "
                           f"`{top['name']}` 耗时 **{top['duration_ms']}ms**（占大头）"
                           f"\n- SQL: `{(top.get('db_statement') or '')[:100]}`"
                           f"\n- 慢日志印证: rows_examined 百万级、rows_sent 个位数 → **无索引全表扫描**"
                           f"\n\n【治理方案（待确认）】create_db_index(table=orders, columns=[status, created_at])")
        yield {"type": "answer",
               "text": f"【症状】最劣接口 **{worst['api']}**：P99 **{worst['p99_s']}s**、错误率 **{worst['error_rate_pct']}%**{detail_text}"}
    else:
        yield {"type": "answer",
               "text": "（离线模式）未配置 DASHSCOPE_API_KEY，仅支持：采集数据 / 梳理拓扑 / 风险扫描 / 故障定位 / 全面体检。"}
