"""U11 mock_server 控制面客户端：backend → mock 的 Action 反馈与控制面代理。

- 治理工具成功后调 forward_action 把动作传回 mock（世界状态变更 → 指标自然恢复）
- 转发失败仅记 warning，不影响治理工具自身的返回值（spec U11 要求）
- DATA_SOURCE=static 时全部空操作（零外呼）
"""
from __future__ import annotations

import logging

import httpx

from .. import config

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0


def _post(path: str, payload: dict) -> dict:
    try:
        resp = httpx.post(f"{config.MOCK_SERVER_URL}{path}", json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001
        return {"error": f"mock_server 不可达: {e}"}


def _get(path: str, params: dict = None) -> dict:
    try:
        resp = httpx.get(f"{config.MOCK_SERVER_URL}{path}", params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001
        return {"error": f"mock_server 不可达: {e}"}


def forward_action(action_type: str, target: str, params: dict = None) -> dict:
    """治理动作 → mock。static 模式零外呼；失败不抛异常。"""
    if not config.is_live():
        return {"skipped": "static 模式不转发"}
    result = _post("/control/actions", {
        "action_type": action_type, "target": target,
        "params": params or {}, "source": "backend-agent",
    })
    if "error" in result:
        logger.warning("Action 转发失败（不影响治理结果）: %s %s -> %s",
                       action_type, target, result["error"])
    else:
        logger.info("Action 已转发至 mock: %s %s -> %s",
                    action_type, target, result.get("effect", ""))
    return result


# --- 控制面代理（U12：前端统一走 backend）---

def scenarios() -> dict:
    return _get("/control/scenarios")


def inject_fault(scenario_id: str) -> dict:
    return _post("/control/inject_fault", {"scenario_id": scenario_id})


def recover_fault(fault_id: str) -> dict:
    return _post("/control/recover_fault", {"fault_id": fault_id})


def world_status() -> dict:
    return _get("/control/world_status")
