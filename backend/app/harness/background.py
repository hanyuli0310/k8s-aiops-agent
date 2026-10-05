"""U9 定时风险扫描后台任务（仅 DATA_SOURCE=live 启动）。

每 RISK_SCAN_INTERVAL_S 执行一次 run_risk_scan，与上轮 open findings 做 diff
计算 new/resolved，写 scan_reports 表（前端"历史扫描报告"数据源）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .. import config, db
from ..tools import registry

logger = logging.getLogger(__name__)

_prev_open_keys: set = set()
_started = False


def _scan_once(trigger: str = "schedule") -> dict:
    """一轮扫描 + diff + 落 scan_reports。同步函数，调用方放 executor。"""
    global _prev_open_keys
    report = json.loads(registry.execute("run_risk_scan", {}))
    if "error" in report:
        logger.warning("定时扫描失败: %s", report["error"])
        return report

    open_keys = {(f["rule_id"], f["resource_ref"]) for f in report["open_findings"]}
    new_keys = open_keys - _prev_open_keys
    resolved_keys = _prev_open_keys - open_keys
    _prev_open_keys = open_keys

    summary = {
        "open": report["summary"]["open"], "P1": report["summary"]["P1"],
        "P2": report["summary"]["P2"],
        "new": sorted(f"{r}@{ref}" for r, ref in new_keys),
        "resolved": sorted(f"{r}@{ref}" for r, ref in resolved_keys),
        "open_titles": [f["title"] for f in report["open_findings"]],
    }
    db.execute(
        """INSERT INTO scan_reports (scan_ts, `trigger`, total_findings, new_findings, resolved, summary_json)
           VALUES (:ts, :tr, :total, :new, :res, :s)""",
        {"ts": int(time.time() * 1000), "tr": trigger,
         "total": report["summary"]["open"], "new": len(new_keys), "res": len(resolved_keys),
         "s": json.dumps(summary, ensure_ascii=False)})
    if new_keys or resolved_keys:
        logger.info("扫描 diff: open=%d new=%s resolved=%s",
                    len(open_keys), sorted(new_keys), sorted(resolved_keys))

    # P2-7：新增 P1 → 自动派子 Agent 预诊断根因
    prediagnosed = _prediagnose_new_p1(report, new_keys)

    return {"open": len(open_keys), "new": len(new_keys),
            "resolved": len(resolved_keys), "prediagnosed": prediagnosed}


def _prediagnose_new_p1(report: dict, new_keys: set) -> int:
    """对本轮【新增的 P1】风险各派一个 diagnose 子 Agent 预分析根因。

    三道闸门，缺一不可 —— 定时扫描每 RISK_SCAN_INTERVAL_S 跑一次，
    不设闸门会把 token 烧穿：
      1. 只处理【新增】且【P1】—— P2 数量多且多为容量类，不值得逐个诊断
      2. 同一 finding_key 只诊断一次（查库去重）—— 未治理的风险每轮都还在 open 里，
         但它不会再出现在 new_keys 中，这一层是防御性的第二道
      3. 单轮上限 PREDIAG_MAX_PER_SCAN —— 一次扫出 5 个新 P1 也不会同时起 5 个子 Agent

    返回实际发起的诊断数。任何异常都被吞掉：预诊断是增值能力，
    不能让它把定时扫描这条主链路搞挂。
    """
    if not config.PREDIAGNOSIS_ENABLED or not new_keys:
        return 0

    by_key = {(f["rule_id"], f["resource_ref"]): f for f in report["open_findings"]}
    candidates = [by_key[k] for k in sorted(new_keys)
                  if k in by_key and by_key[k].get("severity") == "P1"]
    if not candidates:
        return 0

    done = 0
    for finding in candidates:
        if done >= config.PREDIAG_MAX_PER_SCAN:
            logger.info("预诊断达单轮上限 %d，剩余 %d 个新增 P1 留待下轮",
                        config.PREDIAG_MAX_PER_SCAN, len(candidates) - done)
            break
        key = f"{finding['rule_id']}@{finding['resource_ref']}"
        if db.fetch_one("SELECT id FROM prediagnosis WHERE finding_key=:k", {"k": key}):
            continue
        try:
            _run_one_prediagnosis(key, finding)
            done += 1
        except Exception:  # noqa: BLE001
            logger.exception("预诊断失败（不影响扫描主链路）: %s", key)
    return done


def _run_one_prediagnosis(key: str, finding: dict):
    """派一个只读子 Agent 分析单条风险的根因，结果落 prediagnosis 表。"""
    task = (
        f"集群刚新增一条 P1 风险，请定位其根因并给出证据链。\n"
        f"规则：{finding['rule_id']}（{finding['title']}）\n"
        f"资源：{finding['resource_ref']}\n"
        f"规则给出的建议：{finding.get('suggestion') or '（无）'}\n"
        f"证据数据：{json.dumps(finding.get('evidence') or {}, ensure_ascii=False)[:600]}\n\n"
        f"请查询相关指标/日志/Trace/资源配置进行验证，"
        f"输出完整结论，包含关键数值与资源名，并明确指出应优先执行哪个治理动作。"
    )
    t0 = time.perf_counter()
    raw = registry.execute("dispatch_agent", {
        "subagent_type": "diagnose",
        "task": task,
        "description": f"预诊断 {finding['rule_id']}",
    })
    duration_ms = int((time.perf_counter() - t0) * 1000)
    out = json.loads(raw)
    # 用子 Agent 显式回报的 status，不靠"文本里有没有 error"猜 —— 超时被中断的
    # 结论是残缺的，记成 ok 会让人误以为已经有可用的根因分析。
    status = out.get("status") or ("failed" if "error" in out else "ok")

    db.execute(
        """INSERT INTO prediagnosis
           (finding_key, rule_id, resource_ref, severity, title, conclusion,
            tool_calls, tokens, status, duration_ms, created_at)
           VALUES (:k, :r, :ref, :sev, :t, :c, :tc, :tok, :st, :d, :ts)""",
        {"k": key, "r": finding["rule_id"], "ref": finding["resource_ref"],
         "sev": finding.get("severity", "P1"), "t": finding["title"],
         "c": out.get("conclusion") or out.get("error", ""),
         "tc": out.get("tool_calls", 0), "tok": out.get("tokens", 0),
         "st": status, "d": duration_ms,
         "ts": int(time.time() * 1000)})
    logger.info("自主预诊断 %s: %s（%d 次调用 / %d tokens / %dms）",
                key, status, out.get("tool_calls", 0), out.get("tokens", 0), duration_ms)


async def scan_loop():
    loop = asyncio.get_event_loop()
    # 启动先做一轮基线扫描（不计 new，避免冷启动把预埋缺陷全报成"新增告警"）
    global _prev_open_keys
    try:
        baseline = json.loads(await loop.run_in_executor(
            None, lambda: registry.execute("run_risk_scan", {})))
        _prev_open_keys = {(f["rule_id"], f["resource_ref"])
                           for f in baseline.get("open_findings", [])}
        logger.info("基线扫描: %d 条 open（预埋缺陷，不计入 new）", len(_prev_open_keys))
    except Exception:  # noqa: BLE001
        logger.exception("基线扫描失败")
    while True:
        await asyncio.sleep(config.RISK_SCAN_INTERVAL_S)
        try:
            result = await loop.run_in_executor(None, _scan_once)
            logger.debug("定时扫描: %s", result)
        except Exception:  # noqa: BLE001
            logger.exception("定时扫描异常（本轮跳过）")


def start(app_loop=None) -> bool:
    """在 FastAPI startup 中调用；static 模式不启动。返回是否已启动。"""
    global _started
    if not config.is_live() or _started:
        return False
    asyncio.create_task(scan_loop())
    _started = True
    logger.info("定时风险扫描已启动: 每 %ss（live 模式，规则窗 %smin）",
                config.RISK_SCAN_INTERVAL_S, config.RULE_WINDOW_MINUTES)
    return True
