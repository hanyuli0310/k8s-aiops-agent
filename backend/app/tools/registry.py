"""Tool Registry：工具以 JSON Schema 注册，供 LLM function calling 与直接调用。

每个工具除了 schema 之外还声明【安全属性】，供权限门禁（harness/permissions.py）
与审计（harness/audit.py）使用。

安全属性遵循 fail-closed 原则：不声明就按最危险处理。写新工具时若忘了标注，
系统会假设它「会写、不可并行、需要用户确认」——宁可多问一次，绝不默认放行。

三级写入语义：
  is_read_only          纯查询，不写任何表          → 任何模式下放行
  writes_business_data  只写本平台业务表，不碰集群    → readonly 模式仍放行
  is_destructive        治理动作/不可逆操作          → readonly 拒绝，confirm 需确认
"""
from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    """一个工具的完整定义：执行体 + schema + 安全属性 + 钩子。"""

    func: Callable
    schema: dict

    # ── 安全属性（fail-closed 默认值）──
    is_read_only: bool = False           # 默认假设会写
    writes_business_data: bool = False   # 只写本平台业务表，对被管集群无副作用
    is_destructive: bool = False         # 不可逆操作（改集群 / 删表 / 花钱）
    concurrency_safe: bool = False       # 默认不能并行

    # 单个结果的字符预算，超出后走落盘 + 预览（P0-4）
    max_result_chars: int = 2000
    # 需要拿到当前 RunContext（如 dispatch_agent 要派生子 run 并共享中断/事件通道）。
    # 置 True 时 execute() 会把 run 作为 _run 关键字注入。
    needs_run: bool = False

    # ── 可选钩子 ──
    # 返回 'allow' | 'ask' | 'deny'；未提供则由 permissions.decide 用默认策略
    check_permissions: Optional[Callable[[dict], str]] = None
    # 参数/状态校验：返回错误字符串表示不通过，该信息会【发回给模型】自我纠正
    validate_input: Optional[Callable[[dict], Optional[str]]] = None
    # 审计投影：这次调用在审计日志/确认卡片上展示成什么（人类可读一行）
    audit_repr: Optional[Callable[[dict], str]] = None

    def summarize(self, args: dict) -> str:
        """生成人类可读摘要，用于确认卡片与审计日志。失败时回落到工具名。"""
        if self.audit_repr:
            try:
                return str(self.audit_repr(args))
            except Exception:                # noqa: BLE001
                logger.debug("audit_repr 失败", exc_info=True)
        return self.schema["function"]["name"]


_REGISTRY: Dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict = None, *,
         is_read_only: bool = False,
         writes_business_data: bool = False,
         is_destructive: bool = False,
         concurrency_safe: bool = False,
         max_result_chars: int = 2000,
         needs_run: bool = False,
         check_permissions: Callable = None,
         validate_input: Callable = None,
         audit_repr: Callable = None):
    """装饰器：注册一个工具。parameters 为 JSON Schema 的 properties/required。

    安全属性见模块 docstring。破坏性工具务必显式声明 is_destructive=True。
    """
    def deco(func: Callable):
        _REGISTRY[name] = ToolSpec(
            func=func,
            schema={
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters or {"type": "object", "properties": {}},
                },
            },
            is_read_only=is_read_only,
            writes_business_data=writes_business_data,
            is_destructive=is_destructive,
            concurrency_safe=concurrency_safe,
            max_result_chars=max_result_chars,
            needs_run=needs_run,
            check_permissions=check_permissions,
            validate_input=validate_input,
            audit_repr=audit_repr,
        )
        return func
    return deco


def get_spec(name: str) -> Optional[ToolSpec]:
    return _REGISTRY.get(name)


def get_schemas(names: List[str] = None) -> List[dict]:
    if names is None:
        names = list(_REGISTRY)
    return [_REGISTRY[n].schema for n in names if n in _REGISTRY]


def list_tools() -> List[str]:
    return list(_REGISTRY)


def describe_tools() -> List[dict]:
    """列出全部工具及其安全属性（供 /api/status 与人工核查用）。"""
    out = []
    for name, spec in _REGISTRY.items():
        out.append({
            "name": name,
            "read_only": spec.is_read_only,
            "writes_business_data": spec.writes_business_data,
            "destructive": spec.is_destructive,
            "concurrency_safe": spec.concurrency_safe,
            "max_result_chars": spec.max_result_chars,
            "needs_run": spec.needs_run,
        })
    return out


def execute(name: str, args: dict, run=None) -> str:
    """执行工具，永远返回字符串（JSON），异常兜底不打断 agent loop。

    ⚠️ 本函数【不做】权限检查 —— 门禁在 harness/loop.py 的工具执行段。
    直接调用本函数的路径（/api/ingest、governance_tools 的 execute_plan）
    都是用户在 UI 上的显式操作，本身即为授权，无需再次确认。
    """
    spec = _REGISTRY.get(name)
    if spec is None:
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
    call_args = dict(args or {})
    if spec.needs_run:
        # 只有显式声明 needs_run 的工具才拿到 run，其余工具签名不受影响
        call_args["_run"] = run
    try:
        result = spec.func(**call_args)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:  # noqa: BLE001
        logger.error("tool %s failed: %s\n%s", name, e, traceback.format_exc())
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


def ensure_loaded():
    """导入全部工具模块以完成注册（幂等）。

    注意 governance_tools 刻意不在此列 —— 它不注册为 LLM 工具，只供
    /api/governance/* 直接调用（见 CLAUDE.md §3.3）。
    """
    from . import (agent_tools, data_tools, remediation_tools,       # noqa: F401
                   risk_tools, topology_tools)                        # noqa: F401
