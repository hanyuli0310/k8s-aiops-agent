"""风险扫描工具：内置规则 + AI 生成规则的统一执行，findings 生命周期管理。"""
from __future__ import annotations

import json
import re
import time

from .. import db
from ..rules import builtin
from .registry import tool

_SQL_SAFE = re.compile(r"^\s*select\b", re.I)
_SQL_DENY = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant)\b", re.I)


def _run_ai_rules() -> list:
    """执行 AI 生成的 sql_threshold 规则：SQL 返回单个数值，与阈值比较。"""
    findings = []
    rules = db.fetch_all(
        "SELECT rule_id, severity, title, check_ref, threshold, compare FROM risk_rules "
        "WHERE source='ai' AND check_type='sql_threshold' AND enabled=1")
    for r in rules:
        try:
            row = db.fetch_all(r["check_ref"])
            value = list(row[0].values())[0] if row else None
            if value is None:
                continue
            value = float(value)
            hit = value > r["threshold"] if (r["compare"] or "gt") == "gt" else value < r["threshold"]
            if hit:
                findings.append({
                    "rule_id": r["rule_id"], "severity": r["severity"] or "P2",
                    "title": r["title"],
                    "resource_ref": "ai-rule",
                    "evidence": {"measured_value": value, "threshold": r["threshold"],
                                 "compare": r["compare"], "check_sql": r["check_ref"][:200]},
                    "suggestion": "AI 生成规则命中，请结合证据人工确认后治理",
                })
        except Exception as e:  # noqa: BLE001
            findings.append({
                "rule_id": r["rule_id"], "severity": "P3",
                "title": f"{r['title']}（规则执行失败）",
                "resource_ref": "ai-rule",
                "evidence": {"error": str(e)}, "suggestion": "修正该 AI 规则的 SQL 后重试",
            })
    return findings


@tool(
    "run_risk_scan",
    "执行全量风险扫描：内置 13 类规则（高可用/容量/数据库/接口质量）+ 已启用的 AI 生成规则。"
    "结果写入 risk_findings 表；已治理的风险自动转为 resolved。返回按严重度分组的风险报告。",
    writes_business_data=True, max_result_chars=8000
)
def run_risk_scan():
    db.init_db()
    builtin.seed_builtin_rules()
    current = builtin.run_builtin_checks() + _run_ai_rules()
    now = int(time.time())

    current_keys = set()
    for f in current:
        resolved = f.pop("resolved_by_governance", False)
        key = (f["rule_id"], f["resource_ref"])
        current_keys.add(key)
        status = "resolved" if resolved else "open"
        existing = db.fetch_one(
            "SELECT id FROM risk_findings WHERE rule_id=:r AND resource_ref=:ref",
            {"r": f["rule_id"], "ref": f["resource_ref"]})
        params = {
            "r": f["rule_id"], "sev": f["severity"], "t": f["title"], "ref": f["resource_ref"],
            "ev": json.dumps(f["evidence"], ensure_ascii=False), "st": status,
            "sg": f["suggestion"], "ts": now,
        }
        if existing:
            params["id"] = existing["id"]
            db.execute(
                """UPDATE risk_findings SET severity=:sev, title=:t, evidence_json=:ev,
                   status=:st, suggestion=:sg, scan_ts=:ts WHERE id=:id""", params)
        else:
            db.execute(
                """INSERT INTO risk_findings (rule_id, severity, title, resource_ref, evidence_json, status, suggestion, scan_ts)
                   VALUES (:r, :sev, :t, :ref, :ev, :st, :sg, :ts)""", params)

    # 上次 open、这次未检出 → 已修复
    for old in db.fetch_all("SELECT id, rule_id, resource_ref FROM risk_findings WHERE status='open'"):
        if (old["rule_id"], old["resource_ref"]) not in current_keys:
            db.execute("UPDATE risk_findings SET status='resolved', scan_ts=:ts WHERE id=:id",
                       {"id": old["id"], "ts": now})

    return get_risk_report()


@tool(
    "get_risk_report",
    "读取最近一次风险扫描报告（不重新扫描）。返回 open/resolved 风险列表，按严重度排序。",
    is_read_only=True, concurrency_safe=True
)
def get_risk_report():
    rows = db.fetch_all(
        """SELECT rule_id, severity, title, resource_ref, evidence_json, status, suggestion
           FROM risk_findings ORDER BY status, severity, rule_id""")
    for r in rows:
        r["evidence"] = db.json_load(r.pop("evidence_json"))
    open_f = [r for r in rows if r["status"] == "open"]
    resolved = [r for r in rows if r["status"] == "resolved"]
    out = {
        "summary": {
            "open": len(open_f), "resolved": len(resolved),
            "P1": sum(1 for r in open_f if r["severity"] == "P1"),
            "P2": sum(1 for r in open_f if r["severity"] == "P2"),
        },
        "open_findings": open_f,
        "resolved_findings": resolved,
    }
    # 标注**上次扫描时间**：这份报告是结论清单而非观测序列，它的"新鲜度"取决于
    # 上一次 run_risk_scan 是什么时候跑的。没有这个字段时，模型会把半小时前扫出的
    # 风险当成"当前风险"，而事实核对的 C 类也看不见（结论里一个时间戳都没有）。
    last = db.fetch_one("SELECT MAX(scan_ts) AS t FROM risk_findings")
    ts = (last or {}).get("t")
    if ts:
        # ⚠️ scan_ts 是**秒**级 epoch（实测确认）—— 本库时间戳单位不统一：
        #    metrics/realtime_metrics 是毫秒，risk_findings.scan_ts 与日志类表是秒。
        #    写这段时先按毫秒除了 1000，得到 1970 年，就是被这个坑绊到的。
        sec = float(ts)
        age = int(time.time() - sec)
        out["summary"]["last_scan_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(sec))
        out["summary"]["scan_age_seconds"] = age
        if age > 600:
            out["data_age_note"] = (
                f"这份风险报告来自 {age // 60} 分钟前的扫描（{out['summary']['last_scan_at']}）。"
                f"若要回答『当前有哪些风险』，建议先重新执行 run_risk_scan。")
    return out


@tool(
    "create_risk_rule",
    "创建一条 AI 生成的告警规则并入库启用。规则形式：一条只读 SQL（返回单个数值）+ 阈值 + 比较方向。"
    "下次 run_risk_scan 时自动执行。例：检测 Pod 重启次数 SQL 可对 metrics 表聚合 pod.restart_count。",
    {
        "type": "object",
        "properties": {
            "rule_id": {"type": "string", "description": "规则编号，如 AI-001"},
            "title": {"type": "string", "description": "规则标题"},
            "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
            "check_sql": {"type": "string", "description": "SELECT 语句，必须返回单行单列数值"},
            "threshold": {"type": "number"},
            "compare": {"type": "string", "enum": ["gt", "lt"], "description": "gt=实测大于阈值告警"},
        },
        "required": ["rule_id", "title", "severity", "check_sql", "threshold"],
    },
    check_permissions=lambda a: "ask",
    audit_repr=lambda a: f"新建风险规则 {a.get('rule_id')}: {a.get('title')}",
)
def create_risk_rule(rule_id: str, title: str, severity: str, check_sql: str,
                     threshold: float, compare: str = "gt"):
    if not _SQL_SAFE.match(check_sql) or _SQL_DENY.search(check_sql):
        return {"error": "check_sql 必须是只读 SELECT 语句"}
    # 先试跑，保证规则可执行
    try:
        rows = db.fetch_all(check_sql)
        probe = list(rows[0].values())[0] if rows else None
    except Exception as e:  # noqa: BLE001
        return {"error": f"SQL 试跑失败: {e}"}
    if db.fetch_one("SELECT id FROM risk_rules WHERE rule_id=:r", {"r": rule_id}):
        db.execute("""UPDATE risk_rules SET title=:t, severity=:s, check_ref=:c, threshold=:th, compare=:cp, enabled=1
                      WHERE rule_id=:r""",
                   {"r": rule_id, "t": title, "s": severity, "c": check_sql, "th": threshold, "cp": compare})
    else:
        db.execute(
            """INSERT INTO risk_rules (rule_id, source, severity, title, check_type, check_ref, threshold, compare, enabled)
               VALUES (:r, 'ai', :s, :t, 'sql_threshold', :c, :th, :cp, 1)""",
            {"r": rule_id, "s": severity, "t": title, "c": check_sql, "th": threshold, "cp": compare})
    return {"status": "created", "rule_id": rule_id, "probe_value": probe,
            "threshold": threshold, "compare": compare,
            "would_alert_now": (probe is not None and
                                (float(probe) > threshold if compare == "gt" else float(probe) < threshold))}


@tool(
    "list_risk_rules",
    "列出全部告警规则（内置 + AI 生成），含启用状态。",
    is_read_only=True, concurrency_safe=True
)
def list_risk_rules():
    rows = db.fetch_all(
        "SELECT rule_id, source, severity, title, check_type, threshold, compare, enabled FROM risk_rules ORDER BY source, rule_id")
    return {"rules": rows}
