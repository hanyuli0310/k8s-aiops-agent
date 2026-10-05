"""DashScope（通义千问）OpenAI 兼容客户端封装 + 重试与错误分级。

错误分三类（对应改进方案 P0-3 / 原理教程第 2 课的错误分级）：
  ContextTooLong  上下文超长 → 不重试，交调用方压缩后重试（可恢复）
  可重试错误      限流 / 5xx / 超时 / 连接问题 → 指数退避重试
  其他            直接抛出（不可恢复）

顺带在每次成功调用后把 usage 记进 RunContext，供预算护栏与成本展示使用。
"""
from __future__ import annotations

import logging
import random
import time

from openai import OpenAI

from .. import config

logger = logging.getLogger(__name__)

_client = None

# 可重试的错误特征（DashScope 兼容层的报错文本差异较大，用子串匹配兜底）
RETRYABLE_MARKERS = (
    "rate_limit", "RateLimit", "Throttling", "429",
    "500", "502", "503", "504",
    "timeout", "Timeout", "timed out",
    "Connection", "connection", "ServiceUnavailable", "InternalError",
)

# 配额耗尽：**同样是 429，但重试毫无意义** —— 额度按周/按月重置，
# 退避几秒再打一次只是把同一个错误再撞一遍。
# 必须比 RETRYABLE_MARKERS 先判，否则会被上面的 "429" 抢先归成可重试。
#
# 为何单独成类而不是简单归 fatal：批量评测里这个错误的后果特别隐蔽 ——
# 每次调用都失败 → 每次排障都返回空回答 → 判定全部记 0 分 →
# 汇总照样印出"提升 0 个百分点"这种**看起来像结论的假数据**。
# 实测就这么废掉过一轮 L3（0/2 vs 0/2 全是配额耗尽，不是真实结果）。
QUOTA_EXHAUSTED_MARKERS = (
    "quota has been exhausted", "quota exceeded", "insufficient_quota",
    "exceeded your current quota", "余额不足", "配额已用尽", "Arrearage",
)

# 上下文超长的错误特征
CONTEXT_OVERFLOW_MARKERS = (
    "context_length", "too long", "maximum context", "max_tokens",
    "InvalidParameter", "Range of input length",
)


class ContextTooLong(Exception):
    """上下文超长：不重试，由调用方压缩后重试。"""


class Aborted(Exception):
    """运行期被用户中断。"""


class QuotaExhausted(Exception):
    """账号配额耗尽：重试无用，调用方（尤其批量评测）应立刻停下来。

    与限流（429 rate limit）的区别：限流退避几秒就能过，配额要等到下个
    计费周期重置。批量评测撞上它会静默产出满屏 0 分假数据，所以要能被
    单独捕获、立刻中止，而不是当成"这次排障失败了"继续跑下一个。
    """


def get_client() -> OpenAI:
    global _client
    if _client is None:
        # ⚠️ timeout 与 max_retries 必须显式给，不能用 SDK 默认值（实测踩过）：
        #   · SDK 默认 timeout=600s、max_retries=2 —— 一次调用最坏能挂 30 分钟；
        #   · 而外层 chat_with_retry 自己还要重试 LLM_MAX_ATTEMPTS 次，
        #     两层叠加后单次调用的最坏阻塞时长是 600 × 3 × 3 ≈ 1.5 小时。
        #   · 更坑的是中断：run.abort 只在每次 attempt 开头检查，
        #     挂在 HTTP 上的那段时间里“可中断”是失效的 ——
        #     实测出现过评测进程卡在一次调用上 21 分钟、CPU 0%、日志零输出。
        # 所以：超时交给我们自己的控制（LLM_TIMEOUT_S），SDK 层不重试。
        _client = OpenAI(api_key=config.DASHSCOPE_API_KEY,
                         base_url=config.LLM_BASE_URL,
                         timeout=config.LLM_TIMEOUT_S,
                         max_retries=0)
    return _client


def available() -> bool:
    return bool(config.DASHSCOPE_API_KEY and not config.DASHSCOPE_API_KEY.startswith("sk-xxx"))


def _classify(exc: Exception) -> str:
    """把异常归类为 'context' | 'quota' | 'retryable' | 'fatal'。"""
    s = str(exc)
    if any(k in s for k in CONTEXT_OVERFLOW_MARKERS):
        return "context"
    # 配额必须先判：它的报文里也带 429，会被 RETRYABLE_MARKERS 抢走
    if any(k in s for k in QUOTA_EXHAUSTED_MARKERS):
        return "quota"
    if any(k in s for k in RETRYABLE_MARKERS):
        return "retryable"
    return "fatal"


def chat(messages: list, tools: list = None, model: str = None,
         temperature: float = 0.3, json_mode: bool = False):
    """单轮非流式调用，返回 message 对象（含 content / tool_calls）。

    json_mode: 让模型只输出合法 JSON（用于意图识别等结构化场景）。
      注：本端点不支持 tool_choice 强制指定函数（返回 invalid_parameter_error），
      所以结构化输出走 response_format 而非 function calling。

    保留原签名以兼容既有调用点；新代码请用 chat_with_retry。
    """
    kwargs = {"model": model or config.LLM_MODEL, "messages": messages, "temperature": temperature}
    if tools:
        kwargs["tools"] = tools
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = get_client().chat.completions.create(**kwargs)
    return resp.choices[0].message


def chat_with_retry(messages: list, tools: list = None, model: str = None,
                    temperature: float = 0.3, max_attempts: int = None, run=None):
    """带指数退避重试的调用，并把 usage 记进 RunContext。

    Raises:
        ContextTooLong: 上下文超长（调用方应压缩后重试）
        Aborted: 运行期被中断
        Exception: 重试耗尽或不可恢复错误
    """
    max_attempts = max_attempts or config.LLM_MAX_ATTEMPTS
    last_exc = None

    for attempt in range(max_attempts):
        if run is not None and run.abort.is_set():
            raise Aborted("用户中断")
        try:
            kwargs = {"model": model or config.LLM_MODEL,
                      "messages": messages, "temperature": temperature}
            if tools:
                kwargs["tools"] = tools
            resp = get_client().chat.completions.create(**kwargs)

            if run is not None:
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    run.add_usage(getattr(usage, "prompt_tokens", 0),
                                  getattr(usage, "completion_tokens", 0))
                run.llm_failures = 0          # 成功即打断连续失败链
            return resp.choices[0].message

        except Exception as e:                # noqa: BLE001
            last_exc = e
            kind = _classify(e)
            if run is not None:
                run.llm_failures += 1

            if kind == "context":
                raise ContextTooLong(str(e)) from e
            if kind == "quota":
                # 不重试、不退避：额度按周重置，再打只是重复撞同一个错
                raise QuotaExhausted(str(e)) from e
            if kind == "fatal" or attempt == max_attempts - 1:
                raise

            delay = min(config.LLM_RETRY_BASE_S * (2 ** attempt), 8) + random.random()
            logger.warning("LLM 第 %d/%d 次失败（%s），%.1fs 后重试",
                           attempt + 1, max_attempts, str(e)[:150], delay)
            # 退避期间也要能被中断，切成小段睡
            slept = 0.0
            while slept < delay:
                if run is not None and run.abort.is_set():
                    raise Aborted("用户中断")
                time.sleep(min(0.25, delay - slept))
                slept += 0.25

    raise last_exc                            # pragma: no cover


def chat_text(messages: list, model: str = None, temperature: float = 0.3,
              json_mode: bool = False) -> str:
    """纯文本调用（意图识别/摘要用，默认走快模型）。"""
    msg = chat(messages, model=model or config.LLM_MODEL_FAST,
               temperature=temperature, json_mode=json_mode)
    return msg.content or ""
