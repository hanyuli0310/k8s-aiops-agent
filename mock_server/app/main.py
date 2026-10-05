"""mock_server 入口（:9001）：契约 A 的 10 个接口 + 后台 tick 循环。

数据平面（供 data_collector 采集，阿里云 API 风格）：
  GET  /cms/ListMetrics
  GET  /cms/DescribeMetricList
  GET  /sls/GetLogs
  GET  /k8s/resources
  GET  /realtime/metrics
控制平面（供 backend 代理）：
  GET  /control/scenarios
  POST /control/inject_fault
  POST /control/recover_fault
  POST /control/actions
  GET  /control/world_status
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Query
from pydantic import BaseModel

from . import actions, config, faults, world_def as W
from .engine import get_engine
from .renderers import cms, k8s, sls

logging.basicConfig(level=logging.INFO, format="%(asctime)s [mock] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="AIOps Mock World Server", version="1.1.0")

_tick_task: Optional[asyncio.Task] = None


async def _tick_loop():
    """后台 tick：同步演算放线程池，避免阻塞事件循环（spec 风险项）。"""
    loop = asyncio.get_event_loop()
    engine = get_engine()
    while True:
        try:
            stats = await loop.run_in_executor(None, engine.tick)
            if stats["tick"] % 12 == 0:      # 每分钟（5s×12）打一条摘要
                logger.info("tick #%d: req=%d err=%.2f%% traces=%d faults=%d",
                            stats["tick"], stats["requests"], stats["error_rate"] * 100,
                            stats["sampled_traces"], len(engine.active_faults))
        except Exception:                     # noqa: BLE001
            logger.exception("tick failed")
        await asyncio.sleep(config.TICK_INTERVAL_S)


@app.on_event("startup")
async def startup():
    global _tick_task
    check = W.self_check()
    if not check["ok"]:
        raise RuntimeError(f"世界观自检失败：{check['errors']}")
    logger.info("world self-check ok: %s", check["summary"])
    engine = get_engine()
    engine.tick()                             # 先跑一拍，保证接口立刻有数据
    _tick_task = asyncio.create_task(_tick_loop())
    logger.info("tick loop started: interval=%ss entry_rps=%s sample_rate=%s",
                config.TICK_INTERVAL_S, config.ENTRY_RPS, config.TRACE_SAMPLE_RATE)


@app.on_event("shutdown")
async def shutdown():
    if _tick_task:
        _tick_task.cancel()


# ----------------------------------------------------------------- 数据平面

@app.get("/cms/ListMetrics")
def list_metrics():
    return cms.list_metrics(get_engine())


@app.get("/cms/DescribeMetricList")
def describe_metric_list(
    Namespace: str = Query(...), MetricName: str = Query(...),
    StartTime: Optional[int] = Query(None, description="毫秒时间戳"),
    EndTime: Optional[int] = Query(None, description="毫秒时间戳"),
):
    return cms.describe_metric_list(get_engine(), Namespace, MetricName, StartTime, EndTime)


@app.get("/sls/GetLogs")
def get_logs(
    logstore: str = Query(...),
    from_: Optional[int] = Query(None, alias="from", description="秒级时间戳"),
    to: Optional[int] = Query(None, description="秒级时间戳"),
    offset: int = Query(0), lines: int = Query(1000, le=5000),
):
    return sls.get_logs(get_engine(), logstore, from_, to, offset, lines)


@app.get("/k8s/resources")
def k8s_resources():
    return k8s.render(get_engine())


@app.get("/realtime/metrics")
def realtime_metrics(service: Optional[str] = None, instance: Optional[str] = None):
    return get_engine().realtime_snapshot(service, instance)


# ----------------------------------------------------------------- 控制平面

class InjectFaultReq(BaseModel):
    scenario_id: str


class RecoverFaultReq(BaseModel):
    fault_id: str


class ActionReq(BaseModel):
    action_type: str
    target: str
    params: Dict[str, Any] = {}
    source: str = "backend-agent"


@app.get("/control/scenarios")
def scenarios():
    return {"scenarios": faults.list_scenarios()}


@app.post("/control/inject_fault")
def inject_fault(req: InjectFaultReq):
    return faults.inject(get_engine(), req.scenario_id)


@app.post("/control/recover_fault")
def recover_fault(req: RecoverFaultReq):
    return faults.recover(get_engine(), req.fault_id)


@app.post("/control/actions")
def apply_action(req: ActionReq):
    return actions.apply_action(get_engine(), req.action_type, req.target, req.params, req.source)


@app.get("/control/world_status")
def world_status():
    return get_engine().world_status()


@app.get("/control/threshold_table")
def threshold_table():
    """U1 交付物：阈值对照表 + 预埋缺陷清单（供 backend 校准规则阈值、联调对账）。"""
    return {"planted_defects": W.PLANTED_DEFECTS, "threshold_table": W.THRESHOLD_TABLE,
            "self_check": W.self_check()["summary"]}


@app.get("/health")
def health():
    e = get_engine()
    return {"status": "ok", "tick": e.tick_no, "world_version": e.world_version,
            "active_faults": len(e.active_faults)}
