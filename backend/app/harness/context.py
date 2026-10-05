"""上下文管理：消息组装、大工具结果截断、历史压缩、跨步骤 scratchpad。"""
from __future__ import annotations

import json
import logging

from .. import config
from . import llm, tool_results
from . import verifier
from .verifier import Evidence

logger = logging.getLogger(__name__)

# 压缩时默认保留的尾窗消息条数
DEFAULT_TAIL_KEEP = 6


def _fmt_number(v: float) -> str:
    """把数值格式化回**它在原文里的样子**。

    ⚠️ 不能用 `f"{v:g}"`：它会把 1162951.0 写成 `1.16295e+06` ——
    模型对不上工具结果里的 `1162951`，而且精度真的丢了（1.16295e6 = 1162950）。
    "保真补录"用一个会改写数值的格式化函数，等于自相矛盾。
    """
    return str(int(v)) if v == int(v) else str(v)


class ContextManager:
    """一次 Agent 运行的上下文容器。

    - add_tool_result: 工具结果超限时落盘 + 只放结构感知预览（可用 read_tool_result 取回）
    - compact: 历史总量超窗口时，把最早的工具轮次压缩为摘要（有 LLM 用 LLM，无则硬截断）
    - scratchpad: 诊断中间结论（traceID、慢 SQL 等）跨步骤/跨 Agent 传递

    压缩的 API 不变量（重要）：OpenAI 兼容接口要求每条 role="tool" 必须紧跟在
    携带对应 tool_call_id 的 assistant 之后。按【条数】切尾窗会切断这个配对，
    导致 400 messages with role 'tool' must be a response to a preceding
    message with tool_calls。所有切片一律经 _safe_tail_start() 求起点。
    """

    def __init__(self, system_prompt: str, session_id: str = "_"):
        self.messages: list = [{"role": "system", "content": system_prompt}]
        self.scratchpad: dict = {}
        # 落盘用：区分不同会话的工具结果文件
        self.session_id = session_id
        # 事实核对的证据池（E-4）：模型【看到过】的标识符与数值指纹。
        # 增量累积、只存指纹不留原文，所以 reset_for_continuation 之后依然完整 ——
        # 续跑丢的是对话历史，不该连"这个 trace_id 是真的"这类事实一起丢。
        self.evidence = Evidence()
        self.evidence.absorb(system_prompt)

    def add_user(self, content: str, *, evidence: bool = True):
        """追加一条 user 消息。

        evidence=False 用于【内容源自模型自己】的注入（交接摘要、收口指令里的
        任务清单快照）。这类文本进证据池会把模型编造的数字"洗白"成有出处的事实，
        核对就此失效 —— 证据池只能装模型**从外部看到**的内容。
        """
        self.messages.append({"role": "user", "content": content})
        if evidence:
            self.evidence.absorb(content)

    def add_assistant(self, message):
        """接收 openai message 对象或 dict。"""
        if isinstance(message, dict):
            self.messages.append(message)
            return
        entry = {"role": "assistant", "content": message.content or ""}
        if message.tool_calls:
            entry["tool_calls"] = [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            } for tc in message.tool_calls]
        self.messages.append(entry)

    def add_tool_result(self, tool_call_id: str, name: str, result: str,
                        limit: int = None):
        """写入工具结果。超限则落盘并只放结构感知预览（P0-4）。

        limit 由调用方从 ToolSpec.max_result_chars 传入 —— 不同工具的合理体量
        差别很大（风险报告 8000 vs 简单查询 2000），统一用全局值会让标注失效。
        """
        limit = config.TOOL_RESULT_MAX_CHARS if limit is None else limit
        preview = tool_results.persist_and_preview(
            self.session_id, tool_call_id, name, result, limit)
        self.messages.append({
            "role": "tool", "tool_call_id": tool_call_id, "name": name,
            "content": preview,
        })
        # ★ 只吸收【模型看得到的那份】（preview），不是 result 全文。
        #   否则超长结果落盘后，模型没看到的数值也会被当成"它有出处"，
        #   核对就失去了意义 —— 证据池的定义是"模型看到过什么"。
        #   走 absorb_tool_result 而不是 absorb：额外记下这份数据的时间跨度，
        #   用于 C 类时效核对（时间戳不能从提示词里取，详见 verifier 模块文档）。
        self.evidence.absorb_tool_result(name, preview)

    @staticmethod
    def truncate_tool_result(result: str, limit: int = None) -> str:
        """纯截断（不落盘）。保留给不需要取回的场景与旧调用方。"""
        limit = config.TOOL_RESULT_MAX_CHARS if limit is None else limit
        if len(result) <= limit:
            return result
        return (result[:limit]
                + f'\n...[结果超长已截断，原始 {len(result)} 字符。完整数据已入库，可用 sql_query 工具精确查询]')

    def note(self, key: str, value):
        """记录中间结论到 scratchpad。"""
        self.scratchpad[key] = value

    def scratchpad_prompt(self) -> str:
        if not self.scratchpad:
            return ""
        return "\n\n[已确认的中间结论]\n" + json.dumps(self.scratchpad, ensure_ascii=False, indent=1)

    def total_chars(self) -> int:
        """上下文总字符数。

        注意必须把 tool_calls 的 arguments 计入 —— 只统计 content 会把携带大 JSON
        参数的 assistant 消息算成 0，导致该压缩时没压缩。
        """
        n = 0
        for m in self.messages:
            n += len(str(m.get("content") or ""))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                n += len(fn.get("name") or "") + len(fn.get("arguments") or "")
        return n

    # token 估算系数：**用真机实测校准**（拿本项目自己的文档与工具 JSON 做样本，
    # 与 qwen tokenizer 返回的 prompt_tokens 对比）。
    #
    #   样本            字符    实际token  旧公式偏差  新公式偏差
    #   中文技术文档   39,546     19,925      1.32x      1.16x
    #   JSON 数据      10,450      4,348      0.88x      1.22x   ← 旧公式在【低估】
    #   混合           30,450     14,386      1.18x      1.18x
    #
    # 旧公式（中文 1 token/字 + 其余 /4，再 ×4/3）的问题不是"保守"，而是
    # **方向不一致**：对中文高估 32%、对 JSON 却低估 12%。而低估恰恰是危险的
    # 那一侧（撞上限 → 整轮白费 → 还要紧急压缩重试）。
    # 新系数让三类内容统一落在 1.16~1.22x 的轻度高估区间。
    _TOK_PER_CJK = 0.75          # 实测约 0.50，留约 50% 余量
    _TOK_PER_OTHER = 0.5         # 实测约 0.42，留约 20% 余量

    def est_tokens(self) -> int:
        """token 估算。系数经真机实测校准，见上方 _TOK_PER_* 的对照表。

        宁可轻度高估：低估的代价（撞上限、整轮请求白费、还要走紧急压缩重试）
        远大于高估的代价（早压缩一点、少放几条历史）。
        """
        total = 0.0
        for m in self.messages:
            s = str(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                s += json.dumps(tc.get("function") or {}, ensure_ascii=False)
            cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
            total += cjk * self._TOK_PER_CJK + (len(s) - cjk) * self._TOK_PER_OTHER
        return int(total)

    # --- 压缩 ---

    def _safe_tail_start(self, want_tail: int = DEFAULT_TAIL_KEEP) -> int:
        """求一个可安全切片的尾窗起点索引，保证切片不以 role="tool" 开头。

        从期望位置向前扫：只要落在 tool 消息上就继续前移，直到停在它对应的
        assistant(tool_calls) 或更早的 user/assistant 消息上。下界为 1（保留 system）。

        反例（修复前会崩）：
            0 system | 1 user | 2 A1(tool_calls=[a]) | 3 tool(a)
            4 A2(tool_calls=[b]) | 5 tool(b) | 6 A3(tool_calls=[c,d]) | 7 tool(c) | 8 tool(d)
        len=9 时 messages[-6:] 从索引 3 起，tail 以 tool(a) 开头，而 A1 已被压进摘要。
        本函数会把起点从 3 前移到 2（A1），配对得以保全。
        """
        i = max(1, len(self.messages) - want_tail)
        while i > 1 and self.messages[i].get("role") == "tool":
            i -= 1
        return i

    def pressure(self) -> float:
        """上下文压力：字符占比与 token 占比的**较大者**（E-3）。

        两个维度都要看：同样 24000 字符，全中文约 32000 token、全 JSON 只有 8000。
        只看字符会让中文为主的会话压缩得太晚，只看 token 则对 JSON 过度保守。
        """
        by_chars = self.total_chars() / config.CONTEXT_MAX_CHARS
        by_tokens = (self.est_tokens() / config.CONTEXT_MAX_TOKENS
                     if config.CONTEXT_MAX_TOKENS else 0)
        return max(by_chars, by_tokens)

    def micro_compact(self) -> int:
        """L1 微压缩：把最老的超长工具结果换成落盘指针。**零 LLM 调用**。

        为什么值得单独做一档：原来只要越过阈值就直接花一次 LLM 摘要，
        而中段里往往就是几条大工具结果 —— 它们本来就该落盘，换成
        「结构感知预览 + 取回路径」既不花钱也不丢信息（模型仍可
        read_tool_result 取回全文），比让模型用自然语言复述一遍划算得多。

        从最老的开始压，压到压力回落到软阈值以下就停 —— 尾窗里的新结果
        是模型当下正在用的，尽量别动。返回释放的字符数。
        """
        freed = 0
        for i, m in enumerate(self.messages):
            if i == 0 or m.get("role") != "tool":
                continue
            content = str(m.get("content") or "")
            if len(content) < config.MICRO_COMPACT_MIN_CHARS:
                continue
            if "<persisted-output>" in content:
                continue                      # 已经是指针形态，压不动了
            new = tool_results.persist_and_preview(
                self.session_id,
                m.get("tool_call_id") or f"micro-{i}",
                m.get("name") or "tool",
                content,
                config.MICRO_COMPACT_TARGET_CHARS)
            if len(new) < len(content):
                freed += len(content) - len(new)
                m["content"] = new
            if self.pressure() <= config.CONTEXT_SOFT_RATIO:
                break
        if freed:
            logger.info("微压缩释放 %d 字符（零 LLM 调用），压力降至 %.2f",
                        freed, self.pressure())
        return freed

    def compact(self) -> dict:
        """按阶梯压缩，返回本次动作的描述（无动作则返回 None，供调用方 emit 事件）。

            压力 > CONTEXT_SOFT_RATIO   → L1 微压缩（零 LLM）
            压力仍 > 1.0                → L2 中段摘要（一次快模型往返）

        L3 紧急压缩由 force_compact() 承担（撞上 ContextTooLong 之后）。
        """
        if self.pressure() <= config.CONTEXT_SOFT_RATIO:
            return None

        acted = {}
        freed = self.micro_compact()
        if freed:
            acted = {"level": "micro", "freed_chars": freed,
                     "reason": "上下文接近上限，已把较早的大工具结果转为落盘指针"}

        # 微压缩之后【压力仍然超 1.0】才动用 LLM 摘要。
        #
        # ⚠️ 这里原来判的是 `total_chars() > CONTEXT_MAX_CHARS`，只看字符维度。
        #    阈值还小（24000/24000）时两个口径差不多，问题不显；但字符阈值按
        #    "最省 token 的内容"标定后（360k）就出事了：中文会话在 240k 字符处
        #    token 口径早已触顶，而字符数只到 67%，L2 **永远不会触发** ——
        #    只能靠 L1 反复微压缩，直到真的撞上窗口走 L3。
        #    既然 pressure() 已经是"两个口径取较大者"，触发也必须用同一把尺子。
        if self.pressure() > 1.0:
            before = self.total_chars()
            self._compact_at(self._safe_tail_start())
            acted = {"level": "summary",
                     "freed_chars": before - self.total_chars() + freed,
                     "reason": "上下文超出窗口，已把前序对话压缩为摘要"}
        return acted or None

    def force_compact(self, keep_tail: int = 4) -> int:
        """紧急压缩：无条件执行且尾窗更小，返回释放的字符数。

        用于上下文超长错误后的自愈重试（见改进方案 P0-3）。若压缩后仍超窗口，
        对尾窗内的大工具结果做二次截断。
        """
        before = self.total_chars()
        self._compact_at(self._safe_tail_start(want_tail=keep_tail))
        if self.total_chars() > config.CONTEXT_MAX_CHARS:
            for m in self.messages[2:]:
                content = str(m.get("content") or "")
                if m.get("role") == "tool" and len(content) > 500:
                    m["content"] = content[:500] + "\n...[紧急压缩截断]"
        freed = before - self.total_chars()
        logger.info("force_compact 释放 %d 字符，剩余 %d", freed, self.total_chars())
        return freed

    def reset_for_continuation(self, handoff: str) -> int:
        """分段续跑的上下文重置（E-2）：只留 system + 一条交接摘要。

        与 compact()/force_compact() 的区别在于**边界是明确的**：
        步数耗尽是一个天然的分段点，此刻模型刚被要求写完交接单，
        所以可以放心丢掉全部历史轮次，而不用担心截断在半个 tool_call 上
        （那是 compact 必须小心处理 _safe_tail_start 的原因）。

        scratchpad 刻意保留：它是跨 Agent 传递的结构化中间结论，不属于对话历史。
        `evidence` 同样保留（它只存指纹，与上下文体量无关）——
        续跑该丢的是对话历史，不该连"这个 trace_id 是真的"这类事实一起丢。
        返回释放的字符数。
        """
        before = self.total_chars()
        self.messages = [
            self.messages[0],
            {"role": "user", "content": f"[上一段执行的交接摘要，请据此继续]\n{handoff}"},
        ]
        # 交接摘要是模型自己写的，不进证据池（见 add_user 的 evidence 参数说明）
        freed = before - self.total_chars()
        logger.info("续跑重置上下文：释放 %d 字符，剩余 %d", freed, self.total_chars())
        return freed

    def _compact_at(self, start: int):
        """把 messages[1:start] 压成一条摘要，保留 system 与 messages[start:]。"""
        middle = self.messages[1:start]
        if not middle:
            return
        digest = self._summarize(middle)
        self.messages = [
            self.messages[0],
            {"role": "user", "content": f"[前序对话与工具调用摘要]\n{digest}"},
            *self.messages[start:],
        ]
        logger.info("context compacted to %d chars (tail from index %d)",
                    self.total_chars(), start)

    @staticmethod
    def _summarize(messages: list) -> str:
        """把中段对话压成要点摘要，并对关键数值做**保真兜底**（E-3）。

        为什么需要兜底而不是只在提示词里要求：本项目的所有结论都必须有数值出处，
        摘要一旦丢掉水位/行数/耗时这类度量值，后续结论就失去依据 ——
        而"请保留关键数值"只是请求，模型做不到时没人知道。
        所以压缩后做一次机械对比，把丢掉的关键数值直接补录回摘要末尾。
        """
        text = "\n".join(
            f"{m['role']}: {str(m.get('content') or m.get('tool_calls', ''))[:400]}" for m in messages)
        digest = ""
        if llm.available():
            try:
                # chat_text 默认走快模型 —— 摘要是轻结构化任务，不该占用主模型
                digest = llm.chat_text([
                    {"role": "system", "content":
                        "把以下运维排查对话压缩成要点摘要，逐字保留关键数值、资源名、"
                        "traceID、结论，200字以内。数值不得四舍五入或改写单位。"},
                    {"role": "user", "content": text[:8000]},
                ])
            except Exception as e:  # noqa: BLE001
                logger.warning("summarize failed: %s", e)
        if not digest:
            return text[:1500]
        return digest + ContextManager._number_fidelity_note(text, digest)

    @staticmethod
    def _number_fidelity_note(source: str, digest: str) -> str:
        """摘要丢掉的关键数值补录成一行。没丢就返回空串。

        只补录数值本身（不重建上下文）：目的是让模型知道"这些数存在过、
        别当它们不存在"，需要精确语境时它可以用 sql_query 或 read_tool_result 复查。
        """
        try:
            lost = verifier.extract_numbers(source) - verifier.extract_numbers(digest)
        except Exception:                             # noqa: BLE001
            logger.debug("摘要保真校验失败", exc_info=True)
            return ""
        if not lost:
            return ""
        # 大数优先（水位、行数、耗时这类更重要），并限制条数避免摘要反而膨胀
        picked = sorted(lost, key=lambda v: -abs(v))[:12]
        shown = ", ".join(_fmt_number(v) for v in picked)
        logger.info("摘要丢失 %d 个关键数值，已补录 %d 个", len(lost), len(picked))
        return (f"\n[保真补录] 上文出现过但摘要未包含的数值（需要语境时用 sql_query "
                f"或 read_tool_result 复查）：{shown}")

    # --- 开发期护栏 ---

    def assert_api_invariants(self):
        """自检：每条 role="tool" 都必须能匹配到前面某条 assistant 的 tool_call id。

        由 config.HARNESS_STRICT 控制是否启用（默认关）。开启后在每次调用 LLM 前
        执行，把「孤立 tool 消息」这类问题在开发/测试期就暴露，而不是等线上 400。
        """
        pending: set = set()
        for idx, m in enumerate(self.messages):
            role = m.get("role")
            if role == "assistant":
                for tc in m.get("tool_calls") or []:
                    pending.add(tc["id"])
            elif role == "tool":
                tid = m.get("tool_call_id")
                if tid not in pending:
                    raise AssertionError(
                        f"孤立的 tool 消息 at[{idx}] name={m.get('name')} id={tid}；"
                        f"已知 pending={sorted(pending)}；"
                        f"roles={[x.get('role') for x in self.messages]}")

    def check_invariants_if_strict(self):
        """按配置执行自检；非严格模式下只记 warning，不打断运行。"""
        if not config.HARNESS_STRICT:
            return
        try:
            self.assert_api_invariants()
        except AssertionError as e:
            logger.error("API 不变量校验失败: %s", e)
            raise
