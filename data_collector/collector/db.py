"""collector 侧 DB 层（U5）：与 backend/app/db.py **契约同构**双定义。

同构范围 = 契约 C：7 张观测数据表 + realtime_metrics。
不 import backend 的 db.py（Rejected Alternatives 已决策：跨目录引用脆弱），
两端字段以 spec 契约 C 为唯一基准；U16 联调时做两端表结构对账。

SQLite 时启用 WAL + busy_timeout（KTD-7 跨进程并发写）。
"""
from __future__ import annotations

import logging

from sqlalchemy import (
    JSON, BigInteger, Column, Float, Integer, MetaData, String, Table, Text,
    create_engine, event, text,
)

from . import config

logger = logging.getLogger(__name__)
metadata = MetaData()

metrics = Table(
    "metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("namespace", String(64), index=True),
    Column("metric_name", String(128), index=True),
    Column("dims_json", JSON),
    Column("ts", BigInteger, index=True),
    Column("avg", Float), Column("max", Float), Column("min", Float),
)

ingress_logs = Table(
    "ingress_logs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True),
    Column("method", String(16)), Column("url", String(255), index=True),
    Column("status", Integer, index=True), Column("request_time", Float),
    Column("upstream_addr", String(64)), Column("upstream_status", Integer),
    Column("client_ip", String(64)), Column("req_id", String(64), index=True),
    Column("source_pod", String(128)), Column("raw_json", JSON),
)

trace_spans = Table(
    "trace_spans", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trace_id", String(64), index=True), Column("span_id", String(32)),
    Column("parent_span_id", String(32), index=True),
    Column("service", String(64), index=True), Column("host", String(128)),
    Column("name", String(255), index=True), Column("kind", String(16)),
    Column("start_us", BigInteger), Column("duration_us", BigInteger, index=True),
    Column("status_code", String(16)), Column("status_message", Text),
    Column("attr_json", JSON), Column("resource_json", JSON),
    Column("ts", BigInteger, index=True),
)

app_logs = Table(
    "app_logs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True), Column("source_pod", String(128), index=True),
    Column("level", String(8), index=True), Column("pod_ip", String(64)), Column("message", Text),
)

slow_logs = Table(
    "slow_logs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True), Column("instance_id", String(64)),
    Column("db_name", String(64)), Column("sql_text", Text),
    Column("query_time", Float), Column("lock_time", Float),
    Column("rows_examined", BigInteger), Column("rows_sent", Integer),
    Column("user_host", String(128)), Column("start_time", String(32)),
    Column("governed", Integer, server_default="0"),
)

k8s_events = Table(
    "k8s_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True), Column("event_type", String(16), index=True),
    Column("reason", String(64)), Column("message", Text), Column("namespace", String(64)),
    Column("involved_object_kind", String(32)), Column("involved_object_name", String(128)),
)

k8s_resources = Table(
    "k8s_resources", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(32), index=True), Column("namespace", String(64)),
    Column("name", String(128), index=True), Column("spec_json", JSON),
)

# 契约 C 新表：实时通道（水位/容量/带宽），写入端 collector、读取端 backend
realtime_metrics = Table(
    "realtime_metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True),
    Column("service", String(64), index=True),
    Column("instance", String(128), index=True),
    Column("kind", String(16)),
    Column("metric", String(64), index=True),
    Column("value", Float),
    Column("status", String(16)),
    Column("raw_json", JSON),
)

_engine = None


def get_engine():
    global _engine
    if _engine is not None:
        return _engine
    url = config.DB_URL
    if url.startswith("sqlite"):
        _engine = create_engine(url, connect_args={"timeout": 30})

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _rec):      # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")     # KTD-7 跨进程并发写
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    else:
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
    logger.info("collector DB: %s", url.split("@")[-1] if "@" in url else url)
    return _engine


def init_db():
    eng = get_engine()
    metadata.create_all(eng)
    if config.DB_URL.startswith("sqlite"):
        with eng.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
            logger.info("sqlite journal_mode=%s", mode)


def bulk_insert(table: Table, rows: list, chunk: int = 500):
    """批量写入，按批次单事务（KTD-7 降低锁竞争）。"""
    if not rows:
        return 0
    eng = get_engine()
    with eng.begin() as conn:
        for i in range(0, len(rows), chunk):
            conn.execute(table.insert(), rows[i:i + chunk])
    return len(rows)


def execute(sql: str, params: dict = None) -> int:
    with get_engine().begin() as conn:
        return conn.execute(text(sql), params or {}).rowcount


def scalar(sql: str, params: dict = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def replace_table(table: Table, rows: list) -> int:
    """全量替换（K8s 资源快照语义，KTD-8 例外项）：单事务内清空+写入。"""
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(table.delete())
        for i in range(0, len(rows), 500):
            conn.execute(table.insert(), rows[i:i + 500])
    return len(rows)


def json_dump(v):
    """JSON 列直接传 dict/list：SQLAlchemy JSON 类型对 MySQL/SQLite 都会自动序列化，
    预先 dumps 会造成双重编码（JSON 列里存了个 JSON 字符串标量，backend 解一次得到 str）。"""
    return v
