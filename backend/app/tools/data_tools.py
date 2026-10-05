"""数据查询工具：指标 / 日志 / Trace / K8s 资源 / 只读 SQL / 采集。"""
from __future__ import annotations

import json
import math
import re
import time

from .. import config, db
from ..ingest import pipeline
from .registry import tool


def _perf_window_s() -> int:
    """U9.5：live 模式接口统计只看最近窗口；static 全表。"""
    if not config.is_live():
        return 0
    return int(time.time() - config.RULE_WINDOW_MINUTES * 60)


@tool(
    "ingest_data",
    "从阿里云 CMS/SLS/K8s（Mock Provider）全量采集可观测数据入库，返回各表入库行数报告。已有数据会被覆盖，幂等可重复执行。",
    is_destructive=True,
    check_permissions=lambda a: "deny" if config.is_live() else "ask",
    audit_repr=lambda a: "全量数据采集（清空并重建七张观测表）",
)
def ingest_data():
    if config.is_live():
        return {"status": "skipped",
                "note": "live 模式由 data_collector 每 10~60s 持续采集，无需也不允许手动全量重灌（会清掉增量数据）"}
    report = pipeline.run_full_ingest()
    return {"status": "ok", "ingested": report}


@tool(
    "query_metrics",
    "查询 CMS 监控指标的聚合统计。支持按 namespace（acs_k8s/acs_rds_dashboard/acs_kvstore）与 metric_name 过滤，"
    "返回各维度组的均值/最大/最小与数据点数。不传参数时返回全部指标清单。",
    {
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "description": "指标命名空间，如 acs_rds_dashboard"},
            "metric_name": {"type": "string", "description": "指标名，如 ConnectionUsage / namespace.cpu.oversale_rate"},
            "dim_filter": {"type": "string",
                           "description": "维度值过滤（LIKE 匹配 dims_json），填实例 ID 或 pod 名；"
                                          "不确定叫什么名字时先不传此参，从返回的维度组里看"},
        },
    },
    is_read_only=True, concurrency_safe=True, max_result_chars=4000
)
def query_metrics(namespace: str = None, metric_name: str = None, dim_filter: str = None):
    if not namespace and not metric_name:
        # 目录型返回也要带数据时间窗：模型有时直接拿这份"有哪些指标、各多少点"
        # 去判断集群是否在正常采集，而没有时间戳时它无法（核对器也无法）看出
        # 这些点可能全是一小时前的。
        rows = db.fetch_all(
            "SELECT namespace, metric_name, COUNT(*) AS points,"
            "       MIN(ts) AS data_from_ms, MAX(ts) AS data_to_ms"
            " FROM metrics GROUP BY namespace, metric_name")
        newest = 0
        for r in rows:
            for src, dst in (("data_from_ms", "data_from"), ("data_to_ms", "data_to")):
                ms = r.pop(src, None)
                if ms:
                    sec = int(ms) / 1000.0
                    r[dst] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(sec))
                    newest = max(newest, sec)
        out = {"available_metrics": rows}
        if newest:
            out["data_age_note"] = (f"最新数据点在 {int(time.time() - newest)} 秒前。")
        return out
    where, params = [], {}
    if namespace:
        where.append("namespace = :ns")
        params["ns"] = namespace
    if metric_name:
        where.append("metric_name = :mn")
        params["mn"] = metric_name
    if dim_filter:
        where.append("dims_json LIKE :df")
        params["df"] = f"%{dim_filter}%"
    # ★ 聚合里必须带上 MIN(ts)/MAX(ts)：否则这份结果里**一个时间戳都没有**，
    #   而模型会拿它回答"当前水位是多少"。后果有两层：
    #   1) 模型自己不知道手上数据有多旧，可能把一小时前的均值说成"当前"；
    #   2) 事实核对的 C 类（数据时效）完全失效 —— 它靠工具结果里的时间戳判断
    #      数据龄期，没有时间戳就等于没有眼睛。实测过：query_metrics /
    #      api_perf_stats / get_risk_report 返回的时间戳数都是 0，
    #      于是自动刷新复核在这些工具上从来不会触发。
    sql = f"""SELECT namespace, metric_name, dims_json,
                     COUNT(*) AS points, AVG(avg) AS avg_value, MAX(max) AS max_value, MIN(min) AS min_value,
                     MIN(ts) AS data_from_ms, MAX(ts) AS data_to_ms
              FROM metrics WHERE {' AND '.join(where)}
              GROUP BY namespace, metric_name, dims_json ORDER BY avg_value DESC LIMIT 30"""
    rows = db.fetch_all(sql, params)
    newest = 0
    for r in rows:
        r["dims_json"] = db.json_load(r["dims_json"])
        for k in ("avg_value", "max_value", "min_value"):
            if r[k] is not None:
                r[k] = round(r[k], 2)
        # 毫秒 epoch 转可读时间：核对器与模型都更容易识别
        for src, dst in (("data_from_ms", "data_from"), ("data_to_ms", "data_to")):
            ms = r.pop(src, None)
            if ms:
                sec = int(ms) / 1000.0
                r[dst] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(sec))
                newest = max(newest, sec)
    out = {"series": rows}
    if newest:
        age = int(time.time() - newest)
        out["data_age_note"] = (
            f"本结果的最新数据点在 {age} 秒前（{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(newest))}）。"
            f"若要回答『当前』状态且该值明显偏大，请重新查询后再下结论。")
    return out


@tool(
    "query_logs",
    "查询日志。logstore 可选：app（应用日志，含 level/message）、slow（RDS 慢日志）、event（K8s 事件）、"
    "ingress（Nginx 访问日志）。keyword 对消息/SQL 全文匹配。",
    {
        "type": "object",
        "properties": {
            "logstore": {"type": "string", "enum": ["app", "slow", "event", "ingress"]},
            "keyword": {"type": "string", "description": "全文关键词，如 'Too many connections'"},
            "level": {"type": "string", "description": "仅 app 日志有效：INFO/WARN/ERROR"},
            "url": {"type": "string", "description": "仅 ingress 有效：接口路径，如 /api/orders"},
            "status_ge": {"type": "integer", "description": "仅 ingress 有效：状态码下限，如 500"},
            "minutes": {"type": "integer",
                         "description": "只看最近多少分钟（默认 30）。排障用默认值即可；要查更早的历史再放大，传 0 表示不限时间"},
            "limit": {"type": "integer", "description": "返回条数，默认 10"},
        },
        "required": ["logstore"],
    },
    is_read_only=True, concurrency_safe=True, max_result_chars=6000
)
def query_logs(logstore: str, keyword: str = None, level: str = None,
               url: str = None, status_ge: int = None, limit: int = 10,
               minutes: int = None):
    """查日志。**默认只看最近一段时间**，并按时间倒序返回最新的。

    ⚠️ 这里修过一个会导致排障结论系统性错误的缺陷（量化评测时发现）：
    原实现是 `WHERE 1=1 ... ORDER BY ts LIMIT n` —— 既没有任何时间窗口，
    排序还是**升序**，于是返回的是库里**最旧**的日志。

    真机后果：库里存有两小时前一次故障演练的日志，Agent 排查"当前"故障时
    query_logs 返回的全是那次演练的记录，于是它报告"orderdb 慢查询风暴 +
    payment-service OOM 叠加"，而当时实际注入的是 Redis 缓存雪崩 ——
    **结论完全错误，但每一条引用的日志都真实存在**，事实核对也查不出来
    （数据确实在库里，只是时间不对）。

    单测覆盖不到这类问题：测试数据都是刚写入的，不存在"新旧数据混杂"，
    升序和倒序取到的是同一批。只有在长期运行、库里积累了历史数据的
    真实条件下才会暴露。

    minutes=None 用 config 默认；显式传 0 表示不限时间（导出/审计场景）。
    """
    limit = min(limit or 10, 50)
    win = config.LOG_QUERY_DEFAULT_MINUTES if minutes is None else minutes
    params = {"limit": limit}
    # 日志类表的 ts 是【秒】（metrics 才是毫秒），这里按秒算窗口
    time_where = []
    if win and win > 0:
        params["since"] = int(time.time() - win * 60)
        time_where = ["ts >= :since"]

    if logstore == "app":
        where = ["1=1"] + time_where
        if keyword:
            where.append("message LIKE :kw")
            params["kw"] = f"%{keyword}%"
        if level:
            where.append("level = :lv")
            params["lv"] = level.upper()
        # 倒序：排障要看最新的，不是最早的
        sql = f"SELECT ts, source_pod, level, pod_ip, message FROM app_logs WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT :limit"
    elif logstore == "slow":
        where = ["1=1"] + time_where
        if keyword:
            where.append("sql_text LIKE :kw")
            params["kw"] = f"%{keyword}%"
        sql = f"""SELECT ts, instance_id, db_name, sql_text, query_time, rows_examined, rows_sent, user_host, governed
                  FROM slow_logs WHERE {' AND '.join(where)} ORDER BY query_time DESC LIMIT :limit"""
    elif logstore == "event":
        where = ["1=1"] + time_where
        if keyword:
            where.append("message LIKE :kw")
            params["kw"] = f"%{keyword}%"
        sql = f"""SELECT ts, event_type, reason, message, namespace, involved_object_kind, involved_object_name
                  FROM k8s_events WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT :limit"""
    else:  # ingress
        where = ["1=1"] + time_where
        if url:
            where.append("url = :url")
            params["url"] = url
        if status_ge:
            where.append("status >= :sge")
            params["sge"] = status_ge
        if keyword:
            where.append("(req_id LIKE :kw OR url LIKE :kw)")
            params["kw"] = f"%{keyword}%"
        sql = f"""SELECT ts, method, url, status, request_time, upstream_addr, req_id
                  FROM ingress_logs WHERE {' AND '.join(where)} ORDER BY request_time DESC LIMIT :limit"""
    # 把窗口一并回给模型：让它知道自己看的是哪段时间，
    # 否则它无法判断"没查到"是真没有还是窗口太窄
    return {"logstore": logstore, "window_minutes": win or "全部",
            "rows": db.fetch_all(sql, params)}


@tool(
    "api_perf_stats",
    "按接口（method+url）聚合 Ingress 访问日志，返回每个接口的请求数、P99 时延（秒）、5xx 错误率。"
    "用于发现慢接口/高错误率接口，是故障排查第一步。",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "可选，仅统计指定接口路径"},
        },
    },
    is_read_only=True, concurrency_safe=True
)
def api_perf_stats(url: str = None):
    w = _perf_window_s()
    where, params = "WHERE ts >= :w", {"w": w}
    if url:
        where += " AND url = :url"
        params["url"] = url
    groups = db.fetch_all(
        f"SELECT method, url, COUNT(*) AS cnt FROM ingress_logs {where} GROUP BY method, url", params)
    out = []
    for g in groups:
        rows = db.fetch_all(
            "SELECT request_time, status FROM ingress_logs WHERE method=:m AND url=:u AND ts >= :w",
            {"m": g["method"], "u": g["url"], "w": w})
        ts = sorted(r["request_time"] for r in rows)
        p99 = ts[math.ceil(0.99 * len(ts)) - 1] if ts else 0
        err = sum(1 for r in rows if r["status"] >= 500) / len(rows) if rows else 0
        out.append({"api": f"{g['method']} {g['url']}", "requests": g["cnt"],
                    "p99_s": round(p99, 3), "error_rate_pct": round(err * 100, 2)})
    out.sort(key=lambda x: x["p99_s"], reverse=True)
    # 与 query_metrics 同理：聚合结果不带时间戳时，模型会拿它回答"当前"，
    # 而事实核对的 C 类（数据时效）也看不见 —— 必须显式带上数据时间窗。
    # ingress_logs.ts 是**秒级**（同库里 metrics 是毫秒，单位不统一，勿混用）。
    span = db.fetch_one(
        f"SELECT MIN(ts) AS t0, MAX(ts) AS t1 FROM ingress_logs {where}", params)
    res = {"api_stats": out}
    if span and span.get("t1"):
        fmt = "%Y-%m-%dT%H:%M:%S"
        newest = int(span["t1"])
        res["data_from"] = time.strftime(fmt, time.localtime(int(span["t0"])))
        res["data_to"] = time.strftime(fmt, time.localtime(newest))
        age = int(time.time() - newest)
        res["data_age_note"] = (f"本结果的最新日志在 {age} 秒前；"
                                f"回答『当前』状态前请确认这份数据足够新。")
    return res


@tool(
    "query_traces",
    "查询分布式链路。三种用法：1) 传 trace_id 返回该 trace 全部 span（按耗时排列，含 SQL 语句与错误信息）；"
    "2) 传 api_name（如 'POST /api/orders'）返回该接口最慢/出错的根 span 及其 trace_id；"
    "3) 传 slow_ms 返回耗时超过该毫秒数的根 span。",
    {
        "type": "object",
        "properties": {
            "trace_id": {"type": "string"},
            "api_name": {"type": "string", "description": "根 span 名，如 POST /api/orders"},
            "slow_ms": {"type": "number", "description": "根 span 耗时阈值（毫秒）"},
            "only_error": {"type": "boolean", "description": "只看 ERROR 状态"},
            "limit": {"type": "integer", "description": "默认 5"},
        },
    },
    is_read_only=True, concurrency_safe=True, max_result_chars=6000
)
def query_traces(trace_id: str = None, api_name: str = None, slow_ms: float = None,
                 only_error: bool = False, limit: int = 5):
    limit = min(limit or 5, 20)
    if trace_id:
        spans = db.fetch_all(
            """SELECT span_id, parent_span_id, service, name, kind, duration_us, status_code, status_message, attr_json
               FROM trace_spans WHERE trace_id = :tid ORDER BY start_us""", {"tid": trace_id})
        out = []
        for s in spans:
            attr = db.json_load(s["attr_json"]) or {}
            out.append({
                "service": s["service"], "kind": s["kind"], "name": s["name"],
                "duration_ms": round(s["duration_us"] / 1000, 1),
                "status": s["status_code"], "error": s["status_message"] or None,
                "db_statement": attr.get("db.statement"),
            })
        return {"trace_id": trace_id, "span_count": len(spans), "spans": out}
    where = ["parent_span_id = ''"]
    params = {"limit": limit}
    if api_name:
        where.append("name = :n")
        params["n"] = api_name
    if slow_ms:
        where.append("duration_us >= :d")
        params["d"] = int(slow_ms * 1000)
    if only_error:
        where.append("status_code = 'ERROR'")
    rows = db.fetch_all(
        f"""SELECT trace_id, service, name, duration_us, status_code, status_message
            FROM trace_spans WHERE {' AND '.join(where)} ORDER BY duration_us DESC LIMIT :limit""", params)
    return {"root_spans": [{
        "trace_id": r["trace_id"], "api": r["name"], "entry_service": r["service"],
        "duration_ms": round(r["duration_us"] / 1000, 1), "status": r["status_code"],
    } for r in rows]}


@tool(
    "get_k8s_resource",
    "查询 K8s 资源快照。kind 可选 Node/Deployment/Pod/Service/PodDisruptionBudget；"
    "不传 name 返回该类资源摘要列表，传 name 返回完整 spec JSON。",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["Node", "Deployment", "Pod", "Service", "PodDisruptionBudget"]},
            "name": {"type": "string"},
        },
        "required": ["kind"],
    },
    is_read_only=True, concurrency_safe=True, max_result_chars=8000
)
def get_k8s_resource(kind: str, name: str = None):
    if name:
        row = db.fetch_one("SELECT spec_json FROM k8s_resources WHERE kind=:k AND name=:n",
                           {"k": kind, "n": name})
        if not row:
            return {"error": f"{kind}/{name} not found"}
        return db.json_load(row["spec_json"])
    rows = db.fetch_all("SELECT namespace, name, spec_json FROM k8s_resources WHERE kind=:k", {"k": kind})
    out = []
    for r in rows:
        spec = db.json_load(r["spec_json"])
        item = {"namespace": r["namespace"], "name": r["name"]}
        if kind == "Deployment":
            tpl = spec["spec"]["template"]["spec"]["containers"][0]
            item.update({
                "replicas": spec["spec"]["replicas"],
                "image": tpl["image"],
                "resources": tpl.get("resources", {}),
                "has_liveness": "livenessProbe" in tpl,
                "has_readiness": "readinessProbe" in tpl,
            })
        elif kind == "Pod":
            item.update({"node": spec["spec"].get("nodeName"), "pod_ip": spec["status"].get("podIP"),
                         "app": spec["metadata"].get("labels", {}).get("app")})
        elif kind == "Node":
            item.update({"zone": spec["metadata"]["labels"].get("topology.kubernetes.io/zone"),
                         "capacity": spec["status"]["capacity"], "allocatable": spec["status"]["allocatable"]})
        elif kind == "PodDisruptionBudget":
            item.update({"selector_app": spec["spec"]["selector"]["matchLabels"].get("app")})
        out.append(item)
    return {"kind": kind, "items": out}


_SQL_DENY = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|replace)\b", re.I)
_ALLOWED_TABLES = {"metrics", "realtime_metrics", "ingress_logs", "trace_spans",
                   "app_logs", "slow_logs", "k8s_events", "k8s_resources",
                   "topology_edges", "risk_rules", "risk_findings"}
# realtime_metrics 在白名单里：live 模式下它是**最新**的分实例水位（每 10s 一点），
# 比 metrics 表的分钟级聚合更适合回答"现在哪个实例高"。
# tests/test_skill_accuracy.py 会校验本集合与 sql_analytics.md 的表清单严格相等。


@tool(
    "sql_query",
    "对可观测数据库执行只读 SELECT（兜底自由查询）。可用表：metrics(namespace,metric_name,dims_json,ts,avg,max,min)、"
    "realtime_metrics(ts,service,instance,kind,metric,value,status)、"
    "ingress_logs(ts,method,url,status,request_time,upstream_addr,req_id)、"
    "trace_spans(trace_id,span_id,parent_span_id,service,name,kind,duration_us,status_code,status_message)、"
    "app_logs(ts,source_pod,level,pod_ip,message)、slow_logs(ts,sql_text,query_time,rows_examined,rows_sent,user_host,governed)、"
    "k8s_events(ts,event_type,reason,message)、k8s_resources(kind,namespace,name,spec_json)、"
    "topology_edges(source,target,call_count,error_rate,avg_ms,p99_ms)、risk_findings(rule_id,severity,title,status)。"
    "结果最多 50 行。",
    {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "一条 SELECT 语句"}},
        "required": ["sql"],
    },
    is_read_only=True, concurrency_safe=True, max_result_chars=6000
)
def sql_query(sql: str):
    stmt = sql.strip().rstrip(";")
    if not stmt.lower().startswith("select") or _SQL_DENY.search(stmt):
        return {"error": "仅允许只读 SELECT 语句"}
    # 表名字符类必须含数字：k8s_resources / k8s_events 带数字，用 [a-z_]+ 会被
    # 截成 "k" 从而永远匹配不上白名单 —— 等于这两张最关键的配置表无法查询。
    tables = set(re.findall(r"\b(?:from|join)\s+([a-z0-9_]+)", stmt, re.I))
    illegal = {t.lower() for t in tables} - _ALLOWED_TABLES
    if illegal:
        return {"error": f"不允许访问表: {sorted(illegal)}，"
                         f"可用表: {sorted(_ALLOWED_TABLES)}"}
    if " limit " not in stmt.lower():
        stmt += " LIMIT 50"
    try:
        rows = db.fetch_all(stmt)
    except Exception as e:                    # noqa: BLE001
        # 光回一句 "no such column: avg_value" 模型只能继续猜；把涉及表的真实列名
        # 附上，它下一步就能自我纠正。错误信息的价值在于可操作性。
        msg = str(getattr(e, "orig", e))
        hint = _columns_hint(tables)
        return {"error": f"SQL 执行失败: {msg}", "table_columns": hint,
                "hint": "请按上面的真实列名重写查询"}
    out = {"row_count": len(rows), "rows": rows[:50]}
    out.update(_data_age_note(rows[:50]))
    return out


# 结果里最新一条数据旧于这个秒数时，就在返回里明说。
# 600s 的依据：live 模式采集器 10~60s 一轮，正常取数拿到的最新数据应在分钟级以内；
# 留十分钟余量，避免对低频数据源（k8s_events 等）刷屏。
_SQL_STALE_SECONDS = 600


def _data_age_note(rows: list) -> dict:
    """若结果里的数据已经不新鲜，把时间跨度一并告诉模型。

    为什么需要（量化评测实测发现）：`query_logs` 已经加了默认时间窗口，
    但 `sql_query` 是通用查询、**没有任何时间约束** —— 模型写
    `SELECT ... FROM app_logs ORDER BY ts DESC LIMIT 50` 时，若窗口内本来就没新数据，
    拿回的就是一小时前的记录，而它会当成"当前状况"写进结论。
    实测抓到两次：缓存雪崩场景里两种调度模式下都出现了
    "sql_query 返回的数据最新只到 56 / 69 分钟前，而结论在描述当前状态"。

    不强制加时间窗口：查历史、做趋势对比都是正当用法。但必须让模型
    **知道自己手里是旧数据** —— 与 query_logs 把窗口值一并回传是同一思路。
    """
    ts_vals = []
    for r in rows:
        v = r.get("ts") if isinstance(r, dict) else None
        if isinstance(v, (int, float)) and v > 0:
            # 表间 ts 单位不统一（metrics 毫秒、日志类秒），按量级归一
            ts_vals.append(v / 1000 if v > 1e11 else v)
    if not ts_vals:
        return {}
    now = time.time()
    age = now - max(ts_vals)
    if age <= _SQL_STALE_SECONDS:
        return {}
    return {"data_age_note": f"⚠️ 本结果里最新的一条数据已是 {int(age // 60)} 分钟前的，"
                             f"最旧 {int((now - min(ts_vals)) // 60)} 分钟前。"
                             f"若你要判断的是\"当前\"状况，请在 SQL 里加时间条件（如 ts >= ?）"
                             f"重查，或在结论里明确标注这是历史数据"}


def _columns_hint(tables: set) -> dict:
    """取出涉及表的真实列名，供模型纠正 SQL。

    从 SQLAlchemy metadata 读而不是 SELECT 一行推断 —— 表为空时恰恰是最需要
    列名提示的时候（新库、刚部署、采集还没跑），靠数据行推断那时正好失效。
    """
    hint = {}
    for t in sorted({x.lower() for x in tables} & _ALLOWED_TABLES):
        table = db.metadata.tables.get(t)
        if table is not None:
            hint[t] = [c.name for c in table.columns]
    return hint


@tool(
    "read_tool_result",
    "分页读取此前因超长而落盘的工具结果。先看预览里的字段结构与总数，"
    "确认需要哪一段再用 offset 定位。仅当 sql_query 无法满足（如需要原始日志文本）时使用。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "预览 <persisted-output> 中给出的落盘路径"},
            "offset": {"type": "integer", "description": "起始行号，默认 0"},
            "limit": {"type": "integer", "description": "读取行数，默认 200，上限 500"},
        },
        "required": ["path"],
    },
    is_read_only=True, concurrency_safe=True, max_result_chars=6000
)
def read_tool_result(path: str, offset: int = 0, limit: int = 200):
    from ..harness import tool_results
    try:
        # 只允许读落盘目录内的文件 —— path 来自模型输出，必须当作不可信输入
        p = tool_results.resolve_in_store(path)
    except ValueError:
        return {"error": "路径不在允许范围内，只能读取 <persisted-output> 给出的落盘路径"}
    if not p.is_file():
        return {"error": f"文件不存在或已被清理：{path}。请改用 sql_query 查询数据库"}
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        return {"error": f"读取失败：{e}"}
    offset = max(0, int(offset or 0))
    limit = min(max(1, int(limit or 200)), 500)
    chunk = lines[offset:offset + limit]
    return {"total_lines": len(lines), "offset": offset, "returned": len(chunk),
            "has_more": offset + len(chunk) < len(lines), "lines": chunk}


@tool(
    "load_skill",
    "按需加载方法论 Skill 的正文或细则。系统提示词里只给了 Skill 目录（名称+用途），"
    "判断某篇与当前任务相关时用本工具取回全文；正文里若列出了细则文档，"
    "再用 reference 参数取对应细则。不要凭记忆猜 Skill 内容。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill 名称，取自系统提示词的 Skill 目录"},
            "reference": {"type": "string",
                          "description": "可选。细则文档名（不含 .md），取自该 Skill 正文列出的清单"},
        },
        "required": ["name"],
    },
    is_read_only=True, concurrency_safe=True, max_result_chars=12000
)
def load_skill(name: str, reference: str = None):
    from ..harness import skills as skills_mod

    available = sorted(skills_mod.discover())
    if name not in available:
        return {"error": f"未知 Skill: {name}", "available": available}

    if reference:
        body = skills_mod.load_reference(name, reference)
        if not body:
            refs = skills_mod.discover()[name].references
            return {"error": f"Skill「{name}」没有名为 {reference} 的细则",
                    "available_references": refs}
        return {"skill": name, "reference": reference, "content": body}

    meta = skills_mod.discover()[name]
    return {"skill": name, "content": skills_mod.load_body(name),
            "references": meta.references,
            "hint": (f"可用 load_skill(\"{name}\", reference=...) 取细则"
                     if meta.references else "本 Skill 无细则文档")}


@tool(
    "update_plan",
    "维护当前任务的步骤清单。任务需要三步以上时先用它列出计划，"
    "每完成一步就把该步标为 done、把下一步标为 in_progress。\n"
    "两个实际作用（不是给人看的装饰）：\n"
    "1. 用户能在界面上看到进度；\n"
    "2. 若工具调用步数用尽，系统会据此判断任务是否还没做完，"
    "从而自动开新一段接着执行 —— 没有清单就只能中断并交回用户。\n"
    "每次调用传【完整的清单】（会整份替换），不要只传变化的那一条。",
    {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "完整步骤清单，按执行顺序排列",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "步骤描述，一句话"},
                        "status": {"type": "string",
                                   "enum": ["pending", "in_progress", "done"]},
                    },
                    "required": ["title", "status"],
                },
            },
        },
        "required": ["steps"],
    },
    # 只读语义：它只改本轮运行上下文里的内存状态，对被管集群与业务库零副作用。
    # 但不可并行 —— 它写的是共享状态，进并行批会产生竞态。
    is_read_only=True, concurrency_safe=False, needs_run=True,
    max_result_chars=2000,
    audit_repr=lambda a: f"更新任务清单（{len(a.get('steps') or [])} 步）",
)
def update_plan(steps: list, _run=None):
    if _run is None:
        # 离线脚本/直接调用时没有 run，此时清单无处存放。明确报错而不是静默丢弃，
        # 否则模型会以为计划已登记、后续续跑判断却拿不到它。
        return {"error": "当前无运行上下文，无法保存任务清单"}
    plan = _run.set_plan(steps)
    if not plan:
        return {"error": "steps 为空或全部条目缺少 title"}
    # 刻意【不在这里推事件】：旁路通道（push_event）要有 event_sink 才生效，
    # 而离线脚本/子 Agent 场景常常没有，事件就静默丢了（真机验证时实测 4 次调用 0 个事件）。
    # plan_update 由 loop 在工具执行后比对快照、走主通道 yield —— 有序且无条件到达。
    left = _run.unfinished_steps()
    return {"status": "ok", "plan": plan,
            "done": len(plan) - len(left), "total": len(plan)}
