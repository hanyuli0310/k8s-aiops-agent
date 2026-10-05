"""data_collector 配置（U5）"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

MOCK_SERVER_URL = os.getenv("MOCK_SERVER_URL", "http://localhost:9001")

# 采集间隔（KTD-3 定频轮询）
def _num(env: str, default, lo=None, hi=None, cast=float):
    """读数值配置并做范围校验。非法值回退默认并告警。

    间隔为 0 或负数会让采集循环空转打满 CPU；PAGE_SIZE 为 0 则永远取不到数据，
    表现为"采集一直无增量"，很难从日志看出是配置问题。
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


TOPO_INTERVAL_S = int(_num("TOPO_INTERVAL_S", 60, 1, 86400, int))
REALTIME_INTERVAL_S = int(_num("REALTIME_INTERVAL_S", 10, 1, 3600, int))

# KTD-8 保留期：观测数据 2h（防定时扫描被拖死），realtime 24h（行小）
RETENTION_HOURS = _num("RETENTION_HOURS", 2, 0.1, 24 * 30)
REALTIME_RETENTION_HOURS = _num("REALTIME_RETENTION_HOURS", 24, 0.1, 24 * 30)

PAGE_SIZE = int(_num("PAGE_SIZE", 2000, 1, 100000, int))
HTTP_TIMEOUT_S = _num("HTTP_TIMEOUT_S", 20, 0.5, 600)

# --- 数据库：与 backend 同一套库（默认 SQLite，开发期避免污染共享 RDS）---
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "ai_devops_cmdb_lin")


def build_db_url() -> str:
    """优先 DB_URL；否则用 DB_* 组装（密码自动转义）；都没有则落 backend/aiops.db。"""
    explicit = os.getenv("DB_URL", "")
    if explicit:
        return explicit
    if DB_HOST:
        return (f"mysql+pymysql://{quote_plus(DB_USER)}:{quote_plus(DB_PASSWORD)}"
                f"@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4")
    return f"sqlite:///{(BASE_DIR.parent / 'backend' / 'aiops.db').resolve()}"


DB_URL = build_db_url()
