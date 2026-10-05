"""RunContext：单次对话的运行上下文（中断信号 + 运行模式 + 预算 + 旁路事件通道）。

对标 Claude Code 的 ToolUseContext（abortController + 预算 + 会话状态）。
Python 里用 threading.Event 承担 AbortController 的角色 —— /api/chat 的 SSE
生成器与实际跑 Agent 的 worker 线程分离，靠这个 Event 传递中断意图。

设计要点：
1. 所有熔断计数器（llm_failures / emergency_compacts）都存在这里，而不是
   loop 的局部变量 —— 否则跨恢复路径（compact 重试、模型降级）会被重置，
   护栏失效导致无限重试烧 token。
2. event_sink 是【旁路】推送通道：Agent 阻塞等待（如权限确认）时无法 yield，
   需要一条不经过 generator 的路把提醒推给前端。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from .. import config

logger = logging.getLogger(__name__)

# 运行模式（P0-1 权限门禁会用到，此处先定义好取值）
MODE_READONLY = "readonly"      # 只读巡检：拒绝治理动作与配置变更
MODE_CONFIRM = "confirm"        # 默认：破坏性动作弹确认
MODE_AUTO = "auto"              # 无人值守：非破坏性写操作自动放行
VALID_MODES = (MODE_READONLY, MODE_CONFIRM, MODE_AUTO)

# 任务清单里每一步的合法状态（E-2）
PLAN_STATUSES = ("pending", "in_progress", "done")


@dataclass
class RunContext:
    session_id: str
    mode: str = MODE_CONFIRM
    abort: threading.Event = field(default_factory=threading.Event)
    approvals: set = field(default_factory=set)          # 会话内已批准的动作 key

    # ── 预算：所有自动流程都要有硬上限 ──
    tokens_in: int = 0
    tokens_out: int = 0
    max_tokens: int = config.RUN_MAX_TOKENS
    started_at: float = field(default_factory=time.time)
    max_wall_s: float = config.RUN_MAX_WALL_S

    # ── 熔断计数器（必须存在此处，见模块 docstring 第 1 点）──
    llm_failures: int = 0                                # 连续失败次数，成功即归零
    emergency_compacts: int = 0
    max_emergency_compacts: int = 1
    model_downgraded: bool = False                       # 是否已降级到快模型

    # ── 嵌套深度（子 Agent 用，防无限繁殖 + UI 缩进）──
    depth: int = 0

    # ── 任务清单与分段续跑（E-2）──
    # plan 由模型经 update_plan 工具维护，形如
    #   [{"title": "...", "status": "pending|in_progress|done"}]
    # 只存内存不落库：它是本轮的执行状态，不是需要长期检索的资产。
    plan: list = field(default_factory=list)
    continuations: int = 0
    max_continuations: int = config.RUN_MAX_CONTINUATIONS

    # ── 事实核对的自纠正次数（E-4）。存在 run 上而非 loop 局部：
    #    续跑会重开内层循环，计数放局部会被重置，护栏失效。
    corrections: int = 0

    # ── C 类数据时效的自动刷新复核（次数存 run 上，理由同 corrections：
    #    续跑会重开内层循环，放局部会被重置、护栏失效）。
    #    followup_answer 是回传通道：_verify_followup 是生成器，
    #    不能既 yield 事件又 return 回答，只能挂在 run 上带出来。
    refreshes: int = 0
    followup_answer: str = ""

    # ── 仅 naive 基线臂使用：核对结果的"只读探针"（对照评测用）。
    #    基线臂不做自纠正也不告警，但仍要留下幻觉证据，否则它的幻觉率是一片空白，
    #    而空白会被读成"基线没有幻觉"，等于替对照组隐瞒缺陷。
    verify_probe: Optional[dict] = None

    # ── 旁路事件通道（由 main.py 注入 queue.put）──
    event_sink: Optional[Callable[[dict], None]] = None

    def __post_init__(self):
        if self.mode not in VALID_MODES:
            logger.warning("未知运行模式 %r，回落到 %s", self.mode, MODE_CONFIRM)
            self.mode = MODE_CONFIRM

    # --- 中断与预算 ---

    def should_stop(self) -> Tuple[bool, str]:
        """是否应停止本轮。返回 (should_stop, reason)。

        ★ 预算维度支持【停用】：max_tokens / max_wall_s <= 0 表示不限制，
          跳过该维度的检查。当前默认即为停用（见 config 的说明），
          目标是让 Agent 跑到任务完成；用量仍在累计，只是不再据此熔断。

        用户中断（abort）【永远有效】—— 预算可以关，人工开关不能关，
        否则无限循环就真的没有出口了。
        """
        if self.abort.is_set():
            return True, "用户中断"
        total = self.tokens_in + self.tokens_out
        if self.max_tokens > 0 and total > self.max_tokens:
            return True, f"超出 token 预算（{total:,} / {self.max_tokens:,}）"
        if self.max_wall_s > 0:
            elapsed = time.time() - self.started_at
            if elapsed > self.max_wall_s:
                return True, f"超出时间预算（{elapsed:.0f}s / {self.max_wall_s:.0f}s）"
        return False, ""

    def budget_enabled(self) -> bool:
        """本轮是否设了预算上限（供 UI 决定要不要显示"预算 x%"）。"""
        return self.max_tokens > 0 or self.max_wall_s > 0

    def add_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.tokens_in += prompt_tokens or 0
        self.tokens_out += completion_tokens or 0

    # --- 任务清单与续跑（E-2）---

    def set_plan(self, steps: list) -> list:
        """替换整份任务清单。只接受 title 非空的条目，status 非法值归一到 pending。"""
        clean = []
        for s in steps or []:
            if not isinstance(s, dict):
                continue
            title = str(s.get("title") or "").strip()
            if not title:
                continue
            status = str(s.get("status") or "pending").strip().lower()
            if status not in PLAN_STATUSES:
                status = "pending"
            clean.append({"title": title[:120], "status": status})
        self.plan = clean
        return self.plan

    def unfinished_steps(self) -> list:
        return [s for s in self.plan if s.get("status") != "done"]

    def plan_digest(self) -> str:
        """给提示词用的一行式清单（交接摘要里要带上）。"""
        if not self.plan:
            return ""
        marks = {"done": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        return "\n".join(f"{marks.get(s['status'], '[ ]')} {s['title']}" for s in self.plan)

    def can_continue(self) -> Tuple[bool, str]:
        """步数耗尽时是否应当自动续跑。返回 (可以, 不可以的原因)。

        三道闸门，顺序有意义：
        1. 预算优先 —— 超预算/被中断时绝不续跑（续跑只重置步数，绝不重置预算）；
        2. 续跑次数上限 —— 防止在无效循环里反复分段；
        3. 必须有【结构化的未完成信号】—— plan 里有非 done 的步骤。
           没维护 plan 时不猜、不问模型"你做完了吗"，直接交回用户决定。
        """
        stop, why = self.should_stop()
        if stop:
            return False, f"不续跑：{why}"
        if self.continuations >= self.max_continuations:
            return False, f"不续跑：已达续跑上限（{self.max_continuations} 次）"
        if not self.plan:
            return False, "不续跑：没有任务清单，无法判断是否还有未完成步骤"
        left = self.unfinished_steps()
        if not left:
            return False, "不续跑：任务清单已全部完成"
        return True, f"还有 {len(left)} 个未完成步骤"

    # --- 事件 ---

    def usage_event(self) -> dict:
        """推给前端的用量事件。

        用量与花费统计【与预算是否启用无关】：预算停用后仍要让用户看到
        这一轮花了多少，所以这里照旧上报，只是 budget_pct 会变成 None
        （前端据此不显示"预算 x%"，避免显示成误导性的 0%）。

        单价改为从 config 读取 —— 之前硬编码在这里，单价一变就得改代码。
        """
        total = self.tokens_in + self.tokens_out
        cost = (self.tokens_in * config.PRICE_IN_PER_1K
                + self.tokens_out * config.PRICE_OUT_PER_1K) / 1000
        return {
            "type": "usage",
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "est_cost_cny": round(cost, 4),
            "budget_pct": (round(100 * total / self.max_tokens)
                           if self.max_tokens > 0 else None),
            "elapsed_s": round(time.time() - self.started_at, 1),
        }

    def push_event(self, ev: dict):
        """旁路推送（不经过 generator）。用于阻塞等待期间的提醒。

        失败只记 debug —— 推送通道异常绝不能反过来影响 Agent 主流程。
        """
        if not self.event_sink:
            return
        try:
            self.event_sink(ev)
        except Exception:                                # noqa: BLE001
            logger.debug("旁路事件推送失败", exc_info=True)

    # --- 子 Agent ---

    def child(self, session_suffix: str, *, mode: str = MODE_READONLY,
              max_tokens: int = None, max_wall_s: float = None) -> "RunContext":
        """派生子 Agent 的运行上下文：独立预算、深度 +1、共享中断信号与事件通道。

        共享 abort：父被中断时子也要停。
        独立预算：防止单个子 Agent 跑飞拖垮整轮。
        不继承 plan、且禁止续跑：任务清单是父 Agent 的执行状态，
        子 Agent 只负责一个自包含子任务，允许它分段续跑会让预算彻底失控。

        ⚠️ **"独立预算"指的是独立的【上限】，不是免费。**
        子 Agent 跑完后 `dispatch_agent` 会把它的用量回加到父
        （见 tools/agent_tools.py）—— 否则并行派 N 个子 Agent 就能绕过
        RUN_MAX_TOKENS 这条整轮硬上限。墙钟维度天生没这个洞：
        父的 started_at 不变，子跑多久父的 elapsed 就涨多少。
        """
        # ⚠️ 必须用 `is None` 判断，不能写 `max_tokens or config.X`：
        #    0 现在是【合法取值】（表示不限制），会被 or 当成假值吞掉，
        #    结果“显式要求不限制”反而被改回 config 的值。
        return RunContext(
            session_id=f"{self.session_id}/{session_suffix}",
            mode=mode,
            abort=self.abort,                            # 共享：父断则子断
            max_tokens=(config.SUBAGENT_MAX_TOKENS if max_tokens is None
                        else max_tokens),
            max_wall_s=(config.SUBAGENT_MAX_WALL_S if max_wall_s is None
                        else max_wall_s),
            depth=self.depth + 1,
            max_continuations=0,                         # 子 Agent 不续跑
            event_sink=self.event_sink,
        )


# --- 活跃会话注册表，供 /api/chat/stop 查找 ---

_RUNS: dict = {}
_RUNS_LOCK = threading.Lock()


def register(run: RunContext):
    with _RUNS_LOCK:
        _RUNS[run.session_id] = run


def unregister(session_id: str):
    with _RUNS_LOCK:
        _RUNS.pop(session_id, None)


def get(session_id: str) -> Optional[RunContext]:
    with _RUNS_LOCK:
        return _RUNS.get(session_id)


def active_sessions() -> list:
    with _RUNS_LOCK:
        return list(_RUNS)
