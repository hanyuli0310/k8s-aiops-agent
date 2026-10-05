"""FastAPI 入口：/api/chat（SSE 事件流）+ 拓扑/风险/状态 + 动态闭环面板 API。

启动：uvicorn app.main:app --reload --port 8000
live 模式（DATA_SOURCE=live）额外启动定时风险扫描，并代理 mock_server 控制面。
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import config, db
from .agents import base as agents_base
from .harness import approvals, audit, background, llm, retention, runctx, scheduler
from .harness import skills as skills_mod
from .providers import mock_control
from .tools import governance_tools, registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="AIOps Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    db.init_db()
    registry.ensure_loaded()
    started = background.start()
    # 运行产物（工具结果落盘 / 审计 / 预诊断）只增不减，启动清一轮并起定时任务
    cleaned = retention.run_once()
    retention_started = retention.start()
    logger.info("LLM available: %s | tools: %d | DATA_SOURCE=%s | 定时扫描=%s | "
                "启动清理=%s | 定时清理=%s",
                llm.available(), len(registry.list_tools()), config.DATA_SOURCE, started,
                cleaned, retention_started)


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str
    mode: Optional[str] = None      # readonly | confirm(默认) | auto


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """SSE 流式对话：Agent loop 的每一步（意图/思考/工具调用/结果/回答）实时推送。

    生命周期与中断：worker 线程跑 Agent，SSE 生成器负责转发。生成器退出（客户端
    断开、异常、正常结束）时一律置位 run.abort —— 否则 worker 会在无人接收的情况下
    继续把 12 步跑完，持续消耗 token。
    """
    run = runctx.RunContext(session_id=req.session_id,
                            mode=req.mode or config.DEFAULT_PERMISSION_MODE)
    runctx.register(run)

    q: queue.Queue = queue.Queue()
    _SENTINEL = object()
    run.event_sink = q.put          # 旁路事件通道（阻塞等待期间的提醒走这里）

    def worker():
        try:
            for ev in scheduler.handle_message(req.session_id, req.message, run=run):
                q.put(ev)
        except Exception as e:  # noqa: BLE001
            logger.exception("chat worker failed")
            q.put({"type": "error", "text": f"内部错误: {e}"})
            q.put({"type": "done"})
        finally:
            q.put(_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    async def event_stream():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    logger.info("客户端断开，中止会话 %s", req.session_id)
                    run.abort.set()
                    break
                try:
                    # 带超时的 get：阻塞式 q.get() 会让 disconnect 检查永远得不到执行机会
                    ev = await loop.run_in_executor(None, lambda: q.get(timeout=1.0))
                except queue.Empty:
                    continue
                if ev is _SENTINEL:
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        finally:
            run.abort.set()                 # 任何退出路径都置位，通知 worker 收手
            runctx.unregister(req.session_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/stop")
def stop_chat(session_id: str = "default"):
    """前端「停止」按钮：中止指定会话正在进行的 Agent 运行。"""
    run = runctx.get(session_id)
    if run is None:
        return {"ok": True, "stopped": False, "reason": "无活跃会话",
                "active": runctx.active_sessions()}
    run.abort.set()
    # 只取消【本会话】挂起的确认 —— request_id 是 LLM 的 tool_call id，不含会话
    # 信息，全局取消会误伤其他会话正在等待的确认
    cancelled = approvals.cancel_session(session_id)
    logger.info("手动中止会话 %s（取消 %d 个待确认）", session_id, cancelled)
    return {"ok": True, "stopped": True, "cancelled_approvals": cancelled,
            "usage": run.usage_event()}


class ApproveRequest(BaseModel):
    request_id: str
    approved: bool
    remember: bool = False          # 本次会话内同类动作都允许


@app.post("/api/chat/approve")
def approve(req: ApproveRequest):
    """权限确认卡片的回调：唤醒阻塞在该工具上的 Agent。

    had_waiter=False 表示该请求已不在等待（通常是已超时被自动拒绝）。
    """
    had_waiter = approvals.resolve(req.request_id, req.approved, req.remember)
    return {"ok": True, "had_waiter": had_waiter,
            "pending": approvals.pending()}


@app.get("/api/chat/pending-approvals")
def pending_approvals():
    """当前待确认的请求（前端刷新页面后恢复确认卡片用）。"""
    return {"pending": approvals.pending()}


@app.get("/api/audit")
def audit_list(limit: int = 100, destructive_only: bool = False,
               session_id: str = None):
    """工具调用审计：谁在什么模式下批准了什么动作、结果如何。"""
    return {
        "summary": audit.summary(session_id),
        "records": audit.query(limit=limit, destructive_only=destructive_only,
                               session_id=session_id),
    }


@app.get("/api/status")
def status():
    return {
        "llm_available": llm.available(),
        "llm_model": config.LLM_MODEL,
        "llm_model_fast": config.LLM_MODEL_FAST,
        "data_source": config.DATA_SOURCE,
        "db_counts": db.table_counts(),
        # 数据新鲜度：live 模式下据此判断采集是否断流（前端顶部提示）
        "data_freshness": db.data_freshness(),
        "freshness_stale_after_s": config.FRESHNESS_STALE_AFTER_S,
        "tools": registry.list_tools(),
        # 安全态：前端据此渲染模式下拉
        "permission": {
            "default_mode": config.DEFAULT_PERMISSION_MODE,
            "modes": list(runctx.VALID_MODES),
            "approval_timeout_s": config.APPROVAL_TIMEOUT_S,
            "pending_approvals": approvals.pending(),
        },
        "tool_safety": registry.describe_tools(),
        # Skill 与 Agent 资产（P2-1 / P2-2）：前端资产面板与排查都读这里
        "skills": skills_mod.describe(),
        "skill_stats": skills_mod.stats(),
        "agents": agents_base.describe(),
    }


@app.get("/api/skill/{name}")
def skill_content(name: str, reference: str = None):
    """读一篇 Skill 的正文或细则 —— 资产面板据此展示「模型实际看到的内容」。

    name / reference 都经过 skills 模块的白名单校验（只接受已发现的条目），
    路径穿越在那一层就被挡住了，这里不再拼路径。
    """
    metas = skills_mod.discover()
    if name not in metas:
        raise HTTPException(status_code=404, detail=f"未知 Skill: {name}")
    if reference:
        body = skills_mod.load_reference(name, reference)
        if not body:
            raise HTTPException(status_code=404,
                                detail=f"Skill「{name}」没有细则 {reference}")
        return {"name": name, "reference": reference, "content": body}
    return {"name": name, "content": skills_mod.load_body(name),
            "references": metas[name].references}


@app.post("/api/ingest")
def ingest():
    """直接触发采集（对话之外的快捷入口）。"""
    return json.loads(registry.execute("ingest_data", {}))


@app.get("/api/topology")
def topology():
    """拓扑图数据（前端 G6 渲染用）。"""
    result = json.loads(registry.execute("get_topology", {}))
    if "error" in result:
        result = json.loads(registry.execute("build_topology", {}))
    return result


@app.get("/api/risks")
def risks():
    return json.loads(registry.execute("get_risk_report", {}))


# ================= 动态闭环面板 API（spec v1.1 契约 B）=================

@app.get("/api/realtime")
def realtime(service: Optional[str] = None, minutes: int = 10):
    """U12：聚合 realtime_metrics 为时序 series（前端图表数据源）。"""
    since_ms = int((time.time() - minutes * 60) * 1000)
    where, params = "ts >= :w", {"w": since_ms}
    if service:
        where += " AND service = :svc"
        params["svc"] = service
    rows = db.fetch_all(
        f"""SELECT ts, service, instance, metric, value, status FROM realtime_metrics
            WHERE {where} ORDER BY ts""", params)
    series: dict = {}
    for r in rows:
        key = f"{r['instance']}.{r['metric']}"
        series.setdefault(key, {"name": key, "service": r["service"],
                                "instance": r["instance"], "metric": r["metric"],
                                "points": []})
        series[key]["points"].append([r["ts"], r["value"]])
    services = sorted({r["service"] for r in rows})
    return {"series": list(series.values()), "services": services, "row_count": len(rows)}


@app.get("/api/scan-reports")
def scan_reports(limit: int = 20):
    rows = db.fetch_all(
        "SELECT id, scan_ts, `trigger`, total_findings, new_findings, resolved, summary_json "
        "FROM scan_reports ORDER BY id DESC LIMIT :l", {"l": min(limit, 100)})
    for r in rows:
        r["summary"] = db.json_load(r.pop("summary_json"))
    return rows


@app.get("/api/prediagnosis")
def prediagnosis(limit: int = 20):
    """自主预诊断结果（P2-7）：定时扫描发现新增 P1 时子 Agent 自动分析的根因。"""
    rows = db.fetch_all(
        "SELECT id, finding_key, rule_id, resource_ref, severity, title, conclusion, "
        "tool_calls, tokens, status, duration_ms, created_at "
        "FROM prediagnosis ORDER BY id DESC LIMIT :l", {"l": min(limit, 100)})
    return {
        "enabled": config.PREDIAGNOSIS_ENABLED,
        "max_per_scan": config.PREDIAG_MAX_PER_SCAN,
        "records": rows,
    }


# --- mock 控制面代理（前端唯一入口，统一错误包装）---

class InjectReq(BaseModel):
    scenario_id: str


class RecoverReq(BaseModel):
    fault_id: str


@app.get("/api/fault/scenarios")
def fault_scenarios():
    return mock_control.scenarios()


@app.post("/api/fault/inject")
def fault_inject(req: InjectReq):
    return mock_control.inject_fault(req.scenario_id)


@app.post("/api/fault/recover")
def fault_recover(req: RecoverReq):
    return mock_control.recover_fault(req.fault_id)


@app.get("/api/world")
def world():
    return mock_control.world_status()


# --- 治理 check 列表流（U10）---

class PlanReq(BaseModel):
    finding_ids: Optional[List[int]] = None


class ExecuteReq(BaseModel):
    plan_id: int
    item_ids: List[str]


@app.post("/api/governance/plan")
def governance_plan(req: PlanReq):
    return governance_tools.create_plan(req.finding_ids)


@app.get("/api/governance/plans")
def governance_plans():
    return governance_tools.list_plans()


@app.post("/api/governance/execute")
def governance_execute(req: ExecuteReq):
    result = governance_tools.execute_plan(req.plan_id, req.item_ids)
    # 执行完立即手动触发一轮扫描，前端无需等下个定时周期
    if config.is_live() and "error" not in result:
        try:
            background._scan_once(trigger="manual")
        except Exception:  # noqa: BLE001
            logger.exception("治理后立即复扫失败（不影响执行结果）")
    return result
