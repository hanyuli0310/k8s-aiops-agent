"""数据库层：优先 MySQL（DATABASE_URL），连不上自动降级 SQLite，同一套 SQLAlchemy Core 表结构。"""
from __future__ import annotations

import json
import logging
import time

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)

from . import config

logger = logging.getLogger(__name__)

metadata = MetaData()

# --- 可观测数据表 ---
metrics = Table(
    "metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("namespace", String(64), index=True),
    Column("metric_name", String(128), index=True),
    Column("dims_json", JSON),
    Column("ts", BigInteger, index=True),          # 毫秒
    Column("avg", Float), Column("max", Float), Column("min", Float),
)

ingress_logs = Table(
    "ingress_logs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True),          # 秒
    Column("method", String(16)),
    Column("url", String(255), index=True),
    Column("status", Integer, index=True),
    Column("request_time", Float),
    Column("upstream_addr", String(64)),
    Column("upstream_status", Integer),
    Column("client_ip", String(64)),
    Column("req_id", String(64), index=True),
    Column("source_pod", String(128)),
    Column("raw_json", JSON),
)

trace_spans = Table(
    "trace_spans", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trace_id", String(64), index=True),
    Column("span_id", String(32)),
    Column("parent_span_id", String(32), index=True),
    Column("service", String(64), index=True),
    Column("host", String(128)),
    Column("name", String(255), index=True),
    Column("kind", String(16)),
    Column("start_us", BigInteger),
    Column("duration_us", BigInteger, index=True),
    Column("status_code", String(16)),
    Column("status_message", Text),
    Column("attr_json", JSON),                     # 入库时已解开 JSON 字符串
    Column("resource_json", JSON),
    # index 必须与 collector 侧一致（tests/test_schema_sync.py 会校验）：
    # 拓扑构建的核心查询是 WHERE kind='client' AND ts >= :w，缺索引就是全表扫，
    # 且此前"谁先建表谁决定有没有索引"，性能不确定。
    Column("ts", BigInteger, index=True),
)

app_logs = Table(
    "app_logs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True),
    Column("source_pod", String(128), index=True),
    Column("level", String(8), index=True),
    Column("pod_ip", String(64)),
    Column("message", Text),
)

slow_logs = Table(
    "slow_logs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True),
    Column("instance_id", String(64)),
    Column("db_name", String(64)),
    Column("sql_text", Text),
    Column("query_time", Float),
    Column("lock_time", Float),
    Column("rows_examined", BigInteger),
    Column("rows_sent", Integer),
    Column("user_host", String(128)),
    Column("start_time", String(32)),
    Column("governed", Integer, server_default="0"),   # 治理标记：建索引后置 1
)

k8s_events = Table(
    "k8s_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True),
    Column("event_type", String(16), index=True),
    Column("reason", String(64)),
    Column("message", Text),
    Column("namespace", String(64)),
    Column("involved_object_kind", String(32)),
    Column("involved_object_name", String(128)),
)

k8s_resources = Table(
    "k8s_resources", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(32), index=True),
    Column("namespace", String(64)),
    Column("name", String(128), index=True),
    Column("spec_json", JSON),
)

# --- Agent 产出表 ---
topology_edges = Table(
    "topology_edges", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String(64)),
    Column("target", String(64)),
    Column("call_count", Integer),
    Column("error_rate", Float),
    Column("avg_ms", Float),
    Column("p99_ms", Float),
)

risk_rules = Table(
    "risk_rules", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("rule_id", String(32), unique=True),
    Column("source", String(16)),                  # builtin | ai
    Column("severity", String(8)),
    Column("title", String(255)),
    Column("check_type", String(32)),              # python | sql_threshold
    Column("check_ref", Text),                     # builtin 函数名 或 SQL 语句
    Column("threshold", Float),
    Column("compare", String(8)),                  # gt / lt（sql_threshold 用）
    Column("enabled", Integer, server_default="1"),
)

risk_findings = Table(
    "risk_findings", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("rule_id", String(32), index=True),
    Column("severity", String(8)),
    Column("title", String(255)),
    Column("resource_ref", String(128)),
    Column("evidence_json", JSON),
    Column("status", String(16), server_default="open"),   # open | resolved
    Column("suggestion", Text),
    Column("scan_ts", BigInteger),
)

agent_memory = Table(
    "agent_memory", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scope", String(32), index=True),       # conclusion | governance | preference
    Column("mem_key", String(128), index=True),
    Column("content", Text),
    Column("created_at", BigInteger),
)

# 工具调用审计（P1-4）：记录每次权限决策与执行结果，供事后追溯
# 「这条变更是谁批准的、依据什么、结果如何」。
#
# 只在 backend 定义 —— collector 完全不涉及本表，不属于 CLAUDE.md §8.1
# 要求双定义同步的那 8 张契约表（7 张观测表 + realtime_metrics）。
# 与 agent_memory / chat_messages / scan_reports / governance_plans 同类。
agent_audit = Table(
    "agent_audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(64), index=True),
    Column("agent_name", String(64)),
    Column("tool_name", String(64), index=True),
    Column("args_json", Text),
    Column("audit_repr", String(255)),             # 人类可读摘要
    Column("decision", String(16)),                # allow | ask | deny
    Column("reason_type", String(32)),             # rule|mode|tool|user|auto|auto_approve
    Column("run_mode", String(16)),                # readonly | confirm | auto
    Column("is_destructive", Integer),             # 0/1，便于按破坏性动作筛选
    Column("result_status", String(16)),           # ok|error|denied|rejected|timeout|aborted
    Column("duration_ms", Integer),
    Column("created_at", BigInteger, index=True),  # 毫秒
)


# P2-7 自主预诊断：定时扫描发现【新增 P1】时，自动派 diagnose 子 Agent 预分析根因。
# 单独建表而不复用 agent_memory：memory_prompt 会把 recall 到的记忆全部注入系统
# 提示词，长篇诊断报告塞进去会挤爆上下文且大多数轮次并不需要它。
prediagnosis = Table(
    "prediagnosis", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("finding_key", String(160), index=True),  # rule_id@resource_ref，去重用
    Column("rule_id", String(32)),
    Column("resource_ref", String(128)),
    Column("severity", String(8)),
    Column("title", Text),
    Column("conclusion", Text),                      # 子 Agent 的根因分析结论
    Column("tool_calls", Integer),
    Column("tokens", Integer),
    Column("status", String(16)),                    # ok | failed
    Column("duration_ms", Integer),
    Column("created_at", BigInteger, index=True),    # 毫秒
)

chat_messages = Table(
    "chat_messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(64), index=True),
    Column("role", String(16)),
    Column("content", Text),
    Column("tool_calls_json", JSON),
    Column("created_at", BigInteger),
)

# --- 契约 C 新表（spec v1.1）---

realtime_metrics = Table(
    "realtime_metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, index=True),               # 毫秒
    Column("service", String(64), index=True),
    Column("instance", String(128), index=True),
    Column("kind", String(16)),                          # pod | rds | redis
    Column("metric", String(64), index=True),            # cpu_pct/mem_pct/conn_pct/...
    Column("value", Float),
    Column("status", String(16)),
    Column("raw_json", JSON),                            # 写入端 collector，读取端 backend
)

scan_reports = Table(
    "scan_reports", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scan_ts", BigInteger, index=True),
    Column("trigger", String(16)),                       # schedule | manual
    Column("total_findings", Integer),
    Column("new_findings", Integer),
    Column("resolved", Integer),
    Column("summary_json", JSON),
)

governance_plans = Table(
    "governance_plans", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("finding_ids_json", JSON),
    Column("solution_md", Text),
    Column("checklist_json", JSON),
    Column("status", String(16), server_default="draft"),  # draft|confirmed|executing|completed|failed
    Column("created_at", BigInteger),
    Column("executed_at", BigInteger),
    Column("result_json", JSON),
)

_engine = None


def get_engine():
    """优先 MySQL，失败降级 SQLite（同一套表结构）。"""
    global _engine
    if _engine is not None:
        return _engine
    if config.DATABASE_URL:
        try:
            eng = create_engine(config.DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            _engine = eng
            logger.info("using MySQL: %s", config.DATABASE_URL.split("@")[-1])
            return _engine
        except Exception as e:  # noqa: BLE001
            logger.warning("MySQL 不可用（%s），降级 SQLite", e)
    _engine = create_engine(config.SQLITE_URL)
    logger.info("using SQLite: %s", config.SQLITE_URL)
    return _engine


def init_db():
    metadata.create_all(get_engine())


def fetch_all(sql: str, params: dict | None = None) -> list[dict]:
    with get_engine().connect() as conn:
        rows = conn.execute(text(sql), params or {})
        return [dict(r._mapping) for r in rows]


def fetch_one(sql: str, params: dict | None = None) -> dict | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: dict | None = None) -> int:
    with get_engine().begin() as conn:
        result = conn.execute(text(sql), params or {})
        return result.rowcount


def bulk_insert(table: Table, rows: list[dict], chunk: int = 1000):
    eng = get_engine()
    with eng.begin() as conn:
        for i in range(0, len(rows), chunk):
            conn.execute(table.insert(), rows[i:i + chunk])


# 观测表的时间列单位。这几张表**单位不统一**（历史原因：日志类来自 SLS 的秒级
# __time__，指标类来自 CMS 的毫秒 timestamp），混用会算出几十年的荒谬时间差
# —— 本模块的 data_freshness 第一版就把 trace_spans 当成毫秒，得出"距今 56 年"。
# data_collector/collector/retention.py 里有同一份表，
# tests/test_schema_sync.py 会校验两边一致，避免又一处双定义漂移。
OBSERVED_TS_UNIT = {
    "ingress_logs": "s", "app_logs": "s", "slow_logs": "s",
    "k8s_events": "s", "trace_spans": "s",
    "metrics": "ms", "realtime_metrics": "ms",
}


def data_freshness() -> dict:
    """观测数据新鲜度：各表最新记录距今多少秒。

    这是从【事实】推断采集健康，而不是依赖 collector 主动上报 ——
    上报通道本身也会挂，而"最新数据是多久以前的"无法被伪造。
    live 模式下 realtime_metrics 应每 10s 更新，超过阈值即说明采集断流。

    返回 {table: age_s | None}。None = 表为空（从未采集过）。
    """
    now_s = time.time()
    out = {}
    for table, unit in OBSERVED_TS_UNIT.items():
        try:
            row = fetch_one(f"SELECT MAX(ts) AS m FROM {table}")
        except Exception:                      # noqa: BLE001, PERF203
            out[table] = None
            continue
        mx = (row or {}).get("m")
        if not mx:
            out[table] = None
            continue
        latest_s = mx / 1000.0 if unit == "ms" else float(mx)
        out[table] = max(0, int(now_s - latest_s))
    return out


def table_counts() -> dict:
    names = ["metrics", "ingress_logs", "trace_spans", "app_logs", "slow_logs",
             "k8s_events", "k8s_resources", "topology_edges", "risk_rules", "risk_findings",
             "realtime_metrics", "scan_reports", "governance_plans"]
    out = {}
    for n in names:
        try:
            out[n] = fetch_one(f"SELECT COUNT(*) AS c FROM {n}")["c"]
        except Exception:  # noqa: BLE001
            out[n] = 0
    return out


def json_load(v):
    """兼容 MySQL JSON（已是 dict）与 SQLite（字符串）；递归解析防双重编码。"""
    for _ in range(3):      # 最多解三层，防异常数据死循环
        if v is None or isinstance(v, (dict, list)):
            return v
        try:
            v = json.loads(v)
        except Exception:  # noqa: BLE001
            return v
    return v
