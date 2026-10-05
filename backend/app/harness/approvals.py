"""权限确认的跨线程等待/唤醒。

方案 A（阻塞式确认）的实现：Agent 在 worker 线程里跑到需要确认的工具时阻塞在
wait() 上，SSE 生成器把 permission_request 事件推给前端，前端 POST
/api/chat/approve 调用 resolve() 唤醒它。

两段式等待（评审决策 D2）：先等到 timeout - WARN_BEFORE，若仍未决策则通过
on_warn 回调推一次「即将超时」提醒，再等剩余时间。这样演示时讲解超过 5 分钟
也不会被静默拒绝 —— 用户会先收到提醒。

★ 最终超时按【拒绝】处理（fail-closed）：用户没看到卡片就不该执行治理动作。
"""
from __future__ import annotations

import logging
import threading

from .. import config

logger = logging.getLogger(__name__)

_WAITERS: dict = {}
_RESULTS: dict = {}
# request_id → session_id。request_id 是 LLM 的 tool_call id（如 call_xxx），
# 本身不含会话信息，所以必须单独记归属 —— 否则中止会话 A 会误取消会话 B 的待确认。
_OWNERS: dict = {}
_LOCK = threading.Lock()


def wait(request_id: str, timeout: float = None, on_warn=None,
         session_id: str = None) -> dict:
    """阻塞等待用户决策。

    Returns:
        {"approved": bool, "remember": bool, "timeout": bool, "cancelled": bool}

        cancelled=True 表示被 cancel_session()/cancel_all() 取消（通常是会话中断），
        与用户主动点「拒绝」区分开 —— 审计要记 aborted 而非 rejected。
    """
    timeout = config.APPROVAL_TIMEOUT_S if timeout is None else timeout
    warn_before = min(config.APPROVAL_WARN_BEFORE_S, timeout)

    with _LOCK:
        ev = _WAITERS.setdefault(request_id, threading.Event())
        if session_id:
            _OWNERS[request_id] = session_id

    first_leg = max(0.0, timeout - warn_before)
    if not ev.wait(first_leg):
        # 第一段等完仍无决策 → 推提醒，再等剩余时间
        if on_warn:
            try:
                on_warn(int(timeout - first_leg))
            except Exception:                 # noqa: BLE001
                logger.debug("超时提醒回调失败", exc_info=True)
        if not ev.wait(timeout - first_leg):
            logger.warning("权限确认超时（%.0fs），按拒绝处理: %s", timeout, request_id)
            _cleanup(request_id)
            return {"approved": False, "remember": False, "timeout": True,
                    "cancelled": False}

    with _LOCK:
        result = _RESULTS.pop(request_id, {"approved": False, "remember": False})
        _WAITERS.pop(request_id, None)
        _OWNERS.pop(request_id, None)
    result.setdefault("timeout", False)
    result.setdefault("cancelled", False)
    return result


def resolve(request_id: str, approved: bool, remember: bool = False,
            cancelled: bool = False) -> bool:
    """前端回调：写入结果并唤醒等待线程。

    cancelled 供 cancel_all() 使用，标记这不是用户的主动决策。

    Returns:
        是否命中了一个正在等待的请求（False 表示该 id 无人等待，可能已超时）。
    """
    with _LOCK:
        had_waiter = request_id in _WAITERS
        _RESULTS[request_id] = {"approved": approved, "remember": remember,
                                "cancelled": cancelled}
        ev = _WAITERS.setdefault(request_id, threading.Event())
    ev.set()
    logger.info("权限确认结果: %s approved=%s remember=%s cancelled=%s (had_waiter=%s)",
                request_id, approved, remember, cancelled, had_waiter)
    return had_waiter


def pending(session_id: str = None) -> list:
    """当前正在等待确认的 request_id 列表（供调试/前端恢复用）。

    传 session_id 则只返回该会话的。
    """
    with _LOCK:
        if session_id is None:
            return list(_WAITERS)
        return [k for k in _WAITERS if _OWNERS.get(k) == session_id]


def cancel_session(session_id: str) -> int:
    """中止某个会话时，只取消【该会话】挂起的确认，避免影响其他会话。

    标记 cancelled=True —— 这不是用户的主动拒绝，审计应记为 aborted。
    返回取消的数量。
    """
    with _LOCK:
        ids = [k for k in _WAITERS if _OWNERS.get(k) == session_id]
    for rid in ids:
        resolve(rid, approved=False, cancelled=True)
    if ids:
        logger.info("会话 %s 中断，已取消其 %d 个待确认请求", session_id, len(ids))
    return len(ids)


def cancel_all() -> int:
    """取消全部挂起的确认（仅用于进程退出等全局场景）。

    ⚠️ 中止单个会话请用 cancel_session() —— request_id 是 LLM 的 tool_call id，
    不含会话信息，无脑全局取消会误伤其他会话正在等待的确认。
    """
    with _LOCK:
        ids = list(_WAITERS)
    for rid in ids:
        resolve(rid, approved=False, cancelled=True)
    if ids:
        logger.info("已取消全部 %d 个待确认请求", len(ids))
    return len(ids)


def _cleanup(request_id: str):
    with _LOCK:
        _WAITERS.pop(request_id, None)
        _RESULTS.pop(request_id, None)
        _OWNERS.pop(request_id, None)
