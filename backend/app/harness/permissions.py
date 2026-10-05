"""工具权限决策：四层门禁 + 三态运行模式。

对标 Claude Code 的七层纵深防御（原理教程第 3 课），运维场景取其中四层：

    ① 未知工具        → deny
    ② 只读工具        → allow（任何模式）
    ③ 运行模式        → readonly 拒绝治理动作与配置变更
    ④ 工具自身逻辑    → ToolSpec.check_permissions，默认 ask（fail-closed）

三态运行模式：
    readonly  只读巡检。允许「只读」与「只写本平台业务表」的工具，
              只拦治理动作与配置变更 —— 这样只读模式下仍能跑扫描/拓扑，
              保住「巡检」这个动作本身的完整性。
    confirm   默认。破坏性动作需用户确认。
    auto      无人值守。非破坏性写操作自动放行，破坏性仍需人工。

每个决策都带 reason_type，写入审计日志后可回答「这条变更是谁批准的」。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..tools import registry

# 决策来源，会落进 agent_audit.reason_type
REASON_RULE = "rule"                  # 工具固有属性（未知工具 / 只读）
REASON_MODE = "mode"                  # 运行模式决定
REASON_TOOL = "tool"                  # 工具自身的 check_permissions
REASON_USER = "user"                  # 用户在会话内已批准
REASON_AUTO_APPROVE = "auto_approve"  # 【历史值】曾用的逃生阀自动批准。
# 逃生阀已随前端确认卡片上线而移除，新记录不会再产生此值；
# 保留常量是为了让旧审计记录仍能被正确解读（前端也保留对应中文映射）。


@dataclass
class Decision:
    behavior: str                     # allow | ask | deny
    reason_type: str
    message: str = ""


def approval_key(tool_name: str, args: dict) -> str:
    """会话内批准的粒度：工具 + 主要目标资源。

    用户勾选「本次会话都允许」时按此 key 记住，避免同一资源反复弹窗，
    但换了资源仍会重新询问（不会一次授权放开全部）。
    """
    args = args or {}
    target = (args.get("name") or args.get("app") or args.get("instance_id")
              or args.get("table") or args.get("rule_id") or "*")
    return f"{tool_name}:{target}"


def decide(tool_name: str, args: dict, mode: str = "confirm",
           session_approvals=frozenset()) -> Decision:
    """判定一次工具调用的处置方式。纯函数，不产生副作用。"""
    spec = registry.get_spec(tool_name)

    # ── 第 1 层：未知工具 ──
    if spec is None:
        return Decision("deny", REASON_RULE, f"未知工具 {tool_name}")

    # ── 第 2 层：只读工具永远放行 ──
    if spec.is_read_only:
        return Decision("allow", REASON_RULE)

    # ── 第 3 层：运行模式 ──
    if mode == "readonly":
        if spec.writes_business_data and not spec.is_destructive:
            return Decision("allow", REASON_MODE, "只读巡检模式允许写入本平台业务表")
        return Decision(
            "deny", REASON_MODE,
            "当前为只读巡检模式，禁止执行治理动作与配置变更。"
            "如需治理请在界面切换到「确认执行」模式后重试。")

    # ── 第 4 层：工具自身逻辑（默认 ask —— fail-closed）──
    behavior = "ask"
    if spec.check_permissions:
        try:
            behavior = spec.check_permissions(args or {})
        except Exception:                     # noqa: BLE001
            behavior = "ask"                  # 判定函数自己抛异常 → 按需确认处理
    if behavior not in ("allow", "ask", "deny"):
        behavior = "ask"

    if behavior == "deny":
        return Decision("deny", REASON_TOOL, "该工具在当前环境下不可执行")

    if behavior == "allow":
        return Decision("allow", REASON_TOOL)

    # behavior == "ask"：先看会话内是否已批准过同类动作
    if approval_key(tool_name, args) in session_approvals:
        return Decision("allow", REASON_USER, "会话内已批准过该动作")

    # auto 模式用【白名单】而非黑名单：只自动放行明确标记为「只写本平台业务表」
    # 的工具，其余一律仍需人工确认。
    #
    # 为什么不是「只要不是 is_destructive 就放行」：
    #   1. 配置变更类工具（如 create_risk_rule）虽非破坏性，但会改变后续所有
    #      扫描的行为，影响面远大于一次性数据写入 —— 无人值守时不该自动放行；
    #   2. 更关键的是对未来新增工具 fail-closed：黑名单逻辑下，只要有人新加
    #      工具时忘标 is_destructive，auto 模式就会自动放行它。白名单则相反，
    #      忘标的工具默认需要确认。
    #   （对应原理教程第 5 课：异步/无监督场景用工具白名单，有监督才用黑名单。）
    if mode == "auto" and spec.writes_business_data and not spec.is_destructive:
        return Decision("allow", REASON_MODE, "auto 模式放行本平台业务表写入")

    return Decision("ask", REASON_TOOL)
