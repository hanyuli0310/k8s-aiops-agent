"""子 Agent 派发工具：把「派专家 Agent」变成 LLM 可调用的能力（P1-2）。

对应 Claude Code 的 AgentTool（第 5 课）。四条核心约束：
  1. 只回传最终结论 —— 中间的日志/trace 全留在子 Agent 上下文里，不污染主上下文
  2. 子 Agent 机制上只读 —— 在【代码里】过滤掉非只读工具，不靠提示词约束
  3. 独立预算 —— 防止单个子 Agent 跑飞拖垮整轮
  4. 禁止繁殖 —— 子 Agent 拿不到 dispatch_agent，避免无限递归

事件流透传（评审决策 D4）：子 run 由 parent.child() 派生，继承父的 event_sink，
子 Agent 的每一步都会带 agent/depth 标记冒到前端。中断信号也是共享的 ——
父被停时子必须同时停，否则子 Agent 会在后台继续烧 token。
"""
from __future__ import annotations

import logging

from .. import config
from ..agents import base as agents_base
from .registry import get_spec, tool

logger = logging.getLogger(__name__)

# ★ 可派发类型由 agents/defs/*.md 的 dispatchable 字段生成，不再硬编码。
#   硬编码的代价实测过：新增 capacity / dbops 两个专家 Agent 后，定义、Skill、
#   测试全都就位，但这里的列表没同步 —— 两个 Agent 存在却永远派不出去，
#   属于静默失效（tests/test_skill_accuracy.py 现在会盯住这一点）。
SUBAGENT_TYPES = agents_base.dispatchable_keys()

_DISPATCH_DESC_HEAD = (
    "派一个专家子 Agent 执行独立的探索性子任务。\n"
    "子 Agent 有【独立的上下文】，看不到你当前的对话 —— 所以 task 必须是自包含的完整描述。\n"
    "子 Agent 只会把【最终结论文本】回传给你，中间的查询过程不进入你的上下文，"
    "因此特别适合中间数据量大的任务（日志分析、全表扫描、多轮 trace 下钻）。\n"
    "子 Agent 是【只读】的，无法执行治理动作；如需治理请你自己调用治理工具。\n"
    "可用类型：\n"
)


def _dispatch_description() -> str:
    """工具描述也是定义文件的投影：各类型的说明取自该 Agent 的 when_to_use，不手写。"""
    lines = []
    for key in SUBAGENT_TYPES:
        spec = agents_base.AGENT_SPECS.get(key, {})
        lines.append(f"- {key}: {spec.get('when_to_use') or spec.get('role') or ''}")
    return (_DISPATCH_DESC_HEAD + "\n".join(lines)
            + "\n多个独立子任务可以在同一轮里并行派发。")


def refresh_subagent_types() -> list:
    """定义文件热加载后重建枚举与描述（由 agents.base.reload_defs 调用）。"""
    global SUBAGENT_TYPES
    SUBAGENT_TYPES = agents_base.dispatchable_keys()
    spec = get_spec("dispatch_agent")
    if spec is not None:
        fn = spec.schema["function"]
        fn["description"] = _dispatch_description()
        fn["parameters"]["properties"]["subagent_type"]["enum"] = SUBAGENT_TYPES
    return SUBAGENT_TYPES


# 子 Agent 事件里只透传这几类，避免把子的原始工具结果也灌进前端时间线
_FORWARD_TYPES = {"agent_start", "thinking", "tool_call", "tool_result",
                  "error", "aborted", "compacted", "model_fallback"}


@tool(
    "dispatch_agent",
    _dispatch_description(),
    {
        "type": "object",
        "properties": {
            "subagent_type": {"type": "string", "enum": SUBAGENT_TYPES,
                              "description": "子 Agent 类型"},
            "task": {"type": "string",
                     "description": "完整、自包含的任务描述。务必在末尾要求"
                                    "「输出完整结论，包含关键数值与资源名」"},
            "description": {"type": "string", "description": "3-5 字任务名，用于 UI 展示"},
        },
        "required": ["subagent_type", "task"],
    },
    is_read_only=True,          # 子 Agent 只读 → 本工具对外部世界只读
    concurrency_safe=True,      # 多个子 Agent 可并行（配合 P1-1）
    max_result_chars=8000,
    needs_run=True,             # 需要父 run 来派生子 run（共享中断与事件通道）
    audit_repr=lambda a: f"派发 {a.get('subagent_type')} 子Agent: "
                         f"{a.get('description') or (a.get('task') or '')[:24]}",
)
def dispatch_agent(subagent_type: str, task: str, description: str = "", _run=None):
    from ..agents.base import build_agent
    from ..harness.loop import run_agent
    from ..harness.runctx import RunContext

    if subagent_type not in SUBAGENT_TYPES:
        return {"error": f"不支持的子 Agent 类型: {subagent_type}，可选 {SUBAGENT_TYPES}"}

    # 深度护栏：即使工具过滤被绕过，也不允许无限嵌套
    depth = getattr(_run, "depth", 0)
    if depth >= config.SUBAGENT_MAX_DEPTH:
        return {"error": f"子 Agent 嵌套层级已达上限 {config.SUBAGENT_MAX_DEPTH}，"
                         f"请自己完成该子任务"}

    agent = build_agent(subagent_type, task)

    # ★ 机制层收窄：只留只读工具，并摘掉 dispatch_agent 自身（防繁殖）。
    #   不依赖提示词 —— 提示词只是建议，代码过滤才是保证。
    kept = []
    for t in agent["tools"]:
        if t == "dispatch_agent":
            continue
        spec = get_spec(t)
        if spec is not None and spec.is_read_only:
            kept.append(t)
    dropped = [t for t in agent["tools"] if t not in kept and t != "dispatch_agent"]
    agent["tools"] = kept
    if dropped:
        logger.info("子 Agent %s 已摘除非只读工具: %s", agent["name"], dropped)

    # ★ 独立预算 + 共享中断/事件通道
    if _run is not None:
        sub_run = _run.child(f"sub-{subagent_type}")
    else:
        # 离线脚本或直接调用（无父 run）时退化为独立 run
        sub_run = RunContext(session_id=f"sub-{subagent_type}",
                             mode="readonly",
                             max_tokens=config.SUBAGENT_MAX_TOKENS,
                             max_wall_s=config.SUBAGENT_MAX_WALL_S,
                             depth=depth + 1)

    label = description or subagent_type
    if _run is not None:
        _run.push_event({"type": "subagent_start", "agent": agent["name"],
                         "subagent_type": subagent_type, "task": task[:200],
                         "description": label, "depth": sub_run.depth})

    answer, thinking_tail, tool_calls = "", "", 0
    status = "ok"
    for ev in run_agent(agent, task, run=sub_run):
        t = ev.get("type")
        if t == "answer":
            answer = ev.get("text", "")
        elif t == "thinking":
            thinking_tail = ev.get("text", "")
        elif t == "tool_call":
            tool_calls += 1
        elif t in ("error", "aborted"):
            # 显式记状态：调用方不该靠"结论文本里有没有 error 字样"来猜成败。
            # 超预算被中断时也走这里 —— 那种结论是残缺的，不能当成功用。
            status = t
            answer = answer or f"[子 Agent {t}] {ev.get('text') or ev.get('reason')}"
        # 事件透传（D4）：打上归属标记，前端可折叠成一条子 Agent 时间线
        if _run is not None and t in _FORWARD_TYPES:
            _run.push_event({**ev, "subagent": agent["name"],
                             "description": label, "depth": sub_run.depth})

    tokens = sub_run.tokens_in + sub_run.tokens_out
    if _run is not None:
        # ★ 把子 Agent 的消耗【归集到父】。child() 给的是"独立上限"，
        #   独立不等于免费 —— 不归集的话主 Agent 并行派 N 个子 Agent 就能绕过
        #   RUN_MAX_TOKENS 这条整轮硬上限（单个子上限 40000，派 3 个即 120000，
        #   而父 run 的计数还停在自己那点）。
        #   正确语义：子的上限独立（防单个跑飞），子的消耗向上归集（保整轮封顶）。
        _run.add_usage(prompt_tokens=sub_run.tokens_in,
                       completion_tokens=sub_run.tokens_out)
        _run.push_event({"type": "subagent_done", "agent": agent["name"],
                         "description": label, "status": status,
                         "tool_calls": tool_calls,
                         "tokens": tokens, "depth": sub_run.depth})
    logger.info("子 Agent %s %s：%d 次工具调用，%d tokens",
                agent["name"], status, tool_calls, tokens)

    out = {
        "subagent": agent["name"],
        "status": status,                      # ok | error | aborted
        "conclusion": answer or thinking_tail or "（子 Agent 未产出结论）",
        "tool_calls": tool_calls,
        "tokens": tokens,
    }
    if status != "ok":
        # ★ 明确告诉父"别重试"。实测过一次真实浪费：dbops 子 Agent 因目标表为空
        #   反复试探、烧穿 40000 token 预算被中断，父看到 aborted 后**原样重派了一次**，
        #   又烧掉 40435 —— 同样的输入不会有不同的结果。
        #   子 Agent 的消耗已归集到父，重试等于双倍消耗父的预算。
        out["retry"] = False
        out["hint"] = (f"这个子 Agent 已消耗 {tokens} tokens 并以 {status} 收场，"
                       f"**不要用相同任务再派一次**（结果不会变，且消耗计入本轮总预算）。"
                       f"改为：自己用只读工具查关键的那一两项，"
                       f"或把已有部分结论如实汇报并说明缺口。")
    return out
