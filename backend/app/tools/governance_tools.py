"""U10 治理方案与 check 列表流程。

流程：plan（LLM 生成方案 md + 结构化 checklist，降级用规则模板）
     → 前端勾选确认 → execute（逐项执行已注册治理工具 + Action 转发）→ 状态流转。

checklist 项映射到已注册治理工具（registry.execute），LLM 只负责组织与解释，
执行永远走确定性代码路径——LLM 幻觉不会产生未定义的动作。
"""
from __future__ import annotations

import json
import logging
import time

from .. import db
from ..harness import llm
from . import registry

logger = logging.getLogger(__name__)

# finding 规则 → 治理动作模板（降级路径 + LLM 结果的合法性白名单）
RULE_ACTION_TEMPLATES = {
    "HA-001": lambda f: {"tool": "patch_deployment",
                         "params": {"name": _svc(f), "action": "remove_zone_affinity"},
                         "desc": f"移除 {_svc(f)} 的单可用区亲和，副本按可用区打散", "risk": "中"},
    "HA-002": lambda f: {"tool": "patch_deployment",
                         "params": {"name": _svc(f), "action": "set_replicas", "value": "2"},
                         "desc": f"将 {_svc(f)} 扩容至 2 副本", "risk": "低"},
    "HA-003": lambda f: {"tool": "patch_deployment",
                         "params": {"name": _svc(f), "action": "add_probes"},
                         "desc": f"为 {_svc(f)} 补全存活/就绪探针", "risk": "低"},
    "HA-004": lambda f: {"tool": "create_pdb",
                         "params": {"app": _svc(f), "min_available": 1},
                         "desc": f"为 {_svc(f)} 创建 PodDisruptionBudget", "risk": "低"},
    "CAP-001": lambda f: {"tool": "patch_deployment",
                          "params": {"name": _svc(f), "action": "set_cpu_request", "value": "500m"},
                          "desc": f"为 {_svc(f)} 补充 CPU request=500m", "risk": "低"},
    "CAP-002": lambda f: {"tool": "patch_deployment",
                          "params": {"name": _svc(f), "action": "set_memory_limit", "value": "2048Mi"},
                          "desc": f"为 {_svc(f)} 补充内存 limit=2048Mi", "risk": "低"},
    "CAP-003": lambda f: {"tool": "patch_deployment",
                          "params": {"name": "api-gateway", "action": "set_cpu_limit", "value": "2000m"},
                          "desc": "下调超卖大头 api-gateway 的 CPU limit（4000m→2000m，双副本共降 4 核）压回超卖率；"
                                  "注意扩副本类动作会反向推高超卖，建议最后执行本项并复查", "risk": "中"},
    "CAP-004": lambda f: {"tool": "upgrade_rds_instance",
                          "params": {"instance_id": f["resource_ref"], "target_spec": "内存翻倍升配"},
                          "desc": f"升配 {f['resource_ref']} 实例规格", "risk": "中"},
    "DB-001": lambda f: {"tool": "upgrade_rds_instance",
                         "params": {"instance_id": f["resource_ref"], "target_spec": "max_connections 翻倍"},
                         "desc": f"上调 {f['resource_ref']} 最大连接数", "risk": "低"},
    "DB-002": lambda f: {"tool": "create_db_index",
                         "params": {"table": "orders", "columns": ["status", "created_at"]},
                         "desc": "为 orders 表创建复合索引 (status, created_at)，消除全表扫描", "risk": "低"},
    "API-001": lambda f: {"tool": "create_db_index",
                          "params": {"table": "orders", "columns": ["status", "created_at"]},
                          "desc": f"治理 {f['resource_ref']} 的根因（DB 慢查询），建复合索引", "risk": "低"},
}


def _svc(finding: dict) -> str:
    """从 resource_ref（如 default/payment-service）提取服务名。"""
    return finding["resource_ref"].split("/")[-1]


def _template_checklist(findings: list) -> list:
    """降级路径：按规则模板生成 checklist（LLM 不可用时保底）。"""
    items, seen = [], set()
    for i, f in enumerate(findings):
        tpl = RULE_ACTION_TEMPLATES.get(f["rule_id"])
        if not tpl:
            continue
        item = tpl(f)
        key = (item["tool"], json.dumps(item["params"], sort_keys=True))
        if key in seen:      # API-001 与 DB-002 同根因去重
            continue
        seen.add(key)
        items.append({
            "item_id": f"c{i + 1}", "finding_rule": f["rule_id"],
            "finding_ref": f["resource_ref"], "action_type": item["tool"],
            "tool": item["tool"], "target": f["resource_ref"], "params": item["params"],
            "desc": item["desc"], "risk": item["risk"], "default_checked": item["risk"] == "低",
        })
    return items


def _template_solution_md(findings: list, checklist: list) -> str:
    lines = ["## 治理方案（规则模板生成）", "",
             f"共 {len(findings)} 条未治理风险，生成 {len(checklist)} 项治理动作：", ""]
    for f in findings:
        lines.append(f"- **[{f['severity']}] {f['rule_id']}** {f['title']}")
        lines.append(f"  - 建议：{f['suggestion']}")
    lines += ["", "> 勾选下方 check 列表确认后执行；高/中风险项默认不勾选，请人工确认。"]
    return "\n".join(lines)


def _llm_solution(findings: list, checklist: list) -> str:
    """LLM 生成解决方案叙述（checklist 结构不变，只增强可读性与因果解释）。"""
    prompt = (
        "你是 SRE 专家。基于以下风险 findings 与预生成的治理动作清单，写一份简洁的治理方案 Markdown：\n"
        "1) 按因果关系组织（若多个 finding 同根因要点明）；2) 每个动作解释为什么能解决对应风险；\n"
        "3) 给出建议执行顺序与风险提示；4) 400 字以内，不要编造清单外的动作。\n\n"
        f"findings: {json.dumps(findings, ensure_ascii=False)[:3000]}\n\n"
        f"checklist: {json.dumps(checklist, ensure_ascii=False)[:2000]}")
    try:
        return llm.chat_text([{"role": "user", "content": prompt}], model=None)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 方案生成失败，降级模板: %s", e)
        return _template_solution_md(findings, checklist)


def create_plan(finding_ids: list = None) -> dict:
    """生成治理方案：finding_ids 为空 = 全部 open。"""
    where = "status='open'"
    params = {}
    if finding_ids:
        placeholders = ",".join(f":id{i}" for i in range(len(finding_ids)))
        where += f" AND id IN ({placeholders})"
        params = {f"id{i}": v for i, v in enumerate(finding_ids)}
    findings = db.fetch_all(
        f"SELECT id, rule_id, severity, title, resource_ref, suggestion FROM risk_findings WHERE {where}",
        params)
    if not findings:
        return {"error": "没有待治理的 open finding，请先执行风险扫描"}

    checklist = _template_checklist(findings)
    solution_md = _llm_solution(findings, checklist) if llm.available() \
        else _template_solution_md(findings, checklist)

    db.execute(
        """INSERT INTO governance_plans (finding_ids_json, solution_md, checklist_json, status, created_at)
           VALUES (:f, :s, :c, 'draft', :t)""",
        {"f": json.dumps([f["id"] for f in findings]),
         "s": solution_md, "c": json.dumps(checklist, ensure_ascii=False),
         "t": int(time.time() * 1000)})
    plan_id = db.fetch_one("SELECT MAX(id) AS id FROM governance_plans")["id"]
    return {"plan_id": plan_id, "solution_md": solution_md, "checklist": checklist,
            "finding_count": len(findings)}


def list_plans(limit: int = 10) -> list:
    rows = db.fetch_all(
        "SELECT id, status, created_at, executed_at, checklist_json, result_json "
        "FROM governance_plans ORDER BY id DESC LIMIT :l", {"l": limit})
    for r in rows:
        r["checklist"] = db.json_load(r.pop("checklist_json")) or []
        r["result"] = db.json_load(r.pop("result_json"))
    return rows


def execute_plan(plan_id: int, item_ids: list) -> dict:
    """执行勾选项：单项失败不中断后续；状态机 draft→executing→completed/failed。"""
    plan = db.fetch_one("SELECT checklist_json, status FROM governance_plans WHERE id=:id",
                        {"id": plan_id})
    if not plan:
        return {"error": f"plan {plan_id} 不存在"}
    checklist = db.json_load(plan["checklist_json"]) or []
    db.execute("UPDATE governance_plans SET status='executing' WHERE id=:id", {"id": plan_id})

    results = []
    for item in checklist:
        if item["item_id"] not in item_ids:
            continue
        # 白名单校验：只允许已注册治理工具（防 LLM 幻觉动作）
        if item["tool"] not in ("patch_deployment", "create_pdb", "create_db_index",
                                "upgrade_rds_instance"):
            results.append({"item_id": item["item_id"], "status": "failed",
                            "message": f"未注册的治理工具: {item['tool']}"})
            continue
        try:
            out = json.loads(registry.execute(item["tool"], item["params"]))
            ok = "error" not in out
            results.append({"item_id": item["item_id"], "status": "success" if ok else "failed",
                            "message": out.get("change") or out.get("effect") or out.get("note")
                            or out.get("error", ""),
                            "mock_feedback": out.get("mock_feedback")})
        except Exception as e:  # noqa: BLE001
            results.append({"item_id": item["item_id"], "status": "failed", "message": str(e)})

    plan_status = "completed" if all(r["status"] == "success" for r in results) and results \
        else "failed"
    db.execute(
        "UPDATE governance_plans SET status=:s, executed_at=:t, result_json=:r WHERE id=:id",
        {"s": plan_status, "t": int(time.time() * 1000),
         "r": json.dumps(results, ensure_ascii=False), "id": plan_id})
    return {"plan_id": plan_id, "results": results, "plan_status": plan_status}
