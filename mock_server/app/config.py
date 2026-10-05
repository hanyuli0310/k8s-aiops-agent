"""配置（mock_server）"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

def _num(env: str, default, lo=None, hi=None, cast=float):
    """读数值配置并做范围校验：非法值回退默认并告警，而不是让世界跑出荒谬状态。

    没有校验时 TICK_INTERVAL_S=0 会让 tick 循环空转打满 CPU、ENTRY_RPS=0 会让
    所有接口 qps 归零（演示直接失效）、TRACE_SAMPLE_RATE=5 则毫无意义。
    这类错配来自 .env 手改，静默接受比报错更难查。
    """
    raw = os.getenv(env)
    if raw is None or raw == "":
        return cast(default)
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        logger.warning("配置 %s=%r 不是合法数值，回退默认 %s", env, raw, default)
        return cast(default)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        logger.warning("配置 %s=%s 超出允许范围 [%s, %s]，回退默认 %s",
                       env, v, lo, hi, default)
        return cast(default)
    return v


PORT = int(_num("PORT", 9001, 1, 65535, int))
TICK_INTERVAL_S = int(_num("TICK_INTERVAL_S", 5, 1, 3600, int))
WORLD_SEED = int(_num("WORLD_SEED", 42, cast=int))

# KTD-13 数据量预算（四参数中属 mock 侧的两项）
ENTRY_RPS = _num("ENTRY_RPS", 5, 0.1, 10000)
TRACE_SAMPLE_RATE = _num("TRACE_SAMPLE_RATE", 0.1, 0.0, 1.0)

# 引擎缓冲窗口
CMS_WINDOW_MINUTES = 60      # CMS 分钟级环形缓冲
LOG_WINDOW_MINUTES = 30      # 日志/trace 事件缓冲

# 故障传播与恢复（KTD-5）
PROPAGATION_DECAY = 0.6      # 每跳效应衰减系数
PROPAGATION_MAX_HOPS = 3
RECOVERY_HALFLIFE_TICKS = 2  # 动作生效后指数衰减半衰期（拍）
