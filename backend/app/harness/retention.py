"""运行产物的保留期清理（工具结果落盘 / 审计 / 预诊断）。

为什么需要这个模块：这三类产物都是**只增不减**的。
  · `.tool_results/` 每个超长工具结果一个文件
  · `agent_audit` 每次工具调用一条
  · `prediagnosis` 每个新增 P1 一条

`tool_results.prune_old()` 早先就写好了，但一直没有调用点 —— 定义了却没接线，
等于注释里承诺的"防止磁盘无限增长"从未生效。本模块把三者收在一处并接到 startup。

与 data_collector 的 retention 区分开：那个清的是**观测数据**（可从 mock 重新采集），
这个清的是**运行痕迹**。两者保留期不同，且这里的审计有合规含义，默认留得更久。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .. import config, db
from . import tool_results

logger = logging.getLogger(__name__)


def run_once() -> dict:
    """执行一轮清理，返回各项删除数量。任何一项失败不影响其他项。"""
    result = {}

    # ① 工具结果落盘文件
    try:
        result["tool_result_files"] = tool_results.prune_old(
            max_age_s=int(config.TOOL_RESULT_RETENTION_HOURS * 3600))
    except Exception:                       # noqa: BLE001
        logger.exception("清理工具结果落盘失败")
        result["tool_result_files"] = -1

    # ② 审计记录。保留期更长：这是合规凭证，不是缓存。
    cutoff_ms = int((time.time() - config.AUDIT_RETENTION_DAYS * 86400) * 1000)
    result["audit_rows"] = _delete_older("agent_audit", cutoff_ms)

    # ③ 预诊断结论
    pd_cutoff_ms = int((time.time() - config.PREDIAG_RETENTION_DAYS * 86400) * 1000)
    result["prediagnosis_rows"] = _delete_older("prediagnosis", pd_cutoff_ms)

    if any(v for v in result.values() if isinstance(v, int) and v > 0):
        logger.info("运行产物清理: %s", result)
    return result


def _delete_older(table: str, cutoff_ms: int) -> int:
    """删除 created_at 早于 cutoff 的行。表名来自本模块常量，不接受外部输入。"""
    try:
        return db.execute(f"DELETE FROM {table} WHERE created_at < :c",
                          {"c": cutoff_ms}) or 0
    except Exception:                       # noqa: BLE001
        logger.exception("清理 %s 失败", table)
        return -1


_started = False


async def _loop():
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(config.RETENTION_INTERVAL_S)
        try:
            # 放 executor：删行是同步阻塞 IO，直接在事件循环里跑会卡住所有请求
            await loop.run_in_executor(None, run_once)
        except Exception:                   # noqa: BLE001
            logger.exception("定时清理异常（本轮跳过）")


def start() -> bool:
    """启动定时清理。RETENTION_INTERVAL_S<=0 表示只在启动时清一次。"""
    global _started
    if _started or config.RETENTION_INTERVAL_S <= 0:
        return False
    asyncio.create_task(_loop())
    _started = True
    logger.info("运行产物定时清理已启动：每 %ss（落盘 %sh / 审计 %sd / 预诊断 %sd）",
                config.RETENTION_INTERVAL_S, config.TOOL_RESULT_RETENTION_HOURS,
                config.AUDIT_RETENTION_DAYS, config.PREDIAG_RETENTION_DAYS)
    return True
