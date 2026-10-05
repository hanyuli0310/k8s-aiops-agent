"""意图识别：LLM 结构化路由，无 LLM 时降级关键词规则（保证离线可演示）。"""
from __future__ import annotations

import json
import logging
import re

from . import llm

logger = logging.getLogger(__name__)

INTENTS = ["data_ingest", "data_query", "topology", "risk_scan", "rule_create",
           "fault_diagnose", "remediation", "full_checkup", "chat"]

_INTENT_PROMPT = """你是运维平台的意图路由器。将用户输入分类为以下意图之一，并抽取实体。

意图定义：
- data_ingest: 采集/同步/导入监控数据（CMS指标、SLS日志、K8s快照）
- data_query: 查询指标/日志/trace/资源等数据（如"MySQL连接使用率多少"）
- topology: 梳理/查看服务拓扑、调用链路关系
- risk_scan: 执行风险扫描/巡检/查看风险报告
- rule_create: 新建/生成告警规则
- fault_diagnose: 定位故障/慢/报错的根因（如"下单接口很慢"）
- remediation: 执行治理/修复动作（扩副本、建索引、补探针等）
- full_checkup: 全面体检（采集+拓扑+扫描一条龙）
- chat: 闲聊或与运维无关

只输出 JSON：{"intent": "...", "entities": {"service": "...", "api": "...", "resource": "..."}}
entities 没有就留空对象。"""

_KEYWORD_RULES = [
    ("full_checkup", r"全面体检|一条龙|全流程|完整巡检"),
    ("data_ingest", r"采集|同步数据|导入数据|拉取数据|入库"),
    ("topology", r"拓扑|链路梳理|调用关系|依赖关系"),
    ("rule_create", r"生成.*规则|新建.*规则|创建.*规则|加.*告警"),
    ("risk_scan", r"风险|扫描|巡检|隐患|体检"),
    ("remediation", r"治理|修复|解决|扩副本|扩容|建索引|补探针|升配"),
    ("fault_diagnose", r"故障|很慢|超时|报错|错误率|定位|根因|排查|异常|挂了|不可用"),
    ("data_query", r"查|多少|看一下|统计|指标|日志|trace|资源"),
]


def classify_keyword(text: str) -> dict:
    """纯关键词分类（**零 LLM 调用**）。

    AGENT_ROUTING=model 下，意图不再用于选 Agent，只用于两件事：
      · 前端时间线上展示本轮大致在做什么；
      · 判定这类结论要不要写长期记忆（_maybe_remember）。
    既然不再担负路由，就没必要为它花一次快模型往返（实测每次约 1~2 秒）。
    分错了的代价也从"路由到错的 Agent"降为"前端标签不准"。
    """
    for intent, pattern in _KEYWORD_RULES:
        if re.search(pattern, text):
            return {"intent": intent, "entities": {}, "router": "keyword"}
    return {"intent": "chat", "entities": {}, "router": "keyword"}


def classify(text: str) -> dict:
    """返回 {"intent": str, "entities": dict, "router": str}。

    结构化输出（P2-6）：用 response_format={"type":"json_object"} 让模型只输出
    合法 JSON，不再靠正则从自由文本里抠 `{...}`。

    仍保留正则兜底：json_mode 只保证"是合法 JSON"，不保证 schema 正确
    （模型仍可能包一层 {"result": {...}} 或多输出字段）。三层解析全失败
    才降级到关键词规则 —— 意图识别绝不能因为模型抽风就整个哑掉。
    """
    if llm.available():
        try:
            out = llm.chat_text([
                {"role": "system", "content": _INTENT_PROMPT},
                {"role": "user", "content": text},
            ], temperature=0.0, json_mode=True)
            data = _parse_intent(out)
            if data:
                return {**data, "router": "llm"}
            logger.warning("意图 JSON 无法解析或 intent 非法，降级关键词: %r", out[:200])
        except Exception as e:  # noqa: BLE001
            logger.warning("llm intent failed, fallback keyword: %s", e)
    for intent, pattern in _KEYWORD_RULES:
        if re.search(pattern, text):
            return {"intent": intent, "entities": {}, "router": "keyword"}
    return {"intent": "chat", "entities": {}, "router": "keyword"}


def _parse_intent(out: str):
    """从模型输出里取出 {"intent","entities"}，取不到返回 None。"""
    if not out:
        return None
    obj = None
    try:
        obj = json.loads(out)
    except (TypeError, ValueError):
        # json_mode 理论上保证合法 JSON，但换了模型/端点就未必 —— 保留正则兜底
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            try:
                obj = json.loads(m.group())
            except ValueError:
                return None
    if not isinstance(obj, dict):
        return None

    # 模型有时会多包一层（{"result": {...}} / {"data": {...}}）
    if "intent" not in obj:
        for k in ("result", "data", "output"):
            if isinstance(obj.get(k), dict) and "intent" in obj[k]:
                obj = obj[k]
                break

    intent = obj.get("intent")
    if intent not in INTENTS:
        return None
    entities = obj.get("entities")
    if not isinstance(entities, dict):
        entities = {}
    # 实体值统一成字符串并丢掉空值，避免 None/嵌套结构流到下游拼提示词
    entities = {str(k): str(v) for k, v in entities.items()
                if v not in (None, "", [], {})}
    return {"intent": intent, "entities": entities}
