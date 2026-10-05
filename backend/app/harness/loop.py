"""Agent Loop（ReAct）：LLM function-calling ↔ 工具执行循环，直到产出最终答案。

事件流协议（yield dict，经 SSE 推给前端）：
  {"type": "agent_start", "agent": name}
  {"type": "thinking", "text": ...}            # LLM 每步的思考文本
  {"type": "tool_call", "tool": ..., "args": ...}
  {"type": "tool_result", "tool": ..., "result": ...}
  {"type": "answer", "text": ...}              # 最终回答
  {"type": "error", "text": ...}
  {"type": "aborted", "reason": ...}           # 中断 / 超预算
                                               # 之前会先发一个 partial=True 的 answer
  {"type": "compacted", "reason": ...}         # 上下文紧急压缩后重试
  {"type": "model_fallback", "to": ...}        # 主模型不可用，已降级
  {"type": "usage", ...}                       # token 与耗时（每步一次）
  {"type": "plan_update", "plan": [...]}       # 任务清单变化（由 update_plan 旁路推送）
  {"type": "continued", "segment": n, ...}     # 步数耗尽后自动开新一段（E-2）
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .. import config
from ..tools import registry
from . import approvals, audit, llm, permissions, verifier
from .context import ContextManager
from .runctx import RunContext

logger = logging.getLogger(__name__)

# 步数耗尽且【不再续跑】时的收口指令：停止调工具，用已有数据给出阶段性结论。
_WRAP_UP_PROMPT = (
    "[系统] 已达最大工具调用步数上限。请立刻停止调用任何工具，"
    "基于目前已获得的真实数据给出阶段性结论，"
    "并明确说明哪些环节证据不足、建议下一步查什么。"
)

# 步数耗尽但【将要续跑】时的交接指令。与 wrap_up 的差别是刻意的：
# 这份文本的读者是"下一段的自己"，不是用户 —— 所以要的是可执行的交接单，
# 而不是面向人的总结。
_HANDOFF_PROMPT = (
    "[系统] 本段的工具调用步数已用尽，但任务尚未完成，系统将开启新的一段继续执行。\n"
    "请立刻停止调用工具，写一份【交接摘要】给下一段的你自己。必须包含：\n"
    "1. 已经确认的事实与关键数值（逐字保留数字、资源名、trace_id，注明来自哪个工具）；\n"
    "2. 任务清单里哪些已完成、哪些还没做；\n"
    "3. 下一步具体要调用哪个工具、传什么参数、想验证什么。\n"
    "不要写客套话与面向用户的总结 —— 这份文本只用于让你自己接着干。"
)


def run_agent(agent: dict, user_input: str, history: list = None,
              scratchpad: dict = None, run: RunContext = None,
              event_sink=None):
    """执行一个子 Agent 的完整 loop，generator 产出事件流。

    agent: {"name", "system_prompt", "tools": [tool names], "model"?}
    history: 会话历史 [{"role", "content"}]（已裁剪）
    scratchpad: 上游 Agent 传入的中间结论
    run: 运行上下文（中断信号 / 预算 / 熔断计数）。未传则自建一个默认的，
         保证既有调用点不改也能跑。
    event_sink: 可选。除 yield 之外，把每个事件也旁路推给它（子 Agent 事件
         透传用，见 P1-2）。
    """
    registry.ensure_loaded()
    if run is None:
        run = RunContext(session_id="adhoc")

    def emit(ev: dict):
        """统一出口：既 yield 给调用方，也可旁路推给 event_sink。"""
        if event_sink is not None:
            try:
                event_sink(ev)
            except Exception:                     # noqa: BLE001
                logger.debug("event_sink 推送失败", exc_info=True)
        return ev

    yield emit({"type": "agent_start", "agent": agent["name"]})

    ctx = ContextManager(agent["system_prompt"], session_id=run.session_id)
    if scratchpad:
        ctx.scratchpad.update(scratchpad)
    for h in history or []:
        ctx.messages.append({"role": h["role"], "content": h["content"]})
    ctx.add_user(user_input + ctx.scratchpad_prompt())

    tools = registry.get_schemas(agent["tools"])
    model = agent.get("model") or config.LLM_MODEL
    final_answer = ""
    last_thinking = ""

    # ── 分段执行（E-2）──
    # 外层是"段"，内层是"步"。步数耗尽不再直接收口，而是先看能不能续跑：
    # 能续 → 生成交接摘要、重置上下文、开新一段；不能续 → 走原来的 wrap_up。
    # 预算（token/墙钟）是【整轮】的，续跑绝不重置它 —— 硬上限因此始终有效。
    while True:
        exhausted = True
        for step in range(config.MAX_AGENT_STEPS):
            # ── 中断检查点 ①：每步开头 ──
            stop, why = run.should_stop()
            if stop:
                # 不空手而归：把已积累的思考与中间结论一起交出去（零 LLM 调用）
                for ev in _abort_events(why, last_thinking, ctx):
                    yield emit(ev)
                return

            # 分级压缩（E-3）：软阈值触发零成本微压缩，仍超窗口才动用 LLM 摘要。
            # 返回值 emit 出去 —— 此前主动压缩完全不可见，用户只能看到紧急压缩。
            acted = ctx.compact()
            if acted:
                yield emit({"type": "compacted", **acted})
            ctx.check_invariants_if_strict()

            try:
                msg = llm.chat_with_retry(ctx.messages, tools=tools, model=model, run=run)
            except llm.ContextTooLong as e:
                # 自愈：紧急压缩后重试。护栏计数存在 run 里，绝不在此重置。
                if run.emergency_compacts >= run.max_emergency_compacts:
                    yield emit({"type": "error",
                                "text": "上下文超长，紧急压缩后仍无法恢复，请开启新会话继续。"})
                    return
                run.emergency_compacts += 1
                freed = ctx.force_compact(keep_tail=4)
                logger.warning("上下文超长(%s)，紧急压缩释放 %d 字符后重试", str(e)[:100], freed)
                yield emit({"type": "compacted", "reason": "上下文超长，已紧急压缩后重试",
                            "freed_chars": freed})
                continue
            except llm.Aborted:
                for ev in _abort_events("用户中断", last_thinking, ctx):
                    yield emit(ev)
                return
            except Exception as e:                    # noqa: BLE001
                # 主模型连续失败 → 降级到快模型再试（只降一次）
                if (run.llm_failures >= config.LLM_DOWNGRADE_AFTER
                        and not run.model_downgraded
                        and model != config.LLM_MODEL_FAST):
                    run.model_downgraded = True
                    model = config.LLM_MODEL_FAST
                    logger.warning("主模型连续失败 %d 次，降级到 %s", run.llm_failures, model)
                    yield emit({"type": "model_fallback", "to": model,
                                "text": f"主模型暂时不可用，已切换到 {model} 继续"})
                    continue
                yield emit({"type": "error", "text": f"LLM 调用失败（已重试）: {e}"})
                return

            ctx.add_assistant(msg)

            if msg.content:
                last_thinking = msg.content
                yield emit({"type": "thinking", "text": msg.content})

            if not msg.tool_calls:
                final_answer = msg.content or last_thinking
                exhausted = False
                break

            # 连续的只读工具并批并发执行（P1-1），顺序语义不变
            done = 0
            plan_before = list(run.plan)
            for batch in _partition(msg.tool_calls):
                # ── 中断检查点 ②：每批工具执行前 ──
                if run.abort.is_set():
                    # 必须补齐【剩余全部】tool_call 的结果，否则下一轮 API 400
                    _fill_remaining(ctx, msg.tool_calls[done:], "用户中断，工具未执行")
                    for ev in _abort_events("用户中断", last_thinking, ctx):
                        yield emit(ev)
                    return

                for ev in _execute_batch(batch, ctx, run, agent["name"]):
                    yield emit(ev)
                done += len(batch)

            # 任务清单变化走【主通道】而不是工具内部的旁路推送：
            # 旁路要有 event_sink 才生效，离线脚本与子 Agent 常常没有，事件会静默丢失。
            if run.plan != plan_before:
                yield emit({"type": "plan_update", "plan": list(run.plan)})

            yield emit(run.usage_event())

        if not exhausted:
            break

        ok, reason = run.can_continue()
        if not ok:
            logger.info("步数耗尽，%s", reason)
            final_answer = _wrap_up(ctx, last_thinking, run, model)
            break

        run.continuations += 1
        handoff = _handoff(ctx, run, model)
        if not handoff:
            # 交接摘要拿不到就不要盲目续跑：没有交接单的新一段等于从零重来，
            # 会把已经花掉的 token 再烧一遍。
            logger.warning("交接摘要生成失败，放弃续跑")
            final_answer = _wrap_up(ctx, last_thinking, run, model)
            break
        freed = ctx.reset_for_continuation(handoff)
        yield emit({"type": "continued", "segment": run.continuations + 1,
                    "reason": reason, "freed_chars": freed,
                    "plan": list(run.plan),
                    "text": f"步数用尽但任务未完成（{reason}），已交接并继续执行"
                            f"（第 {run.continuations + 1} 段 / 上限 "
                            f"{run.max_continuations + 1} 段）"})

    # ── 结论事实核对（E-4）──
    # 只在 A 类命中（没见过的 traceID / 近似写错的资源名）时追加自纠正往返，
    # 数值对不上（B 类）只作为附带信息，C 类（数据陈旧）只告警不自纠。
    #
    # naive 基线臂（对照评测用）走一条特殊路径：**照样核对，但既不自纠正也不告警**，
    # 结果只记进 run.verify_probe 供评测统计。理由是"机制不可比"不等于"结果不可比" ——
    # 要证明"没有核对机制会放过多少幻觉"，就必须用同一把尺子去量它的回答；
    # 若干脆不量，基线的幻觉率永远是空白，而空白会被读成"它没有幻觉"。
    if final_answer and (config.verify_enabled() or config.harness_is_naive()):
        vr = verifier.check(final_answer, ctx.evidence)
        if config.harness_is_naive():
            # 只留证据，不干预：基线臂必须保持"裸模型"的真实产出。
            # ⚠️ 这里绝不能 return —— 本函数是生成器，return 会静默跳过末尾那条
            #    yield answer，基线臂将一条回答都产不出、被全部记成"未完成"，
            #    从而虚假地证明"Harness 大幅提升"。用 else 分支而非提前返回。
            run.verify_probe = {
                "suspicious": bool(vr["suspicious"]),
                "fake_ids": list(vr["fake_ids"]),
                "near_miss": len(vr["near_miss_resources"]),
                "stale_sources": len(vr.get("stale_sources") or []),
            }
        else:
            if vr["suspicious"] and run.corrections < config.VERIFY_MAX_CORRECTIONS:
                run.corrections += 1
                logger.warning("结论核对可疑（第 %d 次自纠正）：%s",
                               run.corrections, verifier.describe(vr))
                # ★ A1（编造 traceID/spanID）要**带工具**去把真实 ID 查回来。
                #   不给工具时它唯一的出路是"删掉这条证据"—— 结论还在但佐证没了，
                #   在运维报告里是实打实的质量损失；而且 correction_prompt 本来就写着
                #   "或重新调用 query_traces 取回真实的 trace_id"，
                #   不给工具等于指示模型做一件它做不到的事。
                #   纯 A2（近似资源名）不给：正确名字已由 verifier 算出并写进提示词，
                #   给工具只是浪费一轮，还多一个引入新幻觉的面。
                needs_lookup = bool(vr["fake_ids"])
                for ev in _verify_followup(
                        ctx, run, model,
                        prompt=verifier.correction_prompt(vr),
                        tools=tools if needs_lookup else None,
                        agent_name=agent["name"],
                        max_steps=(config.VERIFY_CORRECT_MAX_STEPS
                                   if needs_lookup else 1),
                        label="事实自纠正"):
                    yield emit(ev)
                if run.followup_answer:
                    final_answer = run.followup_answer
                    vr = verifier.check(final_answer, ctx.evidence)
            # ── C 类（数据时效）：自动用最新数据复核一次 ──
            # 早先这里只告警不处理，理由是"数据过期靠模型重写解决不了"。
            # 那个判断没错，但结论下错了：**重写没用，那就重新取数**。
            # 只挂一条"我的结论基于 31 分钟前的数据"、等用户自己想到要追问
            # "用最新数据再确认一次"，等于把系统该做的事推给用户 ——
            # 而用户很可能压根没注意到那条警示。
            if (vr.get("stale_sources") and config.VERIFY_REFRESH_STALE
                    and run.refreshes < config.VERIFY_MAX_REFRESH):
                stale_names = [n for n, _ in vr["stale_sources"]]
                logger.info("数据时效复核：%s 数据过期，自动用最新数据重查", stale_names)
                yield emit({"type": "verify_refresh",
                            "text": f"检测到结论依赖的数据已过期"
                                    f"（{', '.join(stale_names)}），正在用最新数据复核…",
                            "stale_sources": [{"tool": n, "age_seconds": a}
                                              for n, a in vr["stale_sources"]]})
                run.refreshes += 1
                for ev in _verify_followup(
                        ctx, run, model,
                        prompt=verifier.refresh_prompt(vr),
                        tools=tools, agent_name=agent["name"],
                        max_steps=config.VERIFY_REFRESH_MAX_STEPS,
                        label="数据时效复核"):
                    yield emit(ev)
                if run.followup_answer:
                    final_answer = run.followup_answer
                    # 复核后**必须重新核对**：新取的数据可能带来新的引用问题，
                    # 而且要确认时效问题真的解决了（数据源停更时它不会解决）。
                    vr = verifier.check(final_answer, ctx.evidence)

            if vr["suspicious"] or vr.get("stale_sources"):
                # 走到这里说明：要么是 A/B 类残留，要么复核后数据**仍然**陈旧
                # （典型原因是采集器停了，重取也拿不到新数据）——
                # 这种情况必须如实告警，不能因为"已经尝试过复核"就闷掉。
                yield emit({"type": "verify_warning",
                            "text": verifier.describe(vr),
                            "fake_ids": vr["fake_ids"],
                            "near_miss": [{"said": a, "closest": b, "distance": d}
                                          for a, b, d in vr["near_miss_resources"]],
                            "unverified_numbers": vr["unverified_numbers"],
                            "stale_sources": [{"tool": n, "age_seconds": s}
                                              for n, s in vr.get("stale_sources", [])],
                            "time_risk": vr.get("time_risk", False),
                            "corrected": run.corrections > 0,
                            # 让前端/用户能区分"没复核过" 与 "复核过但数据仍旧"
                            "refreshed": run.refreshes > 0})

    yield emit({"type": "answer", "text": final_answer, "scratchpad": ctx.scratchpad})


def _partial_conclusion(last_thinking: str, ctx: ContextManager, reason: str) -> str:
    """中断/超预算时用【已有内容】拼一份部分结论。**零 LLM 调用**。

    为什么不像步数耗尽那样再调一次 LLM 收口：这条路径的触发原因往往就是
    「预算已经耗尽」，再花一次往返自相矛盾。而 last_thinking 与 scratchpad
    是已经产生并付过费的信息 —— 真正的缺陷是把它们丢掉，不是没去生成新的。

    补这个缺口的直接动因：子 Agent 超预算时只回一句「[子 Agent aborted]」，
    父拿不到任何可用信息，于是倾向于原样重派一次（实测白烧过 40435 token）。
    """
    parts = []
    if (last_thinking or "").strip():
        parts.append(last_thinking.strip())
    if ctx.scratchpad:
        parts.append("[已确认的中间结论]\n"
                     + json.dumps(ctx.scratchpad, ensure_ascii=False, indent=1))
    if not parts:
        return ""                     # 一无所获时不产出空 answer，免得盖掉中断提示
    return (f"> ⚠️ 执行被中断（{reason}），以下是中断前已获得的信息，"
            f"**不是完整结论**\n\n" + "\n\n".join(parts))


def _abort_events(reason: str, last_thinking: str, ctx: ContextManager):
    """中断收口的统一出口：先把部分结论交出去，再报中断。

    顺序有意义：前端与 dispatch_agent 都是「answer 优先、aborted 兜底」，
    先 answer 才能让已积累的信息不被一句"已中断"盖掉。
    """
    partial = _partial_conclusion(last_thinking, ctx, reason)
    if partial:
        yield {"type": "answer", "text": partial, "partial": True,
               "scratchpad": ctx.scratchpad}
    yield {"type": "aborted", "reason": reason}


class _Deferred:
    """收集延后执行的写操作，由调用方在串行阶段按原顺序 flush。"""

    def __init__(self):
        self.actions: list = []

    def add(self, fn):
        self.actions.append(fn)

    def flush(self):
        for fn in self.actions:
            fn()


def _partition(tool_calls) -> list:
    """把一轮 tool_calls 切成可并行的批次，【严格保持原顺序】。

    只有【连续】的可并行工具才合成一批：一旦遇到不可并行的（有副作用/需确认），
    就必须先把前面的批跑完再单独跑它。这样保证"写操作之前的读都已完成、
    写操作之后的读看到的是写之后的状态"——顺序语义不被打乱。

    例：[查指标, 查日志, 扩副本, 查拓扑]
        → [[查指标, 查日志], [扩副本], [查拓扑]]
    """
    # 朴素基线臂：每批只放一个，即全部串行。并行取数是被测的 Harness 能力之一，
    # 它同时影响耗时（快）与上下文（一轮拿齐多份证据），基线不该白拿。
    if config.harness_is_naive():
        return [[tc] for tc in tool_calls]

    batches: list = []
    cur: list = []
    for tc in tool_calls:
        spec = registry.get_spec(tc.function.name)
        # 必须同时满足只读与可并行：只读保证无副作用，标记保证作者确认过并发安全
        parallelizable = bool(spec and spec.concurrency_safe and spec.is_read_only)
        if parallelizable:
            cur.append(tc)
            continue
        if cur:
            batches.append(cur)
            cur = []
        batches.append([tc])
    if cur:
        batches.append(cur)
    return batches


def _execute_batch(batch, ctx: ContextManager, run: RunContext, agent_name: str):
    """执行一批工具并按【原顺序】产出事件。单个直接串行，多个并发。"""
    if len(batch) == 1:
        yield from _execute_one(batch[0], ctx, run, agent_name)
        return

    workers = min(len(batch), config.MAX_PARALLEL_TOOLS)

    def work(tc):
        # 每个 worker 独立收集写操作与事件，绝不触碰共享的 ctx
        deferred = _Deferred()
        events = list(_execute_one(tc, ctx, run, agent_name, deferred=deferred))
        return deferred, events

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool") as ex:
        # ThreadPoolExecutor.map 保证【按输入顺序】返回结果，天然满足保序要求
        results = list(ex.map(work, batch))
    elapsed = int((time.perf_counter() - t0) * 1000)
    logger.info("并行执行 %d 个只读工具（%d 线程）耗时 %dms: %s",
                len(batch), workers, elapsed,
                [tc.function.name for tc in batch])

    for deferred, events in results:
        deferred.flush()          # 串行写上下文与审计，顺序与 tool_calls 一致
        yield from events


def _execute_one(tc, ctx: ContextManager, run: RunContext, agent_name: str,
                 deferred: "_Deferred" = None):
    """执行单个 tool_call 的完整链路，generator 产出事件。

    ★ 铁律：每一条提前 return 的路径都必须先写 tool_result，
      否则会制造孤立的 tool_call，下一轮 API 直接 400（即 Bug 1 的另一种成因）。
      所有拒绝路径统一走 reject()，从结构上保证这一点。

    四步：① 参数校验（回给模型）② 权限决策（升级给用户）③ 执行 ④ 审计

    deferred: 并行批传入。此时【所有写操作】（写上下文、写审计）不立即执行，
      而是收集起来交由调用方在串行阶段按原顺序 flush。两个原因：
        1. 保序 —— 乱序写回会让相同输入产生不同上下文，问题无法复现；
        2. SQLite 并发写会锁竞争，而审计是合规功能不允许丢记录。
      并行 worker 里只留 registry.execute（纯只读查询，并发读是安全的）。
    """
    name = tc.function.name
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}

    yield {"type": "tool_call", "tool": name, "args": args}

    spec = registry.get_spec(name)
    summary = spec.summarize(args) if spec else name

    def _write(content: str, limit: int = None):
        """写工具结果：并行批下延后到串行阶段，保证顺序。"""
        def do(content=content, limit=limit):
            ctx.add_tool_result(tc.id, name, content, limit=limit)
        deferred.add(do) if deferred is not None else do()

    def _audit(*a, **kw):
        """写审计：并行批下延后，避免多线程同时写 SQLite。"""
        def do(a=a, kw=kw):
            audit.record(*a, **kw)
        deferred.add(do) if deferred is not None else do()

    def reject(payload: dict):
        """统一的拒绝出口：补齐 tool_result + 产出事件。"""
        _write(json.dumps(payload, ensure_ascii=False))
        return {"type": "tool_result", "tool": name, "result": payload}

    # ── ① 参数/状态校验：失败信息回给【模型】自我纠正，不打扰用户 ──
    if spec is not None and spec.validate_input:
        try:
            err = spec.validate_input(args)
        except Exception as e:                    # noqa: BLE001
            err = f"参数校验异常: {e}"
        if err:
            _audit(run, agent_name, name, args,
                   permissions.Decision("deny", permissions.REASON_TOOL, err),
                   spec, summary, audit.INVALID)
            yield reject({"error": err, "hint": "请修正参数后重试"})
            return

    # ── ② 权限决策：升级给【用户】 ──
    d = permissions.decide(name, args, mode=run.mode,
                           session_approvals=frozenset(run.approvals))

    if d.behavior == "deny":
        _audit(run, agent_name, name, args, d, spec, summary, audit.DENIED)
        yield reject({"error": f"权限拒绝：{d.message}", "reason": d.reason_type})
        return

    if d.behavior == "ask":
        yield {
            "type": "permission_request",
            "request_id": tc.id,
            "tool": name,
            "args": args,
            "summary": summary,
            "is_destructive": bool(spec and spec.is_destructive),
            "timeout_s": config.APPROVAL_TIMEOUT_S,
        }
        # 此刻主线程将阻塞在 wait() 上，无法 yield —— 超时提醒走 RunContext 的旁路通道
        verdict = approvals.wait(
            tc.id,
            session_id=run.session_id,
            on_warn=lambda left: run.push_event({
                "type": "permission_expiring", "request_id": tc.id,
                "tool": name, "summary": summary, "seconds_left": left}))

        if not verdict["approved"]:
            # 三种未批准要区分记录，否则审计失真：
            #   cancelled —— 会话中断导致挂起的确认被取消（用户按了「停止」）
            #   timeout   —— 无人应答，超时兜底
            #   否则       —— 用户主动点了「拒绝」
            if verdict.get("cancelled"):
                status, msg = audit.ABORTED, "会话已中断，操作未执行"
            elif verdict.get("timeout"):
                status, msg = audit.TIMEOUT, "确认超时，操作未执行"
            else:
                status, msg = audit.REJECTED, "用户拒绝了此操作"
            _audit(run, agent_name, name, args, d, spec, summary, status)
            yield reject({"error": msg, "hint": "如需执行请重新发起并确认"})
            return

        if verdict.get("remember"):
            run.approvals.add(permissions.approval_key(name, args))
        d = permissions.Decision("allow", permissions.REASON_USER, "用户批准")
        yield {"type": "permission_granted", "request_id": tc.id, "tool": name,
               "by": d.reason_type}

    # ── 中断检查点 ③：真正动手前的最后一道 ──
    # 并行批一旦提交到线程池就无法逐个撤销，只能让每个 worker 在执行前再看一眼。
    # 少了这道，中断后一批 N 个工具会全部跑完（并行改造引入的回归，已由用例固化）。
    if run.abort.is_set():
        _audit(run, agent_name, name, args, d, spec, summary, audit.ABORTED)
        yield reject({"error": "会话已中断，工具未执行"})
        return

    # ── ③ 执行 ──
    t0 = time.perf_counter()
    result = registry.execute(name, args, run=run)
    duration_ms = int((time.perf_counter() - t0) * 1000)
    status = audit.ERROR if _looks_like_error(result) else audit.OK

    # ── ④ 审计 ──
    _audit(run, agent_name, name, args, d, spec, summary, status, duration_ms)

    # 按【本工具】标注的体量上限写入：超限则落盘 + 只放结构感知预览（P0-4）
    _write(result, limit=spec.max_result_chars)
    yield {"type": "tool_result", "tool": name, "result": _preview(result),
           "duration_ms": duration_ms}


def _looks_like_error(result: str) -> bool:
    """工具结果是否表示失败（registry.execute 把异常包成 {"error": ...}）。"""
    head = result[:200] if result else ""
    return '"error"' in head


def _fill_remaining(ctx: ContextManager, tool_calls, message: str):
    """给未执行的 tool_call 补上错误结果，维持 tool_call/tool_result 配对。"""
    payload = json.dumps({"error": message}, ensure_ascii=False)
    for tc in tool_calls:
        ctx.add_tool_result(tc.id, tc.function.name, payload)


def _verify_followup(ctx: ContextManager, run: RunContext, model: str, *,
                     prompt: str, tools: list = None, agent_name: str = "",
                     max_steps: int = 1, label: str = "复核"):
    """事实核对后的追加复核循环（A 类自纠正 / C 类数据刷新共用）。

    生成器：工具事件照常推给前端，让用户看得见它在复核而不是卡住。
    最终回答通过 `run.followup_answer` 带出（生成器不能既 yield 又 return 值）。

    ## 给不给工具，按「正确答案在哪」决定 —— 这是本函数的核心判断

    | 类别 | 问题 | 正确答案在哪 | 给工具？ |
    |---|---|---|---|
    | A2 近似资源名 | 写了 `rds-mysql-oder` | **已在证据池里**，verifier 算完编辑距离后已把真名写进提示词 | ❌ 不给。改字即可，给了纯属浪费，还多一个引入新幻觉的面 |
    | A1 编造 traceID | 引用了证据池里不存在的 ID | **不在上下文里**，只能去库里查 | ✅ 必须给 |
    | C 类 数据时效 | 数字都真实、但描述的是半小时前的世界 | 只能重新取数 | ✅ 必须给 |

    早先这里是"一律不给工具"，理由是"避免一次核对拖出十几次调用"。但那样
    A1 的唯一出路就只剩**删掉那条证据** —— 结论还在，支撑证据没了，
    在运维报告里是实打实的质量损失。更糟的是 correction_prompt 里本来就写着
    "或重新调用 query_traces 取回真实的 trace_id"，**指示模型做一件它做不到的事**。

    ## 护栏

    · max_steps：复核只该补那几处，不该变成"从头再排一遍"；
    · 次数上限由调用方用 run.corrections / run.refreshes 控制（存在 run 上而非
      局部：续跑会重开内层循环，放局部会被重置、护栏失效）；
    · 中断与预算检查点与主循环一致，用户点停止要能真停下。
    """
    run.followup_answer = ""
    if run.abort.is_set():
        return
    stop, _ = run.should_stop()
    if stop:                          # 预算已尽就不要再花一轮
        return

    ctx.add_user(prompt, evidence=False)
    for _ in range(max(1, max_steps)):
        if run.abort.is_set():
            return
        try:
            ctx.compact()
            ctx.check_invariants_if_strict()
            msg = llm.chat_with_retry(ctx.messages, tools=tools, model=model, run=run)
        except Exception as e:        # noqa: BLE001
            logger.warning("%s调用失败: %s", label, e)
            return
        if not tools:
            # 无工具路径（纯改写）：一轮即出结论，不进上下文循环
            run.followup_answer = (msg.content or "").strip()
            return
        ctx.add_assistant(msg)
        if msg.content:
            yield {"type": "thinking", "text": msg.content}
        if not msg.tool_calls:
            run.followup_answer = (msg.content or "").strip()
            return
        done = 0
        for batch in _partition(msg.tool_calls):
            if run.abort.is_set():
                _fill_remaining(ctx, msg.tool_calls[done:], "用户中断，工具未执行")
                return
            for ev in _execute_batch(batch, ctx, run, agent_name):
                yield ev
            done += len(batch)
        yield run.usage_event()
    logger.warning("%s未在 %d 步内收口，保留原回答并告警", label, max_steps)


def _handoff(ctx: ContextManager, run: RunContext, model: str) -> str:
    """步数耗尽且将要续跑时，让模型写一份交接摘要给下一段的自己（E-2）。

    返回空字符串表示生成失败 —— 调用方据此放弃续跑，而不是带着空交接单重开一段。
    """
    if run.abort.is_set():
        return ""
    prompt = _HANDOFF_PROMPT
    digest = run.plan_digest()
    if digest:
        prompt += f"\n\n当前任务清单：\n{digest}"
    ctx.add_user(prompt, evidence=False)
    try:
        ctx.compact()
        ctx.check_invariants_if_strict()
        # 交接摘要是轻结构化任务，但仍用当前 model：换模型会让"逐字保留数值"
        # 这条要求的执行质量不可控，而数值丢了整段续跑就白做了。
        msg = llm.chat_with_retry(ctx.messages, tools=None, model=model, run=run)
        text = (msg.content or "").strip()
    except Exception as e:                        # noqa: BLE001
        logger.warning("交接摘要生成失败: %s", e)
        return ""
    if not text:
        return ""
    if digest:
        # 清单是结构化事实，不依赖模型有没有在摘要里写全
        text += f"\n\n[任务清单快照]\n{digest}"
    return text


def _wrap_up(ctx: ContextManager, last_thinking: str, run: RunContext, model: str) -> str:
    """步数耗尽且不再续跑时的收口：追加系统指令再要一次纯文本回答，
    失败则回落到最近的思考文本。"""
    text = ""
    if not run.abort.is_set():
        ctx.add_user(_WRAP_UP_PROMPT, evidence=False)
        try:
            ctx.compact()
            ctx.check_invariants_if_strict()
            msg = llm.chat_with_retry(ctx.messages, tools=None, model=model, run=run)
            text = msg.content or ""
        except Exception as e:                    # noqa: BLE001
            logger.warning("步数耗尽后的收口调用失败: %s", e)

    # 说清"跑了几段、为什么停"，而不是笼统地说撞了步数上限 ——
    # 续跑 3 段后停下和第 1 段就停下，对用户意味着完全不同的事。
    segments = run.continuations + 1
    budget = f"{config.MAX_AGENT_STEPS} 步"
    if segments > 1:
        budget = f"{segments} 段 × {config.MAX_AGENT_STEPS} 步"
    left = run.unfinished_steps()
    tail = f"，任务清单尚余 {len(left)} 项未完成" if left else ""

    text = text or last_thinking
    if not text:
        return (f"已用尽执行预算（{budget}）{tail}，且未能得出结论，"
                f"请缩小问题范围重试。")
    return (f"> ⚠️ 已用尽执行预算（{budget}）{tail}，以下为阶段性结论\n\n{text}")


def _preview(result: str, limit: int = 1500):
    """给前端的工具结果预览（尽量保留结构化 JSON）。"""
    if len(result) <= limit:
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result[:limit] + f"...[截断，共{len(result)}字符]"
