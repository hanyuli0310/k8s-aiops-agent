"""Memory：短期会话记忆（chat_messages）+ 长期记忆（agent_memory，跨会话结论/治理记录）。"""
from __future__ import annotations

import json
import logging
import time

from .. import db

logger = logging.getLogger(__name__)

# 各 scope 在一次 recall 中的配额。避免单一 scope（尤其 governance —— 每次治理
# 都会写一条）挤占全部名额，把诊断结论完全排出上下文。
SCOPE_QUOTA = {"conclusion": 3, "governance": 3, "preference": 2}

# 每个 scope 在库里保留的最大条数，超出的旧记录由 prune() 清理。
KEEP_PER_SCOPE = 20

# ⚠️ 不可 prune 的 scope。
# governance 条目在 static 模式下是【承载状态的标记】，不只是给 LLM 看的记忆：
# rules/builtin.py 的 _has_governance_prefix() 靠它判定 finding 是否
# resolved_by_governance。删掉标记 = 已治理的风险翻回 open，直接破坏
# 「全量治理后 0 open / 11 resolved」的验收基准。
#
# 拥挤问题（governance 挤掉 conclusion）只发生在【注入提示词】这一层，
# 已由 recall() 的 SCOPE_QUOTA 解决，不需要也不应该动持久化层。
# 且 governance 的 key 是资源级的（patch:{name}:{action} / pdb:{app} /
# db_index:{table} / rds_upgrade:{id}）且走 upsert，行数本身天然有界。
NO_PRUNE_SCOPES = frozenset({"governance"})


# --- 短期：会话历史 ---

def save_chat(session_id: str, role: str, content: str, tool_calls: list = None):
    db.execute(
        """INSERT INTO chat_messages (session_id, role, content, tool_calls_json, created_at)
           VALUES (:s, :r, :c, :t, :ts)""",
        {"s": session_id, "r": role, "c": content,
         "t": json.dumps(tool_calls or [], ensure_ascii=False), "ts": int(time.time())})


def recent_chat(session_id: str, limit: int = 10) -> list:
    """取最近若干条会话消息，供下一轮作为历史注入。

    ★ 绝不能把 tool_calls 结构化还原回 assistant 消息：OpenAI 兼容接口要求
      带 tool_calls 的 assistant 后面必须紧跟对应 tool_call_id 的 role="tool"
      消息，而历史表里没有存工具结果 —— 还原结构就会制造孤立的 tool_call，
      下一轮请求直接 400（与 Bug 1 同类）。

      改为把"上轮调用过哪些工具、参数是什么"摘要成一行文本附在 content 后面。
      模型同样能利用（"刚才那个 trace 再看看"能知道 trace_id），
      而配对不变量丝毫不受影响。
    """
    rows = db.fetch_all(
        """SELECT role, content, tool_calls_json FROM chat_messages WHERE session_id=:s
           ORDER BY id DESC LIMIT :l""", {"s": session_id, "l": limit})
    out = []
    for r in reversed(rows):
        msg = {"role": r["role"], "content": r["content"] or ""}
        if r["role"] == "assistant":
            hint = _tool_hint(r.get("tool_calls_json"))
            if hint:
                msg["content"] = (msg["content"] + "\n" + hint).strip()
        out.append(msg)
    return out


# 摘要里保留的工具调用条数与单条参数长度上限 —— 历史提示不能反过来挤爆上下文
_HINT_MAX_CALLS = 6
_HINT_MAX_ARG_CHARS = 80


def _tool_hint(tool_calls_json) -> str:
    """把上轮的工具调用压成一行可读摘要。解析失败一律返回空串（历史提示不值得让整轮挂掉）。"""
    if not tool_calls_json:
        return ""
    try:
        calls = (tool_calls_json if isinstance(tool_calls_json, list)
                 else json.loads(tool_calls_json))
    except (TypeError, ValueError):
        return ""
    if not calls:
        return ""

    parts = []
    for c in calls[:_HINT_MAX_CALLS]:
        if not isinstance(c, dict):
            continue
        name = c.get("tool") or c.get("name") or "?"
        args = c.get("args")
        if isinstance(args, dict) and args:
            inner = ", ".join(f"{k}={v}" for k, v in args.items())
            if len(inner) > _HINT_MAX_ARG_CHARS:
                inner = inner[:_HINT_MAX_ARG_CHARS] + "…"
            parts.append(f"{name}({inner})")
        else:
            parts.append(f"{name}()")
    if not parts:
        return ""
    more = f" 等 {len(calls)} 次" if len(calls) > _HINT_MAX_CALLS else ""
    return f"[上一轮我调用过：{'; '.join(parts)}{more}]"



# --- 长期：跨会话结论 ---

def remember(scope: str, key: str, content: str):
    """写长期记忆（按 scope+key upsert）。

    scope: conclusion(诊断结论) / governance(治理记录) / preference(用户偏好)。
    写入后顺带 prune，保证单 scope 不会无限增长。
    """
    existing = db.fetch_one(
        "SELECT id FROM agent_memory WHERE scope=:s AND mem_key=:k", {"s": scope, "k": key})
    if existing:
        db.execute("UPDATE agent_memory SET content=:c, created_at=:t WHERE id=:id",
                   {"c": content, "t": int(time.time()), "id": existing["id"]})
    else:
        db.execute(
            "INSERT INTO agent_memory (scope, mem_key, content, created_at) VALUES (:s, :k, :c, :t)",
            {"s": scope, "k": key, "c": content, "t": int(time.time())})
    prune(scope)


def prune(scope: str = None, keep: int = KEEP_PER_SCOPE):
    """清理：每个 scope 只保留最近 keep 条。scope 为 None 时清理全部可清理 scope。

    NO_PRUNE_SCOPES 中的 scope 被跳过 —— 它们的条目承载业务状态而非仅是记忆，
    详见模块顶部注释。
    """
    scopes = [scope] if scope else list(SCOPE_QUOTA)
    for s in scopes:
        if s in NO_PRUNE_SCOPES:
            continue
        try:
            rows = db.fetch_all(
                "SELECT id FROM agent_memory WHERE scope=:s ORDER BY created_at DESC",
                {"s": s})
            stale = [r["id"] for r in rows[keep:]]
            for mid in stale:
                db.execute("DELETE FROM agent_memory WHERE id=:id", {"id": mid})
            if stale:
                logger.info("prune scope=%s 清理 %d 条陈旧记忆", s, len(stale))
        except Exception as e:  # noqa: BLE001
            logger.warning("prune scope=%s 失败（已忽略）: %s", s, e)


def recall(query: str = None, limit: int = 8) -> list:
    """检索长期记忆：关键词命中优先，再按 scope 配额补齐。

    配额的意义：治理动作每次都会写记忆，纯按时间倒序取 N 条会让 governance
    挤满全部名额，诊断结论（最有引用价值的那类）反而被排出上下文。
    """
    picked: list = []
    seen: set = set()

    def _add(rows: list):
        for r in rows:
            key = (r["scope"], r["mem_key"])
            if key not in seen:
                seen.add(key)
                picked.append(r)

    # ① 关键词命中优先
    if query:
        terms = [t for t in query.replace("，", " ").replace(",", " ").split()
                 if len(t) >= 2][:5]
        if terms:
            clauses = " OR ".join(
                f"(content LIKE :t{i} OR mem_key LIKE :t{i})" for i in range(len(terms)))
            params = {f"t{i}": f"%{t}%" for i, t in enumerate(terms)}
            params["l"] = limit
            _add(db.fetch_all(
                f"SELECT scope, mem_key, content, created_at FROM agent_memory "
                f"WHERE {clauses} ORDER BY created_at DESC LIMIT :l", params))

    # ② 按 scope 配额补齐，保证每类记忆都有代表
    for scope, n in SCOPE_QUOTA.items():
        _add(db.fetch_all(
            "SELECT scope, mem_key, content, created_at FROM agent_memory "
            "WHERE scope=:s ORDER BY created_at DESC LIMIT :n", {"s": scope, "n": n}))

    return picked[:limit]


def memory_prompt(query: str = None) -> str:
    """把相关长期记忆渲染为注入 system prompt 的片段。"""
    rows = recall(query)
    if not rows:
        return ""
    lines = [f"- [{r['scope']}] {r['mem_key']}: {r['content'][:200]}" for r in rows]
    return "\n\n[长期记忆（历史结论与治理记录，可直接引用，但需注意时效）]\n" + "\n".join(lines)
