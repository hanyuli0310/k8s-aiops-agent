"""data_collector 入口（U5）：asyncio 双通道调度。

定时通道（TOPO_INTERVAL_S，默认 60s）：拓扑类 + 日志 + CMS 指标 + 保留期清理
实时通道（REALTIME_INTERVAL_S，默认 10s）：水位/容量/带宽

启动：python -m collector.main
两个通道各自独立循环，互不阻塞；mock_server 不可达时退避重试、不崩溃。
"""
from __future__ import annotations

import asyncio
import logging

from . import config, db, realtime, retention, scheduled
from .client import MockClient

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [collector] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# 连续失败到达此数后把日志升级为 ERROR 并给出可操作提示。
# 原实现每轮都 logger.exception，栈刷屏但看不出"已经连续挂了多久"——
# 偶发一次网络抖动和 mock_server 整体宕掉在日志里长得一样。
_ALERT_AFTER = 3


def _on_failure(channel: str, streak: int, interval_s: float):
    """失败日志分级：首次记栈，连续失败则升级为 ERROR 并算出断流时长。"""
    if streak < _ALERT_AFTER:
        logger.exception("%s采集失败（第 %d 次，本轮跳过）", channel, streak)
        return
    logger.error("%s采集已连续失败 %d 次（约 %.0f 秒无新数据）——"
                 "请检查 mock_server 是否可达（%s）与数据库连接",
                 channel, streak, streak * interval_s, config.MOCK_SERVER_URL)


async def scheduled_loop(client: MockClient):
    wm = scheduled.Watermark()
    wm.load_from_db()
    round_no = 0
    streak = 0
    while True:
        round_no += 1
        try:
            report = await scheduled.run_once(client, wm)
            written = {k: v for k, v in report.items() if v}
            if streak >= _ALERT_AFTER:
                logger.info("定时采集已恢复（此前连续失败 %d 次）", streak)
            streak = 0
            logger.info("定时采集 #%d: %s", round_no, written or "无增量")
            retention.run_once()
        except Exception:      # noqa: BLE001
            streak += 1
            _on_failure("定时", streak, config.TOPO_INTERVAL_S)
        await asyncio.sleep(config.TOPO_INTERVAL_S)


async def realtime_loop(client: MockClient):
    round_no = 0
    streak = 0
    while True:
        round_no += 1
        try:
            n = await realtime.run_once(client)
            if streak >= _ALERT_AFTER:
                logger.info("实时采集已恢复（此前连续失败 %d 次）", streak)
            streak = 0
            if round_no % 6 == 1:      # 每分钟打一条，避免日志刷屏
                logger.info("实时采集 #%d: %d 行", round_no, n)
        except Exception:      # noqa: BLE001
            streak += 1
            _on_failure("实时", streak, config.REALTIME_INTERVAL_S)
        await asyncio.sleep(config.REALTIME_INTERVAL_S)


async def main():
    logger.info("collector 启动 | mock=%s | topo=%ss realtime=%ss | 保留 观测%sh/实时%sh",
                config.MOCK_SERVER_URL, config.TOPO_INTERVAL_S, config.REALTIME_INTERVAL_S,
                config.RETENTION_HOURS, config.REALTIME_RETENTION_HOURS)
    db.init_db()
    client = MockClient()
    if not await client.wait_ready():
        logger.error("mock_server 未就绪，退出")
        await client.close()
        return
    try:
        await asyncio.gather(scheduled_loop(client), realtime_loop(client))
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("collector 已停止")
