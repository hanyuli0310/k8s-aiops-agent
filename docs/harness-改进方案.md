# Agent Harness 改进方案

> **目标**：把 `backend/app/harness/` 从"骨架完整的 Demo Harness"升级为"有安全门禁、能自愈、上下文有韧性的生产级 Agent Harness"。
>
> **方法论来源**：对 Claude Code 源码（`CC/`）的系统性拆解，见 [`Coding-Agent实现原理教程.md`](Coding-Agent实现原理教程.md)。本方案的每一项改动都标注了对应的原理课次。
>
> **状态**：待评审。评审通过后按第 6 节顺序实施。
>
> **约束**：
> - 改动集中在 `backend/app/harness/`、`backend/app/tools/registry.py`、`backend/app/agents/base.py`，这三处是全部代码的公共依赖，需在个人分支（`Dev/*`）+ 独立 `DB_NAME` 上开发（见 CLAUDE.md §10）。
> - 每一步完成后按 CLAUDE.md §9 的验收基准自查（static 11 条风险不多不少 / live 稳态 7 条）。
> - 不引入 LangChain 等框架，保持自研 Harness 的项目定位。

---

## 1. 结论摘要

现状**不是"Agent 实现比较简单"，而是"骨架方向正确但缺三层护栏"**：

| 维度 | 现状 | 目标 |
|---|---|---|
| 事件流架构 | ✅ generator + SSE，已同构 | 保持 |
| 工具注册 | ✅ JSON Schema + 白名单 | 加安全谓词 |
| **安全门禁** | ❌ 治理工具零门禁，仅提示词约束 | 四层决策链 + 三态模式 + 审计 |
| **失败自愈** | ❌ LLM 一次异常整轮废掉 | 重试 + 分级恢复 + 熔断 |
| **上下文韧性** | ⚠️ 有压缩但会切断配对（会崩） | 修复 + 落盘 + token 记账 |
| 可中断 | ❌ 客户端断开仍烧 token | RunContext + abort |
| 子 Agent | ⚠️ 硬编码 3 步序列 | 工具化的 `dispatch_agent` |

发现 **3 个真实 bug**，其中 1 个会导致运行时 API 400 崩溃（第 2 节）。

---

## 2. 现状评估

### 2.1 已经做对的部分（不要改）

在改进之前先明确哪些设计**已经和工业级实现同构**，避免过度重构：

| 现有实现 | 对应 Claude Code 机制 | 说明 |
|---|---|---|
| `loop.run_agent()` 是 generator，yield 事件字典 | `query()` 是 `AsyncGenerator`（第2课） | 主循环与 UI 解耦，SSE 层只消费事件流 |
| `registry.execute()` 捕获所有异常返回 `{"error":...}` | 工具错误包成 `tool_result` 而非抛出（第2课） | **关键正确决策**：保证 tool_call 与 tool_result 永远配对，规避了最常见的 400 错误 |
| `@tool` + JSON Schema 注册，`get_schemas(names)` 按白名单取子集 | `Tool.inputSchema` + `filterToolsForAgent`（第1/5课） | 结构正确，缺的是安全谓词 |
| `AGENT_SPECS` 每 Agent 独立工具白名单 | Agent 定义的 `tools[]`（第5课） | 已有权限收窄意识 |
| `_run_checkup` 每阶段新建 `ContextManager`，仅 scratchpad 传 800 字 | 子 Agent 上下文隔离 + 只回传结论（第5课） | **已是朴素但正确的子 Agent 隔离** |
| live 模式摘除 `ingest_data`（内部 DELETE 七表） | 按上下文动态过滤危险工具（第5课） | 有"按场景禁用破坏性工具"意识 |
| `sql_query` 强制 SELECT + `_SQL_DENY` + 表白名单 + 自动 LIMIT 50 | 工具级 `validateInput`（第1/3课） | **已是工具自检的正确范例**，其他工具应对齐此标准 |
| `intent.classify` LLM 失败降级正则 | fallback 链 | 有降级意识 |
| `background.scan_loop` 冷启动先做基线扫描不计 new | 避免首轮误报 | 细节到位 |

### 2.2 核心差距

对照六课原理，缺失项按影响排序：

1. **权限门禁完全缺失**（第3课）——`patch_deployment` / `upgrade_rds_instance` / `create_db_index` / `ingest_data` 直接执行，唯一约束是 `_BASE_PROMPT` 第 4 条"执行治理前必须得到用户明确确认"。**这是提示词约束而非机制约束**，模型误判、SLS 日志内容注入、幻觉都能穿透。
2. **不可中断**（第2课）——`main.py` daemon 线程与 SSE 生命周期无关联。
3. **无失败自愈**（第2课）——`llm.chat` 抛异常 → `yield error; return`。
4. **上下文压缩会破坏 API 不变量**（第4课）——见 Bug 1。
5. **工具结果硬截断丢数据**（第4课）——运维场景日志/资源量大，2000 字符截断后不可恢复。
6. **无 token/成本记账**（第4课）——只有 `total_chars()` 代理指标，且统计不全。
7. **Skill 无渐进式披露**（第6课）——整篇 md 拼进系统提示词，随技能增长线性膨胀。
8. **无并行工具执行**（第2课）——多个只读查询串行等待。
9. **无决策审计**（第3课）——运维场景无法回答"这次变更谁批准的"。

---

## 3. Part A：3 个真实 Bug（P0，先修）

### Bug 1 ⚠️ 上下文压缩切断 tool_call / tool_result 配对（会导致 400 崩溃）

**位置**：[`harness/context.py:67-77`](../backend/app/harness/context.py)

```python
def compact(self):
    if self.total_chars() <= config.CONTEXT_MAX_CHARS:
        return
    head, tail = self.messages[0], self.messages[-6:]   # ← 按【条数】切，不看类型
    middle = self.messages[1:-6]
```

**问题**：`messages[-6:]` 完全不考虑消息类型。OpenAI 兼容接口要求每条 `role="tool"` 必须紧跟在携带对应 `tool_call_id` 的 `assistant` 之后。切点落错就产生"孤立的 tool 消息"。

**必然触发的场景**：

```
索引: 0 system │ 1 user │ 2 A1(tool_calls=[a]) │ 3 tool(a)
      4 A2(tool_calls=[b]) │ 5 tool(b) │ 6 A3(tool_calls=[c,d]) │ 7 tool(c) │ 8 tool(d)

len=9 → messages[-6:] 起点 = 索引 3
     → tail = [tool(a), A2, tool(b), A3, tool(c), tool(d)]
                ↑ tool(a) 的 assistant(A1) 已被压进摘要
     → 压缩后消息列表以孤立 tool 开头
     → 400 messages with role 'tool' must be a response to a preceding message with tool_calls
```

另一条同样必然的路径：**单条 assistant 携带 ≥6 个 tool_calls** 时，`messages[-6:]` 全是 tool 消息，一条 assistant 都没有。

**触发概率评估**：Agent 有 11+ 个工具，`MAX_AGENT_STEPS=12`，qwen 常一次并行发多个 tool_calls，且 `CONTEXT_MAX_CHARS=24000` 在 12 步 × 每步多个 2000 字符结果下很容易突破。**这是高概率线上崩溃，不是理论风险。**

**修法**：把切点往前推到安全位置（对应第4课 `adjust_index_to_preserve_api_invariants`）。

```python
def _safe_tail_start(self, want_tail: int = 6) -> int:
    """把尾窗起点往前推，保证切片不以 role='tool' 开头。

    返回可安全切片的起始索引：从期望位置向前扫，只要落在 tool 消息上就继续前移，
    直到停在其对应的 assistant(tool_calls) 或更早的 user/assistant 上。
    """
    i = max(1, len(self.messages) - want_tail)
    while i > 1 and self.messages[i].get("role") == "tool":
        i -= 1
    return i


def compact(self):
    """历史超窗口时压缩：保留 system + 安全尾窗，中段压成摘要。"""
    if self.total_chars() <= config.CONTEXT_MAX_CHARS:
        return
    start = self._safe_tail_start()
    middle = self.messages[1:start]
    if not middle:
        return
    digest = self._summarize(middle)
    self.messages = [
        self.messages[0],
        {"role": "user", "content": f"[前序对话与工具调用摘要]\n{digest}"},
        *self.messages[start:],
    ]
    logger.info("context compacted to %d chars (tail from %d)", self.total_chars(), start)
```

**配套：开发期护栏**（把这类问题在测试期就暴露，而不是等线上 400）

```python
def assert_api_invariants(self):
    """开发期自检：每条 role='tool' 都必须能匹配到前面某条 assistant 的 tool_call id。

    在 loop 每次调用 LLM 前执行（可用环境变量 HARNESS_STRICT 控制是否启用）。
    """
    pending: set[str] = set()
    for idx, m in enumerate(self.messages):
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            pending |= {tc["id"] for tc in m["tool_calls"]}
        elif role == "tool":
            tid = m.get("tool_call_id")
            if tid not in pending:
                raise AssertionError(
                    f"孤立的 tool 消息 at[{idx}] name={m.get('name')} id={tid}；"
                    f"已知 pending={sorted(pending)}")
```

**验收**：构造一个 12 步、每步 3 个并行工具调用的会话，确认压缩触发后不报 400；开启 `HARNESS_STRICT=1` 跑一遍 static 模式全面体检。

---

### Bug 2 达到最大步数时丢弃全部已积累结论

**位置**：[`harness/loop.py:42,57,71-72`](../backend/app/harness/loop.py)

```python
final_answer = ""                                    # L42
for step in range(config.MAX_AGENT_STEPS):
    ...
    if not msg.tool_calls:
        final_answer = msg.content or ""             # L57 —— 仅在 break 分支赋值
        break
else:
    final_answer = "已达最大执行步数，基于现有信息给出结论：\n" + (
        final_answer or "（未能得出结论，请缩小问题范围重试）")   # L71-72
```

**问题**：`for...else` 分支执行的前提是循环**未 break**，此时 `final_answer` 恒为 `""`，所以 `final_answer or ...` 永远走 fallback 分支。

**后果**：跑满 12 步的复杂排查（恰恰是最有价值的场景——多轮 trace 下钻、跨表关联）中，模型积累的所有 `thinking` 文本全部丢弃，用户只看到"未能得出结论，请缩小问题范围重试"。这是**静默的价值损失**，不报错，但体验极差。

**修法（两步）**：

```python
# ① 保留每一步的思考文本
last_thinking = ""
for step in range(config.MAX_AGENT_STEPS):
    ...
    if msg.content:
        last_thinking = msg.content          # ← 记住最近一次思考
        yield {"type": "thinking", "text": msg.content}
    if not msg.tool_calls:
        final_answer = msg.content or last_thinking
        break

# ② 步数耗尽时不直接放弃，追加 meta 消息让模型收口
#    （对应第2课 max_output_tokens_recovery：注入 nudge 而非报错退出）
else:
    ctx.add_user(
        "[系统] 已达最大工具调用步数上限。请立刻停止调用任何工具，"
        "基于目前已获得的真实数据给出阶段性结论，"
        "并明确说明哪些环节证据不足、建议下一步查什么。")
    try:
        msg = llm.chat_with_retry(ctx.messages, tools=None, run=run)   # tools=None 强制出文本
        final_answer = msg.content or last_thinking
    except Exception:                                                  # noqa: BLE001
        final_answer = last_thinking or "（未能得出结论，请缩小问题范围重试）"
    if final_answer:
        final_answer = (f"> ⚠️ 已达最大执行步数（{config.MAX_AGENT_STEPS}），"
                        f"以下为阶段性结论\n\n{final_answer}")
```

**验收**：把 `MAX_AGENT_STEPS` 临时改为 3，跑一次故障定位，确认输出的是有内容的阶段性结论而非固定 fallback 文案。

---

### Bug 3 governance 记忆无限增长并挤掉诊断结论

**位置**：[`tools/remediation_tools.py:42-46`](../backend/app/tools/remediation_tools.py) + [`harness/memory.py:42-58`](../backend/app/harness/memory.py)

```python
# remediation_tools.py —— 纯 INSERT，无去重、无上限
def _record_governance(key: str, content: str):
    db.execute("""INSERT INTO agent_memory (scope, mem_key, content, created_at)
                  VALUES ('governance', :k, :c, :t)""", {...})

# memory.py —— 无 scope 配额，按时间倒序取 8 条
return db.fetch_all(
    "SELECT scope, mem_key, content, created_at FROM agent_memory "
    "ORDER BY created_at DESC LIMIT :l", {"l": limit})
```

**问题**：`memory.remember()` 有 upsert 逻辑，但 `_record_governance` 绕过了它直接 INSERT。跑几轮全量治理（11 条风险 × 多次演示）后，`agent_memory` 里堆积几十条 governance 记录，而 `recall()` 默认取最近 8 条——**全是治理动作**，`fault_diagnose` / `risk_scan` / `full_checkup` 写入的诊断结论被完全挤出。

**后果**：长期记忆功能事实上失效。这也解释了为什么演示多轮后 Agent 的"历史结论引用"能力会退化。

**修法（两处）**：

```python
# ① memory.py：按 scope 分配额度，避免单一 scope 挤占全部名额
_SCOPE_QUOTA = {"conclusion": 3, "governance": 3, "preference": 2}

def recall(query: str = None, limit: int = 8) -> list:
    """按 scope 配额检索长期记忆。带 query 时优先关键词命中，再按配额补齐。"""
    picked, seen = [], set()

    if query:                                   # 关键词命中优先
        terms = [t for t in query.replace("，", " ").replace(",", " ").split()
                 if len(t) >= 2][:5]
        if terms:
            clauses = " OR ".join(
                f"(content LIKE :t{i} OR mem_key LIKE :t{i})" for i in range(len(terms)))
            params = {f"t{i}": f"%{t}%" for i, t in enumerate(terms)}
            params["l"] = limit
            for r in db.fetch_all(
                    f"SELECT scope, mem_key, content, created_at FROM agent_memory "
                    f"WHERE {clauses} ORDER BY created_at DESC LIMIT :l", params):
                key = (r["scope"], r["mem_key"])
                if key not in seen:
                    seen.add(key)
                    picked.append(r)

    for scope, n in _SCOPE_QUOTA.items():       # 再按配额补齐，保证各类都有代表
        for r in db.fetch_all(
                "SELECT scope, mem_key, content, created_at FROM agent_memory "
                "WHERE scope=:s ORDER BY created_at DESC LIMIT :n", {"s": scope, "n": n}):
            key = (r["scope"], r["mem_key"])
            if key not in seen:
                seen.add(key)
                picked.append(r)

    return picked[:limit]


def prune(keep_per_scope: int = 20):
    """定期清理：每个 scope 只保留最近 N 条（在 remember() 后调用）。"""
    for scope in _SCOPE_QUOTA:
        db.execute(
            "DELETE FROM agent_memory WHERE scope=:s AND id NOT IN ("
            "  SELECT id FROM (SELECT id FROM agent_memory WHERE scope=:s "
            "                  ORDER BY created_at DESC LIMIT :n) AS t)",
            {"s": scope, "n": keep_per_scope})
```

```python
# ② remediation_tools.py：改用可合并的 key，复用 memory.remember() 的 upsert
def _record_governance(resource: str, content: str):
    """治理记录按资源合并，同一资源的多次治理只保留最新状态。"""
    from ..harness import memory
    memory.remember("governance", f"治理动作:{resource}", content)
```

> 注意：`_record_governance` 现有调用点传的是 `key`，改签名后需同步更新调用处（`remediation_tools.py` 内 4 个治理工具）。

**验收**：连续执行 15 次治理动作后，`SELECT scope, COUNT(*) FROM agent_memory GROUP BY scope` 各 scope 不超过 20 条；`memory_prompt()` 的输出中 conclusion 类记忆仍然存在。

---

## 4. Part B：P0 改进项（4 项，做完项目质量跃一个台阶）

### P0-1 ⭐ 工具安全谓词 + 四层权限门禁

**优先级最高。** 这是项目从"演示玩具"到"可信运维 Agent"的分界线。

#### 问题

[`loop.py:67`](../backend/app/harness/loop.py) 直接 `registry.execute(name, args)`。以下工具零门禁：

| 工具 | 真实影响 | 现有约束 |
|---|---|---|
| `patch_deployment` | 改 Deployment 副本数/探针/资源限制 | 仅提示词 |
| `upgrade_rds_instance` | RDS 升配（真实环境=花钱） | 仅提示词 |
| `create_db_index` | 生产库建索引（可能锁表） | 仅提示词 |
| `create_pdb` | 创建 PodDisruptionBudget | 仅提示词 |
| `ingest_data` | **DELETE 七张表** | live 模式在 `build_agent` 里摘除（提示词层） |

风险路径：
- 模型误判用户意图（"看看能不能扩容" → 直接扩容）
- **Prompt injection**：`query_logs` 读到的 SLS 日志内容进入上下文，恶意日志可诱导工具调用
- 幻觉：模型"以为"用户已确认

#### 设计：`ToolSpec` 安全谓词（第1课，fail-closed 默认值）

```python
# tools/registry.py
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class ToolSpec:
    func: Callable
    schema: dict
    # ── 安全属性：fail-closed 默认值，不声明就按最危险处理 ──
    is_read_only: bool = False          # 默认假设会写
    is_destructive: bool = False        # 不可逆操作（删除/覆盖/花钱）
    concurrency_safe: bool = False      # 默认不能并行
    max_result_chars: int = 2000        # 单结果预算（超出走落盘，见 P0-4）
    # 权限钩子：返回 'allow' | 'ask' | 'deny'
    check_permissions: Optional[Callable[[dict], str]] = None
    # 参数/状态校验：失败信息【发回给模型】自我纠正，不打扰用户（第1课错误分层）
    validate_input: Optional[Callable[[dict], Optional[str]]] = None
    # 审计投影：这条调用记进审计日志时展示什么（第3课 toAutoClassifierInput 同思路）
    audit_repr: Optional[Callable[[dict], str]] = None


def tool(name, description, parameters=None, *,
         is_read_only=False, is_destructive=False, concurrency_safe=False,
         max_result_chars=2000, check_permissions=None,
         validate_input=None, audit_repr=None):
    def deco(func):
        _REGISTRY[name] = ToolSpec(
            func=func,
            schema={"type": "function", "function": {
                "name": name, "description": description,
                "parameters": parameters or {"type": "object", "properties": {}}}},
            is_read_only=is_read_only, is_destructive=is_destructive,
            concurrency_safe=concurrency_safe, max_result_chars=max_result_chars,
            check_permissions=check_permissions,
            validate_input=validate_input, audit_repr=audit_repr)
        return func
    return deco


def get_spec(name: str) -> Optional[ToolSpec]:
    return _REGISTRY.get(name)
```

> **兼容性**：`_REGISTRY[n]["schema"]` 的字典访问要改成 `_REGISTRY[n].schema`。受影响的是 `get_schemas()` 和 `execute()`，共 2 处。

#### 给 18 个工具打标

```python
# data_tools.py —— 7 个查询工具全部显式声明只读 + 可并行
@tool("query_metrics", "...", {...},
      is_read_only=True, concurrency_safe=True, max_result_chars=4000)
@tool("query_logs", "...", {...},
      is_read_only=True, concurrency_safe=True, max_result_chars=6000)   # 日志量大
@tool("query_traces", "...", {...},
      is_read_only=True, concurrency_safe=True, max_result_chars=6000)
@tool("api_perf_stats", "...", {...}, is_read_only=True, concurrency_safe=True)
@tool("get_k8s_resource", "...", {...},
      is_read_only=True, concurrency_safe=True, max_result_chars=8000)   # spec_json 大
@tool("sql_query", "...", {...},
      is_read_only=True, concurrency_safe=True, max_result_chars=6000)
      # 注：sql_query 已有 SELECT-only + 表白名单 + LIMIT 50 的内部校验，无需额外 validate_input

@tool("ingest_data", "...", {...},
      is_destructive=True,          # 内部 DELETE 七张表
      check_permissions=lambda a: "deny" if config.is_live() else "ask",
      audit_repr=lambda a: "全量数据采集（清空并重建七张观测表）")
      # ↑ live 模式从"提示词层摘除"升级为"机制层拒绝"，双重保险

# topology_tools.py
@tool("build_topology", "...", {...}, max_result_chars=4000)   # 写 topology_edges，非只读
@tool("get_topology", "...", {...}, is_read_only=True, concurrency_safe=True)

# risk_tools.py
@tool("run_risk_scan", "...", {...}, max_result_chars=8000)    # 写 risk_findings
@tool("get_risk_report", "...", {...}, is_read_only=True, concurrency_safe=True)
@tool("list_risk_rules", "...", {...}, is_read_only=True, concurrency_safe=True)
@tool("create_risk_rule", "...", {...},
      check_permissions=lambda a: "ask",                        # 新增规则影响后续扫描
      audit_repr=lambda a: f"新建风险规则 {a.get('rule_id')}: {a.get('title')}")

# remediation_tools.py —— 4 个治理工具全部 destructive + ask
@tool("patch_deployment", "...", {...},
      is_destructive=True, check_permissions=lambda a: "ask",
      audit_repr=lambda a: f"patch {a.get('name')} {a.get('action')}={a.get('value')}")
@tool("create_pdb", "...", {...},
      is_destructive=True, check_permissions=lambda a: "ask",
      audit_repr=lambda a: f"创建 PDB for {a.get('name')} minAvailable={a.get('min_available')}")
@tool("create_db_index", "...", {...},
      is_destructive=True, check_permissions=lambda a: "ask",
      audit_repr=lambda a: f"建索引 {a.get('table')}({','.join(a.get('columns') or [])})")
@tool("upgrade_rds_instance", "...", {...},
      is_destructive=True, check_permissions=lambda a: "ask",
      audit_repr=lambda a: f"RDS 升配 {a.get('instance_id')} → {a.get('target_class')}")
@tool("list_governance_actions", "...", {...}, is_read_only=True, concurrency_safe=True)
```

#### 决策链（第3课七层的运维精简版，取四层）

```python
# harness/permissions.py（新建）
"""工具权限决策：四层门禁 + 三态运行模式。

运行模式（RunContext.mode）：
  readonly —— 只读巡检，拒绝一切写操作（对应 Claude Code 的 plan 模式）
  confirm  —— 默认。破坏性动作弹确认卡片
  auto     —— 无人值守。非破坏性写操作自动放行，破坏性仍需人工
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..tools import registry

Behavior = Literal["allow", "ask", "deny"]


@dataclass
class Decision:
    behavior: Behavior
    reason_type: str          # rule | mode | tool | user | auto
    message: str = ""


def approval_key(tool_name: str, args: dict) -> str:
    """会话内批准的粒度：工具 + 主要目标资源。"""
    target = (args.get("name") or args.get("instance_id")
              or args.get("table") or "*")
    return f"{tool_name}:{target}"


def decide(tool_name: str, args: dict, mode: str = "confirm",
           session_approvals: frozenset = frozenset()) -> Decision:
    spec = registry.get_spec(tool_name)

    # ── 第 1 层：未知工具直接拒绝 ──
    if spec is None:
        return Decision("deny", "rule", f"未知工具 {tool_name}")

    # ── 第 2 层：只读工具永远放行 ──
    if spec.is_read_only:
        return Decision("allow", "rule")

    # ── 第 3 层：运行模式 ──
    if mode == "readonly":
        return Decision("deny", "mode",
                        "当前为只读巡检模式，禁止执行任何写入/治理动作。"
                        "如需治理请在界面切换到「确认执行」模式。")

    # ── 第 4 层：工具自身权限逻辑（fail-closed 默认 ask）──
    behavior: Behavior = "ask"
    if spec.check_permissions:
        behavior = spec.check_permissions(args)   # type: ignore[assignment]
    if behavior == "deny":
        return Decision("deny", "tool", "工具在当前环境下不可执行")

    # 会话内已批准同类动作（用户勾选过「本次会话都允许」）
    if approval_key(tool_name, args) in session_approvals:
        return Decision("allow", "user", "会话内已批准")

    # auto 模式：非破坏性写操作放行，破坏性仍需人工
    if mode == "auto" and not spec.is_destructive:
        return Decision("allow", "mode", "auto 模式放行非破坏性写操作")

    return Decision(behavior, "tool")
```

#### 人在环：SSE 反向通道

现状是 `thread + queue` 单向推送，需要一个反向确认通道。

```python
# harness/approvals.py（新建）
"""权限确认的跨线程等待/唤醒。SSE 推 permission_request，前端 POST 回 approve。"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_WAITERS: dict[str, threading.Event] = {}
_RESULTS: dict[str, dict] = {}
_LOCK = threading.Lock()

APPROVAL_TIMEOUT_S = 300


def wait(request_id: str, timeout: float = APPROVAL_TIMEOUT_S) -> dict:
    """阻塞等待用户决策。返回 {"approved": bool, "remember": bool}。

    ★ 超时默认【拒绝】（fail-closed）——用户没看到卡片就不该执行治理。
    """
    with _LOCK:
        ev = _WAITERS.setdefault(request_id, threading.Event())
    if not ev.wait(timeout):
        logger.warning("权限确认超时(%ss)，默认拒绝: %s", timeout, request_id)
        _cleanup(request_id)
        return {"approved": False, "remember": False, "timeout": True}
    with _LOCK:
        result = _RESULTS.pop(request_id, {"approved": False, "remember": False})
        _WAITERS.pop(request_id, None)
    return result


def resolve(request_id: str, approved: bool, remember: bool = False) -> bool:
    """前端回调：写入结果并唤醒等待线程。返回是否命中了等待者。"""
    with _LOCK:
        _RESULTS[request_id] = {"approved": approved, "remember": remember}
        ev = _WAITERS.setdefault(request_id, threading.Event())
    ev.set()
    return True


def cancel_all(prefix: str = ""):
    """会话中断时唤醒所有等待者（按拒绝处理），避免线程泄漏。"""
    with _LOCK:
        ids = [k for k in _WAITERS if k.startswith(prefix)]
    for rid in ids:
        resolve(rid, approved=False)


def _cleanup(request_id: str):
    with _LOCK:
        _WAITERS.pop(request_id, None)
        _RESULTS.pop(request_id, None)
```

```python
# main.py 新增接口
class ApproveRequest(BaseModel):
    request_id: str
    approved: bool
    remember: bool = False        # 本次会话内同类动作都允许


@app.post("/api/chat/approve")
def approve(req: ApproveRequest):
    """前端确认卡片的回调。"""
    approvals.resolve(req.request_id, req.approved, req.remember)
    return {"ok": True}
```

#### loop.py 的工具执行段重写

```python
def _execute_one(tc, ctx, run, agent_name: str):
    """执行单个 tool_call，yield 事件。

    ★ 铁律：每一条提前 return 的路径都必须先 ctx.add_tool_result()，
      否则会制造孤立的 tool_call（下一轮 API 400，即 Bug 1 的另一种成因）。
    """
    name = tc.function.name
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    yield {"type": "tool_call", "tool": name, "args": args}

    spec = registry.get_spec(name)

    def reject(payload: dict):
        """统一的拒绝路径：补齐 tool_result + 推事件。"""
        body = json.dumps(payload, ensure_ascii=False)
        ctx.add_tool_result(tc.id, name, body)
        return {"type": "tool_result", "tool": name, "result": payload}

    # ① 参数/状态校验 —— 失败信息回给【模型】自我纠正
    if spec and spec.validate_input:
        if err := spec.validate_input(args):
            yield reject({"error": err, "hint": "请修正参数后重试"})
            return

    # ② 权限决策 —— 升级给【用户】
    d = permissions.decide(name, args, mode=run.mode,
                           session_approvals=frozenset(run.approvals))
    summary = spec.audit_repr(args) if (spec and spec.audit_repr) else name

    if d.behavior == "deny":
        audit.record(run, agent_name, name, args, d, spec, summary, "denied")
        yield reject({"error": f"权限拒绝：{d.message}", "reason": d.reason_type})
        return

    if d.behavior == "ask":
        yield {"type": "permission_request", "request_id": tc.id, "tool": name,
               "args": args, "summary": summary,
               "is_destructive": bool(spec and spec.is_destructive)}
        verdict = approvals.wait(tc.id)
        if not verdict["approved"]:
            audit.record(run, agent_name, name, args, d, spec, summary,
                         "timeout" if verdict.get("timeout") else "rejected")
            msg = "确认超时，操作未执行" if verdict.get("timeout") else "用户拒绝了此操作"
            yield reject({"error": msg})
            return
        if verdict.get("remember"):
            run.approvals.add(permissions.approval_key(name, args))
        d = permissions.Decision("allow", "user", "用户批准")

    # ③ 执行
    t0 = time.perf_counter()
    result = registry.execute(name, args)
    ms = int((time.perf_counter() - t0) * 1000)
    status = "error" if '"error"' in result[:200] else "ok"
    audit.record(run, agent_name, name, args, d, spec, summary, status, ms)

    # ④ 落盘 + 预览（见 P0-4）
    stored = tool_results.persist_and_preview(
        run.session_id, tc.id, name, result,
        spec.max_result_chars if spec else config.TOOL_RESULT_MAX_CHARS)
    ctx.add_tool_result(tc.id, name, stored)
    yield {"type": "tool_result", "tool": name, "result": _preview(result),
           "duration_ms": ms}
```

#### 演示价值

三态模式是**成本最低、说服力最高的演示点**：

```
现场演示脚本：
1. 切到「只读巡检」→ 让 Agent 执行治理 → 被机制拦下并说明原因
2. 切回「确认执行」→ 同一指令 → 弹出确认卡片（含 audit_repr 摘要）
3. 点击批准 → 执行 → 复扫验证 resolved
4. 打开「操作审计」页 → 看到这条动作、谁批准的、reason_type=user
```

比"我们有安全设计"的口头声明强得多，也直接对应真实运维的合规要求。

**工作量**：registry 扩展 + 18 个工具打标 + permissions.py + approvals.py + loop 改造 + 1 个 API + 前端确认卡片 ≈ **1 天**。

---

### P0-2 RunContext + 可中断（abort）

#### 问题

[`main.py:55-66`](../backend/app/main.py) 起 daemon 线程跑 `handle_message`，与 SSE 生命周期无任何关联：

- 用户关闭页面 / 网络断开 → **worker 线程照样把 12 步跑完**，持续消耗 qwen-max token
- 模型陷入无效循环时**没有任何停止手段**
- 无单轮时间上限、无 token 预算上限

#### 设计：贯穿全链路的运行上下文

```python
# harness/runctx.py（新建）
"""单次对话的运行上下文：中断信号 + 运行模式 + 预算。

对应 Claude Code 的 ToolUseContext（abortController + 预算 + 会话状态）。
Python 里用 threading.Event 承担 AbortController 的角色。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class RunContext:
    session_id: str
    mode: str = "confirm"                     # readonly | confirm | auto
    abort: threading.Event = field(default_factory=threading.Event)
    approvals: set[str] = field(default_factory=set)   # 会话内已批准的动作 key

    # ── 预算（第2/4课：所有自动流程都要有硬上限）──
    tokens_in: int = 0
    tokens_out: int = 0
    max_tokens: int = 120_000
    started_at: float = field(default_factory=time.time)
    max_wall_s: float = 300.0

    # ── 熔断计数器（★ 必须存在这里而不是局部变量，
    #    否则跨恢复路径会被重置 —— 第2/4课反复强调的教训）──
    llm_failures: int = 0
    emergency_compacts: int = 0
    max_emergency_compacts: int = 1

    def should_stop(self) -> tuple[bool, str]:
        if self.abort.is_set():
            return True, "用户中断"
        if self.tokens_in + self.tokens_out > self.max_tokens:
            return True, f"超出 token 预算（{self.max_tokens:,}）"
        if time.time() - self.started_at > self.max_wall_s:
            return True, f"超出时间预算（{self.max_wall_s:.0f}s）"
        return False, ""

    def usage_event(self) -> dict:
        """推给前端的用量事件。价格按百炼 qwen-max 估算，仅作量级参考。"""
        total = self.tokens_in + self.tokens_out
        return {
            "type": "usage",
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "est_cost_cny": round(
                (self.tokens_in * 0.02 + self.tokens_out * 0.06) / 1000, 4),
            "budget_pct": round(100 * total / self.max_tokens),
            "elapsed_s": round(time.time() - self.started_at, 1),
        }


# 活跃会话注册表，供 /api/chat/stop 查找
_RUNS: dict[str, RunContext] = {}


def register(run: RunContext):
    _RUNS[run.session_id] = run


def unregister(session_id: str):
    _RUNS.pop(session_id, None)


def get(session_id: str) -> RunContext | None:
    return _RUNS.get(session_id)
```

#### 三个中断检查点（第2课的检查点分布）

```python
# loop.py
for step in range(config.MAX_AGENT_STEPS):
    # ① 每步开头
    stop, why = run.should_stop()
    if stop:
        yield {"type": "aborted", "reason": why}
        return

    ctx.compact()
    msg = llm.chat_with_retry(...)
    ...

    # ② 每个工具执行前（长工具尤其重要）
    for idx, tc in enumerate(msg.tool_calls):
        if run.abort.is_set():
            # ★ 必须补齐【剩余全部】tool_call 的结果，保持配对
            for rest in msg.tool_calls[idx:]:
                ctx.add_tool_result(rest.id, rest.function.name,
                                    '{"error":"用户中断，工具未执行"}')
            yield {"type": "aborted", "reason": "用户中断"}
            return
        yield from _execute_one(tc, ctx, run, agent["name"])
```

```python
# main.py
@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    run = runctx.RunContext(session_id=req.session_id,
                            mode=req.mode or "confirm")
    runctx.register(run)

    q: queue.Queue = queue.Queue()
    _SENTINEL = object()

    def worker():
        try:
            for ev in scheduler.handle_message(req.session_id, req.message, run=run):
                q.put(ev)
        except Exception as e:                                # noqa: BLE001
            logger.exception("chat worker failed")
            q.put({"type": "error", "text": f"内部错误: {e}"})
            q.put({"type": "done"})
        finally:
            q.put(_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    async def event_stream():
        loop = asyncio.get_event_loop()
        try:
            while True:
                # ③ 客户端断开检测
                if await request.is_disconnected():
                    logger.info("客户端断开，中止会话 %s", req.session_id)
                    run.abort.set()
                    break
                try:
                    ev = await loop.run_in_executor(
                        None, lambda: q.get(timeout=1.0))
                except queue.Empty:
                    continue                                  # 回到 disconnect 检查
                if ev is _SENTINEL:
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        finally:
            run.abort.set()                                   # 任何退出路径都置位
            approvals.cancel_all()                            # 唤醒挂起的确认等待
            runctx.unregister(req.session_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/chat/stop")
def stop_chat(session_id: str = "default"):
    """前端「停止」按钮。"""
    if run := runctx.get(session_id):
        run.abort.set()
        approvals.cancel_all()
        return {"ok": True, "stopped": True}
    return {"ok": True, "stopped": False, "reason": "无活跃会话"}
```

> **注意**：`q.get(timeout=1.0)` 配合 `queue.Empty` 是必需的——原来的阻塞 `q.get()` 会让 disconnect 检查永远得不到执行机会。

**工作量 ≈ 2 小时。收益/成本比全清单最高。**

---

### P0-3 LLM 重试 + 分级自愈 + 熔断

#### 问题

[`loop.py:46-50`](../backend/app/harness/loop.py)：

```python
try:
    msg = llm.chat(ctx.messages, tools=tools)
except Exception as e:
    yield {"type": "error", "text": f"LLM 调用失败: {e}"}
    return                    # ← 一次瞬时 429/500 就整轮废掉
```

DashScope 的限流和瞬时 5xx 是常态。且**上下文超长错误**目前会被当成普通错误直接放弃，而它是完全可恢复的。

#### 设计

```python
# harness/llm.py 扩展
import logging
import random
import time

logger = logging.getLogger(__name__)

RETRYABLE_MARKERS = ("rate_limit", "Throttling", "429", "500", "502", "503", "504",
                     "timeout", "Timeout", "Connection", "ServiceUnavailable")
CONTEXT_OVERFLOW_MARKERS = ("context_length", "too long", "maximum context",
                            "InvalidParameter", "Range of input length")


class ContextTooLong(Exception):
    """上下文超长：不重试，交给调用方压缩后重试。"""


class Aborted(Exception):
    """运行期被中断。"""


def chat_with_retry(messages: list, tools: list = None, model: str = None,
                    temperature: float = 0.3, max_attempts: int = 3, run=None):
    """指数退避 + 抖动重试；顺带把 usage 记进 RunContext。

    错误分三类（第2课的错误分级）：
      - 上下文超长 → 抛 ContextTooLong，调用方压缩后重试（可恢复）
      - 可重试错误 → 退避重试，1s/2s/4s + 抖动
      - 其他 → 直接抛出（不可恢复）
    """
    last_exc = None
    for attempt in range(max_attempts):
        if run and run.abort.is_set():
            raise Aborted("用户中断")
        try:
            kwargs = {"model": model or config.LLM_MODEL,
                      "messages": messages, "temperature": temperature}
            if tools:
                kwargs["tools"] = tools
            resp = get_client().chat.completions.create(**kwargs)
            if run and (u := getattr(resp, "usage", None)):
                run.tokens_in += getattr(u, "prompt_tokens", 0) or 0
                run.tokens_out += getattr(u, "completion_tokens", 0) or 0
            if run:
                run.llm_failures = 0            # 成功打断连续失败链
            return resp.choices[0].message
        except Exception as e:                  # noqa: BLE001
            last_exc, s = e, str(e)
            if any(k in s for k in CONTEXT_OVERFLOW_MARKERS):
                raise ContextTooLong(s) from e
            if run:
                run.llm_failures += 1
            if not any(k in s for k in RETRYABLE_MARKERS) or attempt == max_attempts - 1:
                raise
            delay = min(2 ** attempt, 8) + random.random()
            logger.warning("LLM 第 %d/%d 次失败（%s），%.1fs 后重试",
                           attempt + 1, max_attempts, s[:120], delay)
            time.sleep(delay)
    raise last_exc                              # pragma: no cover
```

```python
# loop.py 的自愈分支（注意护栏标志存在 run 里，绝不在循环内重置）
for step in range(config.MAX_AGENT_STEPS):
    ...
    try:
        msg = llm.chat_with_retry(ctx.messages, tools=tools,
                                  model=agent.get("model"), run=run)
    except llm.ContextTooLong:
        if run.emergency_compacts >= run.max_emergency_compacts:
            yield {"type": "error",
                   "text": "上下文超长，紧急压缩后仍无法恢复。请开启新会话继续。"}
            return
        run.emergency_compacts += 1
        freed = ctx.force_compact(keep_tail=4)     # 更激进：只留 system + 摘要 + 4 条
        yield {"type": "compacted", "reason": "上下文超长，已紧急压缩后重试",
               "freed_chars": freed}
        continue                                    # ← 自愈重试
    except llm.Aborted:
        yield {"type": "aborted", "reason": "用户中断"}
        return
    except Exception as e:                          # noqa: BLE001
        # 主模型持续失败 → 降级到快模型再试一次（第2课模型降级）
        if run.llm_failures >= 3 and agent.get("model") != config.LLM_MODEL_FAST:
            logger.warning("主模型连续失败，降级到 %s", config.LLM_MODEL_FAST)
            agent["model"] = config.LLM_MODEL_FAST
            yield {"type": "model_fallback", "to": config.LLM_MODEL_FAST,
                   "text": f"主模型不可用，已切换到 {config.LLM_MODEL_FAST}"}
            continue
        yield {"type": "error", "text": f"LLM 调用失败（已重试）: {e}"}
        return
```

配套的 `force_compact`：

```python
# context.py
def force_compact(self, keep_tail: int = 4) -> int:
    """紧急压缩：无条件执行，尾窗更小。返回释放的字符数。"""
    before = self.total_chars()
    start = self._safe_tail_start(want_tail=keep_tail)
    middle = self.messages[1:start]
    if middle:
        digest = self._summarize(middle)
        self.messages = [self.messages[0],
                         {"role": "user", "content": f"[前序摘要]\n{digest}"},
                         *self.messages[start:]]
    # 仍然超长 → 对尾窗里的大工具结果做二次截断
    if self.total_chars() > config.CONTEXT_MAX_CHARS:
        for m in self.messages[2:]:
            if m.get("role") == "tool" and len(str(m.get("content") or "")) > 500:
                m["content"] = str(m["content"])[:500] + "\n...[紧急压缩截断]"
    return before - self.total_chars()
```

**工作量 ≈ 3 小时。**

---

### P0-4 工具结果落盘替代硬截断

#### 问题

[`context.py:47-53`](../backend/app/harness/context.py) 在 `TOOL_RESULT_MAX_CHARS=2000` 处硬切，**剩余内容永久丢失**。

运维场景这是硬伤：

| 工具 | 典型结果规模 | 2000 字符能装下的 |
|---|---|---|
| `query_logs`（500 条 nginx 日志） | 数万字符 | 约 10 条 |
| `get_k8s_resource`（完整 Deployment spec） | 数千~上万 | spec 的开头几个字段 |
| `run_risk_scan`（11 条 finding + 建议） | 数千 | 前 3~4 条 |
| `query_traces`（一条完整 trace 的 spans） | 数千 | 前几个 span |

提示词引导模型"用 sql_query 精查"，但**模型得先知道该查什么**——而它连数据概貌和字段结构都没看到。

#### 设计：结构感知预览 + 落盘 + 幂等（第4课 L1）

```python
# harness/tool_results.py（新建）
"""超长工具结果落盘 + 结构感知预览。

对应 Claude Code 的 toolResultStorage：不做无脑截断，而是
「保留结构与总量的预览 + 可取回的路径」。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

STORE = config.BACKEND_DIR / ".tool_results"
SAMPLE_N = 3


def persist_and_preview(session_id: str, tool_call_id: str, tool_name: str,
                        result: str, limit: int) -> str:
    """超限结果落盘，返回给模型的是结构感知预览 + 取回说明。"""
    if len(result) <= limit:
        return result

    d = STORE / session_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{tool_name}-{tool_call_id}.json"
    if not path.exists():          # ★ 幂等：同 tool_call_id 内容确定，不重复写
        try:
            path.write_text(result, encoding="utf-8")
        except OSError as e:
            logger.warning("落盘失败 %s: %s", path, e)
            return result[:limit] + f"\n...[结果超长已截断，原始 {len(result)} 字符]"

    return f"{_structural_preview(result, limit)}\n\n{_retrieval_note(path, len(result))}"


def _structural_preview(result: str, limit: int) -> str:
    """结构感知预览：JSON 数组只留前 N 条 + 总数，保留完整字段结构。

    比截断前 N 个字符有用一个数量级 —— 模型看到
    {"logs": {"_total": 487, "_sample": [{完整字段}]}}
    就知道有 487 条、每条什么结构，可以直接写出精确的 sql_query。
    """
    try:
        obj = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return result[:limit]

    if isinstance(obj, list):
        obj = {"_root_list": obj}

    if not isinstance(obj, dict):
        return result[:limit]

    summary: dict = {}
    for k, v in obj.items():
        if isinstance(v, list):
            summary[k] = {"_total": len(v), "_sample": v[:SAMPLE_N]}
        else:
            summary[k] = v
    text = json.dumps(summary, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit]


def _retrieval_note(path: Path, size: int) -> str:
    return (f"<persisted-output>\n"
            f"完整结果 {size} 字符，已保存到：{path}\n"
            f"上方为前 {SAMPLE_N} 条样本 + 各字段总数，字段结构已完整呈现。\n"
            f"需要全量分析时：优先用 sql_query 精确查询（数据都在库里，可聚合过滤）；\n"
            f"确需原始内容时用 read_tool_result(path=\"{path}\", offset=0, limit=200)。\n"
            f"</persisted-output>")


def cleanup(session_id: str):
    """会话结束清理（可选，也可交给定期任务）。"""
    d = STORE / session_id
    if d.exists():
        for f in d.iterdir():
            f.unlink(missing_ok=True)
        d.rmdir()
```

配套取回工具（**必须有，否则落盘等于丢失**）：

```python
# data_tools.py 新增
@tool("read_tool_result",
      "分页读取此前因超长而落盘的工具结果。先看预览确认需要哪一段，再用 offset 定位。"
      "仅当 sql_query 无法满足（如需要原始日志文本）时使用。",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "预览中给出的落盘路径"},
           "offset": {"type": "integer", "description": "起始行号，默认 0"},
           "limit": {"type": "integer", "description": "读取行数，默认 200"}},
       "required": ["path"]},
      is_read_only=True, concurrency_safe=True, max_result_chars=6000)
def read_tool_result(path: str, offset: int = 0, limit: int = 200):
    from ..harness.tool_results import STORE
    p = Path(path).resolve()
    if not str(p).startswith(str(STORE.resolve())):   # ★ 路径逃逸防护
        return {"error": "路径不在允许范围内"}
    if not p.exists():
        return {"error": f"文件不存在：{path}"}
    lines = p.read_text(encoding="utf-8").splitlines()
    return {"total_lines": len(lines), "offset": offset,
            "returned": len(lines[offset:offset + limit]),
            "lines": lines[offset:offset + limit]}
```

> `.tool_results/` 需加入 `backend/.gitignore`。

**工作量 ≈ 3 小时。**


---

## 5. Part C：P1 改进项（5 项，能力升级）

### P1-1 只读工具并行执行

#### 问题

[`loop.py:60-70`](../backend/app/harness/loop.py) 的 `for tc in msg.tool_calls` 是**纯串行**。故障定位场景模型常一次请求 4~5 个查询（指标 + 日志 + trace + K8s 资源），每个几百毫秒的 DB 查询串行累加，用户白等 2~3 秒。

#### 设计：保序分批（第2课 `partitionToolCalls`）

关键约束：**保持模型给出的顺序，只合并"连续的"并发安全调用**。因为 `[查A, 改B, 查C]` 中 C 可能依赖 B 的效果，不能重排。

```python
# harness/loop.py
from concurrent.futures import ThreadPoolExecutor

MAX_PARALLEL_TOOLS = 5


def _partition(tool_calls) -> list[tuple[bool, list]]:
    """把 tool_calls 切成批次：连续的并发安全调用合并成一批，其余单独成批。

    [查A, 查B, 改C, 查D] → [(True,[A,B]), (False,[C]), (True,[D])]

    fail-closed：谓词判断本身抛异常时按「不安全」处理。
    """
    batches: list[tuple[bool, list]] = []
    for tc in tool_calls:
        try:
            spec = registry.get_spec(tc.function.name)
            safe = bool(spec and spec.concurrency_safe)
        except Exception:                          # noqa: BLE001
            safe = False
        if safe and batches and batches[-1][0]:
            batches[-1][1].append(tc)
        else:
            batches.append((safe, [tc]))
    return batches
```

并行批的执行需要**先统一过完权限、再并发跑**（避免多个确认卡片同时弹出）：

```python
def _run_batch_parallel(batch, ctx, run, agent_name):
    """并行批：全是只读工具，权限决策必然是 allow，可直接并发。"""
    prepared = []
    for tc in batch:
        name = tc.function.name
        args = _parse_args(tc.function.arguments)
        yield {"type": "tool_call", "tool": name, "args": args}
        prepared.append((tc, name, args))

    with ThreadPoolExecutor(max_workers=min(len(prepared), MAX_PARALLEL_TOOLS)) as ex:
        futures = {ex.submit(registry.execute, name, args): (tc, name, args)
                   for tc, name, args in prepared}
        for fut, (tc, name, args) in futures.items():
            result = fut.result()
            spec = registry.get_spec(name)
            audit.record(run, agent_name, name, args,
                         permissions.Decision("allow", "rule"), spec, name, "ok")
            stored = tool_results.persist_and_preview(
                run.session_id, tc.id, name, result,
                spec.max_result_chars if spec else config.TOOL_RESULT_MAX_CHARS)
            ctx.add_tool_result(tc.id, name, stored)
            yield {"type": "tool_result", "tool": name, "result": _preview(result)}


# 主循环里替换原来的串行遍历
for parallel, batch in _partition(msg.tool_calls):
    if run.abort.is_set():
        _fill_remaining_tool_results(ctx, msg.tool_calls, from_tc=batch[0])
        yield {"type": "aborted", "reason": "用户中断"}
        return
    if parallel and len(batch) > 1:
        yield from _run_batch_parallel(batch, ctx, run, agent["name"])
    else:
        for tc in batch:
            yield from _execute_one(tc, ctx, run, agent["name"])
```

> **必须遵守**：并行批只能包含只读工具。`patch_deployment` 等治理工具会并发写 `k8s_resources` 快照，交错写入会破坏 spec_json。这正是 `concurrency_safe` 默认 `False` 的价值（fail-closed）。
>
> 另外 `db.py` 的连接需确认线程安全（SQLite 已启用 WAL + busy_timeout；MySQL 需确认 SQLAlchemy 连接池配置支持多线程取连接）。

**工作量 ≈ 3 小时（含并发安全验证）。**

---

### P1-2 工具化的子 Agent：`dispatch_agent`

#### 问题

[`scheduler._run_checkup`](../backend/app/harness/scheduler.py) 是**硬编码的 3 步序列**：

```python
steps = [("data", "..."), ("topology", "..."), ("risk", "...")]
```

改进方向不是加更多硬编码分支（`if intent == "xxx": steps = [...]`），而是**把"派子 Agent"变成一个工具**，让主 Agent 根据集群实际状况自己决定派谁、派几个、并行还是串行。

#### 对你们场景的收益

| 场景 | 现状 | 用 `dispatch_agent` 后 |
|---|---|---|
| 11 类风险规则扫描 | 单 Agent 串行，全部中间数据进主上下文 | 按类别派 3~4 个并行子 Agent（HA类 / CAP类 / DB+API类），只回传 P1/P2 结论 |
| 故障定位多假设验证 | 单线程逐个假设试 | 并行验证"RDS 慢查询 / Ingress 限流 / Pod OOM"三条假设，主 Agent 综合证据 |
| 全面体检 | 硬编码 3 步 | 主 Agent 按当前集群状况动态编排，发现异常可追加派诊断子 Agent |

#### 实现

```python
# tools/agent_tools.py（新建）
"""子 Agent 派发工具：把「派专家 Agent」变成 LLM 可调用的能力。

对应 Claude Code 的 AgentTool（第5课）。三条核心约束：
  1. 只回传最终结论 —— 中间的日志/trace 全留在子 Agent 上下文里
  2. 子 Agent 机制上只读 —— 过滤掉所有非只读工具，不靠提示词约束
  3. 独立预算 —— 防止单个子 Agent 跑飞拖垮整轮
"""
from __future__ import annotations

from .. import config
from .registry import tool, get_spec

SUBAGENT_TYPES = ["topology", "risk", "diagnose"]


@tool(
    "dispatch_agent",
    "派一个专家子 Agent 执行独立的探索性子任务。\n"
    "子 Agent 有【独立的上下文】，看不到你当前的对话——所以 task 必须是自包含的完整描述。\n"
    "子 Agent 只会把【最终结论文本】回传给你，中间的查询过程不会进入你的上下文，"
    "因此适合中间数据量大的任务（日志分析、全表扫描、多轮 trace 下钻）。\n"
    "子 Agent 是【只读】的，无法执行治理动作；如需治理请你自己调用治理工具。\n"
    "可用类型：\n"
    "- topology: 拓扑梳理与异常边识别\n"
    "- risk: 风险扫描与治理建议（只输出建议，不执行）\n"
    "- diagnose: 按五步排查法定位单个故障的根因，输出证据链\n"
    "多个独立子任务可以在同一轮里并行派发。",
    {
        "type": "object",
        "properties": {
            "subagent_type": {"type": "string", "enum": SUBAGENT_TYPES},
            "task": {"type": "string",
                     "description": "完整、自包含的任务描述。务必在末尾要求"
                                    "「输出完整结论，包含关键数值与资源名」"},
            "description": {"type": "string", "description": "3-5 字任务名，用于 UI 展示"},
        },
        "required": ["subagent_type", "task"],
    },
    is_read_only=True,          # 子 Agent 只读 → 本工具对外部世界只读
    concurrency_safe=True,      # 多个子 Agent 可并行（配合 P1-1）
    max_result_chars=8000,
    audit_repr=lambda a: f"派发 {a.get('subagent_type')} 子Agent: {a.get('description') or ''}",
)
def dispatch_agent(subagent_type: str, task: str, description: str = ""):
    from ..agents.base import build_agent
    from ..harness.loop import run_agent
    from ..harness.runctx import RunContext

    if subagent_type not in SUBAGENT_TYPES:
        return {"error": f"不支持的子 Agent 类型: {subagent_type}，"
                         f"可选 {SUBAGENT_TYPES}"}

    agent = build_agent(subagent_type, task)

    # ★ 机制层收窄：只保留只读工具（不依赖提示词），并禁止再派子 Agent（防繁殖）
    agent["tools"] = [
        t for t in agent["tools"]
        if t != "dispatch_agent" and (s := get_spec(t)) and s.is_read_only
    ]

    # ★ 独立预算：40k token / 120s，防止跑飞
    sub_run = RunContext(session_id=f"sub-{subagent_type}", mode="readonly",
                         max_tokens=40_000, max_wall_s=120.0)

    answer, tool_calls, thinking_tail = "", 0, ""
    for ev in run_agent(agent, task, run=sub_run):
        t = ev.get("type")
        if t == "answer":
            answer = ev.get("text", "")
        elif t == "thinking":
            thinking_tail = ev.get("text", "")
        elif t == "tool_call":
            tool_calls += 1
        elif t in ("error", "aborted"):
            answer = answer or f"[子 Agent {t}] {ev.get('text') or ev.get('reason')}"

    return {
        "subagent": agent["name"],
        "conclusion": answer or thinking_tail or "（子 Agent 未产出结论）",
        "tool_calls": tool_calls,
        "tokens": sub_run.tokens_in + sub_run.tokens_out,
    }
```

#### 配套改动

1. `registry.ensure_loaded()` 加上 `agent_tools` 的 import。
2. `AGENT_SPECS` 给 `general` 和 `diagnose` 加 `dispatch_agent`；**不要给 `topology`/`risk`/`diagnose` 之外的类型**（`dispatch_agent` 内部已过滤，双重保险）。
3. `_run_checkup` 可以简化——保留硬编码 3 步作为稳定的 `full_checkup` 路径（演示可靠性），但在 `general` Agent 里让模型能自主派发，两条路径并存。
4. 子 Agent 的进度需要透传到 UI：`dispatch_agent` 目前吞掉了子 Agent 的事件流。若要展示，需把 `run_agent` 的事件通过回调/队列往上冒（可作为二期，一期先只回传结论）。

> **子 Agent 的 prompt 写法是使用成败的关键**：中间过程全部丢弃，所以 `task` 描述里必须明确要求"输出完整结论，包含关键数值与资源名"。工具描述里已经写了这条提示。

**工作量 ≈ 4 小时（不含事件流透传）。**

---

### P1-3 Token 记账 + 预算 + 分模型路由

#### 问题

1. 完全没有 token/成本可见性。
2. [`context.total_chars()`](../backend/app/harness/context.py) **只统计 `content`，漏掉 `tool_calls` 的 `arguments`** —— 大 JSON 参数被算作 0，导致该压缩时没压缩。
3. 所有 Agent 都用 `qwen-max`（[`llm.chat` 默认 `config.LLM_MODEL`](../backend/app/harness/llm.py)），成本偏高且响应慢。

#### 修法

```python
# context.py：把 tool_calls 计入长度
def total_chars(self) -> int:
    n = 0
    for m in self.messages:
        n += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            n += len(fn.get("name") or "") + len(fn.get("arguments") or "")
    return n


def est_tokens(self) -> int:
    """中文场景的 token 估算：中文 ~1 token/字，英文/JSON ~4 字符/token。

    末尾 ×4/3 保守系数 —— 第4课：低估的代价（撞上限，整轮请求白费）
    远大于高估的代价（早压缩一点）。
    """
    total = 0.0
    for m in self.messages:
        s = str(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            s += json.dumps(tc.get("function", {}), ensure_ascii=False)
        cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
        total += cjk + (len(s) - cjk) / 4
    return int(total * 4 / 3)
```

```python
# agents/base.py：AGENT_SPECS 加 model 字段
AGENT_SPECS = {
    "data":     {..., "model": config.LLM_MODEL_FAST},   # 采集汇报，结构化任务
    "topology": {..., "model": config.LLM_MODEL_FAST},   # 拓扑梳理，规则性强
    "risk":     {..., "model": config.LLM_MODEL},        # 治理决策，需要强推理
    "diagnose": {..., "model": config.LLM_MODEL},        # 根因推理，需要强推理
    "general":  {..., "model": config.LLM_MODEL},        # 兜底对话
}

def build_agent(key, user_query=""):
    ...
    return {"name": ..., "system_prompt": ..., "tools": tools,
            "model": spec.get("model") or config.LLM_MODEL}
```

前端展示：SSE 每步推 `run.usage_event()`，界面上显示"本轮已用 12.3k tokens / 预算 120k · ¥0.31 · 8.2s"。

**收益**：粗估节省 50%+ LLM 成本，`data`/`topology` 类任务响应更快；预算护栏防止单轮失控。

**工作量 ≈ 2 小时。**

---

### P1-4 决策审计日志

#### 动机

运维场景的**核心合规需求**：事后必须能回答"这条 `upgrade_rds_instance` 是谁批准的？依据什么？什么时候？结果如何？"。第3课的 `decisionReason.type` 就是为此设计的。

这也是**低成本高回报的差异化亮点**——它把"AI 帮你改配置"从听起来很危险，变成可审计、可追溯、可交接的运维流程。

#### 表结构（`db.py` 第 16 张表）

```sql
CREATE TABLE IF NOT EXISTS agent_audit (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id   VARCHAR(64),
    agent_name   VARCHAR(64),
    tool_name    VARCHAR(64),
    args_json    TEXT,
    audit_repr   VARCHAR(255),   -- 人类可读摘要，来自 ToolSpec.audit_repr
    decision     VARCHAR(16),    -- allow / ask / deny
    reason_type  VARCHAR(32),    -- rule / mode / tool / user / auto
    run_mode     VARCHAR(16),    -- readonly / confirm / auto
    is_destructive TINYINT,
    result_status VARCHAR(16),   -- ok / error / denied / rejected / timeout
    duration_ms  INT,
    created_at   BIGINT,
    INDEX idx_audit_ts (created_at),
    INDEX idx_audit_destructive (is_destructive, created_at)
);
```

> 注意：这张表只在 `backend/app/db.py` 定义，`data_collector` 不涉及（不属于 CLAUDE.md §8.1 说的契约同构 8 张表）。

```python
# harness/audit.py（新建）
"""工具调用审计：记录每一次权限决策与执行结果，供事后追溯。"""
from __future__ import annotations

import json
import logging
import time

from .. import db

logger = logging.getLogger(__name__)


def record(run, agent_name: str, tool_name: str, args: dict, decision,
           spec, summary: str, status: str, duration_ms: int = 0):
    """写审计记录。失败不影响主流程（审计不能反过来搞挂 Agent）。"""
    try:
        db.execute(
            """INSERT INTO agent_audit
               (session_id, agent_name, tool_name, args_json, audit_repr,
                decision, reason_type, run_mode, is_destructive,
                result_status, duration_ms, created_at)
               VALUES (:sid, :ag, :tn, :aj, :ar, :d, :rt, :rm, :de, :rs, :ms, :ts)""",
            {"sid": run.session_id, "ag": agent_name, "tn": tool_name,
             "aj": json.dumps(args, ensure_ascii=False, default=str)[:4000],
             "ar": (summary or tool_name)[:255],
             "d": decision.behavior, "rt": decision.reason_type,
             "rm": run.mode,
             "de": 1 if (spec and spec.is_destructive) else 0,
             "rs": status, "ms": duration_ms, "ts": int(time.time() * 1000)})
    except Exception as e:                        # noqa: BLE001
        logger.warning("审计写入失败（已忽略）: %s", e)
```

```python
# main.py 新增查询接口
@app.get("/api/audit")
def audit_list(limit: int = 100, destructive_only: bool = False,
               session_id: str = None):
    where, params = [], {"l": min(limit, 500)}
    if destructive_only:
        where.append("is_destructive=1")
    if session_id:
        where.append("session_id=:sid")
        params["sid"] = session_id
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = db.fetch_all(
        f"SELECT * FROM agent_audit {clause} ORDER BY created_at DESC LIMIT :l", params)
    return {"count": len(rows), "records": rows}
```

前端在「运维控制台」Tab 加一个「操作审计」区块，或独立 Tab。重点展示：时间 / 摘要 / 决策 / 批准来源 / 结果 / 耗时，破坏性动作标红。

**工作量 ≈ 3 小时（含前端）。**

---

### P1-5 反幻觉的机制化校验

#### 动机

CLAUDE.md §8.9 把"数据类结论必须来自工具真实返回"列为**项目评分要点**，但目前只在 `_BASE_PROMPT` 第 1 条约束。可以做一层机制校验——思路对标第3课的"读后写"检查：**用状态记录来验证模型的声明**。

#### 实现

```python
# context.py 扩展
import re

class ContextManager:
    def __init__(self, system_prompt: str):
        self.messages = [{"role": "system", "content": system_prompt}]
        self.scratchpad: dict = {}
        # 工具真实返回过的事实，用于校验最终答案是否有据
        self.observed: dict[str, set[str]] = {
            "trace_ids": set(), "resources": set()}

    def add_tool_result(self, tool_call_id, name, result):
        self._harvest(result)                     # ← 记录工具真实返回了什么
        self.messages.append({...})

    def _harvest(self, result: str):
        self.observed["trace_ids"] |= set(re.findall(r"\b[0-9a-f]{16,32}\b", result))
        self.observed["resources"] |= set(re.findall(
            r"\b(?:rds-|kvstore-|nginx-|node-)[\w.-]+|\b[a-z][\w-]*-service\b", result))

    def check_grounding(self, answer: str) -> list[str]:
        """检查最终答案里的 traceID / 资源名是否都在工具返回中出现过。

        只做提示不阻断 —— 正则会有误判（如模型合理转述），
        目的是给出「可疑」信号供 UI 标注与人工复核。
        """
        issues = []
        for tid in set(re.findall(r"`([0-9a-f]{16,32})`", answer)):
            if tid not in self.observed["trace_ids"]:
                issues.append(f"traceID `{tid}` 未在任何工具返回中出现")
        for res in set(re.findall(r"\*\*((?:rds-|kvstore-|nginx-|node-)[\w.-]+)\*\*", answer)):
            if res not in self.observed["resources"]:
                issues.append(f"资源名 **{res}** 未在任何工具返回中出现")
        return issues
```

```python
# loop.py：yield answer 前校验
issues = ctx.check_grounding(final_answer)
if issues:
    logger.warning("grounding 可疑项: %s", issues)
    yield {"type": "grounding_warning", "issues": issues}
yield {"type": "answer", "text": final_answer, "scratchpad": ctx.scratchpad}
```

前端在答案旁显示一个"⚠️ N 项待核实"的小标记，点开看明细。

> **定位**：这是**提示信号**而非硬门禁。正则必然有误判（模型可能合理地转述、聚合、或引用长期记忆里的历史结论），所以不阻断、不改写答案，只标注。价值在于"Agent 能自证结论有据"这个能力本身。

**工作量 ≈ 2 小时。**

---

## 6. Part D：P2 改进项（打磨，有余力再做）

| # | 项 | 现状 | 目标 | 对应课次 |
|---|---|---|---|---|
| P2-1 | **Skill 渐进式披露** | [`_load_skill`](../backend/app/agents/base.py) 把整篇 md 拼进系统提示词，技能一多提示词线性膨胀 | frontmatter（`description`/`when_to_use`）进提示词；正文用 `load_skill(name)` 工具按需取；深度内容放 `skills/<name>/references/` 让模型 Read | 第6课 |
| P2-2 | **Agent 定义外移到 markdown** | `AGENT_SPECS` 是 Python dict，加 Agent 要改代码 | `app/agents/*.md` + frontmatter（`tools`/`model`/`max_turns`/`when_to_use`/`permission_mode`），非程序员也能加专家 Agent | 第5/6课 |
| P2-3 | **volatile 内容后置** | [`memory_prompt`](../backend/app/harness/memory.py) 注入在系统提示词**中间**，每次内容都变 → 前缀永不稳定 | 稳定部分（角色/准则/Skill）在前，记忆和 `live_note` 放**最后**，为将来接 prompt cache 铺路 | 第4/5课 |
| P2-4 | **`_run_scripted` 降为测试夹具** | [scheduler.py:106-168](../backend/app/harness/scheduler.py) 用 60 行重复实现了业务流，改工具要改两处 | 移到 `tests/` 当集成测试基线；生产路径只保留"无 LLM 时明确报错 + 引导配 key" | — |
| P2-5 | **多轮对话保留 tool_calls** | [`recent_chat`](../backend/app/harness/memory.py) 只取 `role, content`，丢了表里已存的 `tool_calls_json` | 跨轮追问"刚才那个 trace 再看看"时模型能知道上轮查了什么 | 第2课 |
| P2-6 | **意图识别用结构化输出** | [intent.py](../backend/app/harness/intent.py) 用正则从文本里抠 JSON | 改用 function calling 或 `response_format={"type":"json_object"}`，更稳定 | 第1课 |
| P2-7 | **定时扫描接自主预诊断** | [background.py](../backend/app/harness/background.py) 扫出 new findings 只写表 | 有 P1 新增时自动派一个 `diagnose` 子 Agent 预分析根因，结果推前端。"自主发现并预诊断"是很强的能力演示 | 第5课 |
| P2-8 | **`ensure_loaded` 补 governance_tools** | 只 import 了 4 个工具模块 | 确认 `governance_tools` 不注册为 LLM 工具是刻意设计（是的，见 CLAUDE.md §3.3），加注释说明避免后人误加 | — |

---

## 7. 落地顺序与验收

### 7.1 分步计划

```
┌─ 第 1 步（半天）│ Part A：3 个 Bug
│  · Bug 1 压缩配对修复 + assert_api_invariants 护栏
│  · Bug 2 结论保留 + 收口重试
│  · Bug 3 记忆 scope 配额 + prune + 治理记录合并
│  ↑ 纯修复，零新增依赖，风险最低，先把地基补稳
│
├─ 第 2 步（1 天）│ P0-2 RunContext + abort · P0-3 重试与自愈
│  ↑ RunContext 是后面所有功能的载体（预算/模式/熔断计数都挂在它上面），
│    必须先立起来，否则 P0-1/P1-3 无处安放
│
├─ 第 3 步（1 天）│ P0-1 权限门禁 + P1-4 审计日志
│  ↑ 一起做 —— 审计日志正是权限决策的产物，共享 Decision/ToolSpec 结构。
│    这一步完成后项目定位发生质变
│
├─ 第 4 步（半天）│ P0-4 结果落盘 + read_tool_result · P1-1 只读并行
│  ↑ 运维数据量大，两项收益立刻可感（信息不丢 + 响应变快）
│
├─ 第 5 步（1 天）│ P1-2 dispatch_agent · P1-3 token 记账 + 分模型路由
│  ↑ 能力与成本双优化
│
└─ 第 6 步（弹性）│ P1-5 反幻觉校验 · P2 系列
```

**若只有一天**：做第 1 + 第 2 步（修 bug + 可中断 + 重试）。这三件事决定 Agent"不会崩、不会失控、不会白烧钱"，是所有其他功能的前提。

**若要冲项目亮点**：优先第 3 步（权限三态 + 审计）。演示链路见 P0-1 末尾。

### 7.2 每步的回归验收

按 CLAUDE.md §9 的既有基准，每步完成后必须自查：

**static 模式**（标准答案 `data/data/world_manifest.json`）
- [ ] 采集行数：metrics 3904 / ingress 11939 / trace 9246 / app 395 / slow 50 / events 40 / k8s 35
- [ ] 拓扑 9 边
- [ ] 风险扫描精准 11 条，不多不少
- [ ] 全量治理后 0 open / 11 resolved

**live 模式**（基准 mock `/control/threshold_table`）
- [ ] 稳态 7 条预埋配置缺陷精准命中，数据类规则零误报
- [ ] 注入 `rds_conn_spike` 后 ≤2 个扫描周期内 DB-001/CAP-004/API-001 告警，`resource_ref` 精确到故障实例（双 RDS 不串报）
- [ ] 治理执行 → mock 收到 Action → 指标自然回落 → 窗口滑过后全部 resolved

**本次改动新增的验收项**
- [ ] `HARNESS_STRICT=1` 下跑满 12 步的会话不触发 `assert_api_invariants`
- [ ] `MAX_AGENT_STEPS=3` 时故障定位输出有内容的阶段性结论（非固定 fallback）
- [ ] 连续 15 次治理后 `agent_memory` 各 scope ≤20 条，conclusion 类未被挤空
- [ ] readonly 模式下治理工具被拒绝，且拒绝原因回传给模型（模型会向用户解释而非重试）
- [ ] confirm 模式下弹确认卡片；拒绝/超时（可临时把 `APPROVAL_TIMEOUT_S` 调成 5s 验证）均正确补齐 tool_result，下一轮不报 400
- [ ] 客户端断开后 backend 日志出现"客户端断开，中止会话"，且无后续 LLM 调用日志
- [ ] `query_logs` 拉 500 条日志时，模型收到的是结构预览（含 `_total`）而非截断文本，且能用 `read_tool_result` 取回
- [ ] 一轮多个只读查询在日志中体现为并发（时间戳重叠）
- [ ] `/api/audit?destructive_only=true` 能查到每次治理动作及其 `reason_type`

### 7.3 文件清单

**新增（7 个）**

| 文件 | 职责 | 所属 |
|---|---|---|
| `backend/app/harness/runctx.py` | RunContext + 活跃会话注册表 | P0-2 |
| `backend/app/harness/permissions.py` | 四层权限决策 + 三态模式 | P0-1 |
| `backend/app/harness/approvals.py` | 权限确认的跨线程等待/唤醒 | P0-1 |
| `backend/app/harness/audit.py` | 审计写入 | P1-4 |
| `backend/app/harness/tool_results.py` | 超长结果落盘 + 结构感知预览 | P0-4 |
| `backend/app/tools/agent_tools.py` | `dispatch_agent` 子 Agent 派发 | P1-2 |
| `backend/.gitignore` 追加 `.tool_results/` | — | P0-4 |

**修改（9 个）**

| 文件 | 改动要点 |
|---|---|
| `harness/context.py` | Bug1 安全尾窗 + `assert_api_invariants` + `force_compact` + `total_chars` 修正 + `est_tokens` + grounding 采集 |
| `harness/loop.py` | Bug2 结论保留 + abort 检查点 + `_execute_one` 权限链 + `_partition` 并行 + grounding 校验 |
| `harness/llm.py` | `chat_with_retry` + `ContextTooLong`/`Aborted` + usage 记账 |
| `harness/memory.py` | Bug3 scope 配额 + `prune` |
| `harness/scheduler.py` | 透传 `run` 参数；`_run_scripted` 后续降级为测试夹具（P2-4） |
| `tools/registry.py` | `ToolSpec` dataclass + `tool()` 扩参 + `get_spec()`；`get_schemas`/`execute` 适配属性访问 |
| `tools/data_tools.py` 等 4 个工具模块 | 18 个工具打安全标；新增 `read_tool_result` |
| `tools/remediation_tools.py` | `_record_governance` 改用 `memory.remember` |
| `agents/base.py` | `model` 字段；后续 volatile 内容后置（P2-3） |
| `db.py` | 新增 `agent_audit` 表 |
| `main.py` | `/api/chat` 加 mode 参数 + disconnect 检测；新增 `/api/chat/stop`、`/api/chat/approve`、`/api/audit` |
| `config.py` | 新增 `HARNESS_STRICT`、`APPROVAL_TIMEOUT_S`、`MAX_PARALLEL_TOOLS`、`RUN_MAX_TOKENS`、`RUN_MAX_WALL_S` |

**前端（3 处，非本方案重点但需配套）**
- `hooks/useChat.ts`：处理新事件类型 `permission_request` / `aborted` / `compacted` / `usage` / `model_fallback` / `grounding_warning`
- 新增 `PermissionCard` 组件（确认卡片）+ 「停止」按钮 + 模式切换下拉
- 新增「操作审计」区块（消费 `/api/audit`）

### 7.4 风险与回滚

| 风险 | 缓解 |
|---|---|
| `registry.py` 从 dict 改 dataclass 是破坏性改动 | 一次性改完 `get_schemas`/`execute` 两处访问点；改完先跑 `/api/status` 确认 18 个工具都在 |
| 并行执行可能暴露 db 连接线程安全问题 | `MAX_PARALLEL_TOOLS` 可配为 1 快速降级为串行；先在 SQLite（已 WAL）验证再上 MySQL |
| 权限门禁可能挡住既有演示流程 | 默认 `confirm` 模式；`auto` 模式保留给自动化场景；`_run_scripted` 离线路径不经过权限层 |
| 审计表写入成为热路径 | `audit.record` 内部 try/except 吞异常，绝不因审计失败挂掉 Agent |
| 落盘目录膨胀 | `.tool_results/<session_id>/`，可加定期清理；不进 git |

---

## 8. 与原理教程的对应索引

便于实施时回查设计依据：

| 本方案项 | 原理教程课次 | 核心概念 |
|---|---|---|
| P0-1 权限门禁 | 第 3 课 | 七层纵深防御、fail-closed 默认 ask、decisionReason 可追溯 |
| P0-2 abort | 第 2 课 | abortController、中断时补齐 tool_result 配对 |
| P0-3 重试自愈 | 第 2 课 | 错误分级、扣留机制、熔断计数存在跨迭代 State |
| P0-4 结果落盘 | 第 4 课 | L1 工具结果预算、幂等替换、预览+路径 |
| Bug 1 | 第 4 课 | `adjust_index_to_preserve_api_invariants` |
| Bug 2 | 第 2 课 | max_output_tokens 恢复（注入 nudge 而非报错退出）|
| Bug 3 | 第 4 课 | 缓存与去重记录是两种数据结构 |
| P1-1 并行 | 第 2 课 | `partitionToolCalls` 保序分批 |
| P1-2 子 Agent | 第 5 课 | 上下文隔离、只回传结论、机制层只读、独立预算、防繁殖 |
| P1-3 token 记账 | 第 4 课 | 保守系数 ×4/3、预算护栏 |
| P1-4 审计 | 第 3 课 | `decisionReason.type` |
| P1-5 反幻觉 | 第 3 课 | 用状态记录验证模型声明（读后写检查同思路）|
| P2-1 Skill | 第 6 课 | 渐进式披露三层 |
| P2-2 Agent 定义 | 第 5/6 课 | markdown + frontmatter、`when_to_use` |
| P2-3 内容后置 | 第 4/5 课 | 字节级确定性与 prompt cache |


---

## 9. 评审决策记录

> 评审于方案初稿后进行。以下 5 项决策**覆盖**前文对应段落的原始设计，实施时以本节为准。

### D1｜引入第四类「内部写」标记，`readonly` 只禁治理动作

**背景**：初稿把 `readonly` 定义为"拒绝一切写操作"，但 `run_risk_scan` 会写 `risk_findings`、`build_topology` 会写 `topology_edges`——按初稿语义，**只读巡检模式下连风险扫描都跑不了**，"巡检"这个动作本身失去意义。

**决策**：把写入语义拆成三级。

```python
# ToolSpec 三级写入语义
is_read_only         = True   # 纯查询，不写任何表
writes_business_data = True   # 只写本平台业务表（risk_findings / topology_edges），
                              # 对被管集群零副作用
is_destructive       = True   # 治理动作 / 不可逆操作（改集群、删表、花钱）
```

`decide()` 第 3 层改为：

```python
if mode == "readonly":
    if spec.writes_business_data and not spec.is_destructive:
        return Decision("allow", "mode", "只读巡检模式允许写入本平台业务表")
    return Decision("deny", "mode",
                    "当前为只读巡检模式，禁止执行治理动作与配置变更。"
                    "如需治理请在界面切换到「确认执行」模式。")
```

工具打标调整：

| 工具 | 标记 | 理由 |
|---|---|---|
| `run_risk_scan` | `writes_business_data=True` | 写 `risk_findings`，不碰集群 → readonly 允许 |
| `build_topology` | `writes_business_data=True` | 写 `topology_edges`，不碰集群 → readonly 允许 |
| `create_risk_rule` | **不标** `writes_business_data`，`check_permissions=ask` | 新建规则会改变**后续所有扫描的行为**，属于配置变更而非数据写入 → readonly 拒绝 |
| `ingest_data` | `is_destructive=True` | 内部 DELETE 七表 → readonly 拒绝 |
| 4 个治理工具 | `is_destructive=True` | → readonly 拒绝 |

**副产品**：`readonly` 模式因此成为一个真正有用的模式——"让 Agent 全面巡检但绝不动手"，这恰好是运维交接、值班巡检场景的默认姿势。

### D2｜权限确认超时前推「即将超时」提醒

**背景**：初稿 300s 超时后静默拒绝。演示时讲解超过 5 分钟会被静默打断。

**决策**：`approvals.wait()` 分两段等待。

```python
APPROVAL_TIMEOUT_S     = 300
APPROVAL_WARN_BEFORE_S = 60          # 剩 60s 时提醒

def wait(request_id, timeout=APPROVAL_TIMEOUT_S, on_warn=None) -> dict:
    """先等 timeout-WARN_BEFORE；仍未决策则回调 on_warn 推提醒，再等剩余时间。
    最终超时仍然默认【拒绝】（fail-closed 不变）。"""
    ev = _WAITERS.setdefault(request_id, threading.Event())
    first_leg = max(0.0, timeout - APPROVAL_WARN_BEFORE_S)
    if not ev.wait(first_leg):
        if on_warn:
            try:
                on_warn(int(timeout - first_leg))
            except Exception:                    # noqa: BLE001
                logger.debug("超时提醒回调失败", exc_info=True)
        if not ev.wait(timeout - first_leg):
            _cleanup(request_id)
            return {"approved": False, "remember": False, "timeout": True}
    ...
```

**实现约束**：此刻主线程正阻塞在 `wait()` 里，无法 `yield` 事件。因此 `RunContext` 需要提供一个**旁路推送通道**：

```python
# runctx.py 补充
@dataclass
class RunContext:
    ...
    event_sink: Optional[Callable[[dict], None]] = None   # 由 main.py 注入 q.put

    def push_event(self, ev: dict):
        """旁路推送（不经过 generator）。用于阻塞等待期间的提醒。"""
        if self.event_sink:
            try:
                self.event_sink(ev)
            except Exception:                    # noqa: BLE001
                logger.debug("旁路事件推送失败", exc_info=True)
```

`main.py` 的 worker 里注入：`run.event_sink = q.put`。

新增 SSE 事件类型：`permission_expiring`（`{request_id, tool, seconds_left}`），前端在确认卡片上显示倒计时。`permission_request` 事件同时带上 `timeout_s` 供前端起倒计时。

### D3｜`_run_scripted` 保留，但从「隐式降级」改为「显式离线脚本模式」+ 复用为测试基线

**决策理由**（三方权衡）：

- **不删**：无 API Key 仍可完整演示五条业务流，这是项目的真实能力（评审/离线环境下有价值），删掉是能力倒退。
- **但要显式化**：现在 `llm.available()` 为假就静默走脚本流。API Key 填错的人得到一个"能用但不智能"的系统，且**不知道为什么**——这是最糟的失败模式（静默降级）。
- **顺带白拿一个测试基线**：脚本流本身就是"用真实工具跑通真实业务流"的端到端路径，正好当回归测试用。

**三项改动**：

1. **入口显式声明**。`_run_scripted` 开头改为：
   ```python
   yield {"type": "degraded", "mode": "offline_script",
          "reason": "未配置有效的 DASHSCOPE_API_KEY，已切换到离线脚本模式（固定工具序列，无 LLM 推理）",
          "hint": "配置 backend/.env 的 DASHSCOPE_API_KEY 后重启即可启用完整 Agent 能力"}
   yield {"type": "agent_start", "agent": "ScriptedAgent(离线脚本模式)"}
   ```
   前端在消息上方显示一条黄色横幅，而不是让用户以为这就是 Agent 的正常水平。

2. **文案格式化抽成 helper**。目前工具返回结构一变（如 `open_findings` 字段改名），脚本流会 `KeyError` 但 LLM 路径不会——两条路径的耦合点是"对工具返回结构的假设"。把这些假设集中到 `_fmt_*()` 函数里（`_fmt_ingest` / `_fmt_topology` / `_fmt_risk` / `_fmt_diagnose`），改工具时只需检查这一处。

3. **复用为集成测试**。新增 `backend/tests/test_scripted_flows.py`，对 5 条脚本流断言关键数值（对齐 CLAUDE.md §9 的 static 验收基准）：
   ```python
   def test_scripted_risk_scan_static():
       events = list(scheduler._run_scripted("t", "风险扫描", "risk_scan"))
       answer = next(e for e in events if e["type"] == "answer")["text"]
       assert "11" in answer          # static 基准：精准 11 条
   ```
   这样"改工具要改两处"的成本，变成了"改工具会被测试拦住"的收益。

**不做的事**：不把脚本流移出生产路径（保留离线能力），也不试图让它和 LLM 路径共享实现（两者的控制流本质不同，强行统一会更糟）。

### D4｜`dispatch_agent` 一期即透传子 Agent 事件流

**背景**：初稿一期吞掉子 Agent 中间事件，UI 会出现"派发 → 长时间空白 → 突然出结论"。

**决策**：一期就往上冒。`run_agent` 增加可选的 `event_sink` 参数，子 Agent 的事件加 `agent_id` / `depth` 标记后推给同一个 SSE 通道。

```python
# tools/agent_tools.py
def dispatch_agent(subagent_type: str, task: str, description: str = "",
                   _run=None):                       # 由 registry 注入当前 RunContext
    ...
    sub_id = f"{subagent_type}-{uuid4().hex[:6]}"
    depth = (_run.depth + 1) if _run else 1

    def sink(ev: dict):
        """把子 Agent 事件打标后旁路推给前端。"""
        if _run:
            _run.push_event({**ev, "agent_id": sub_id, "depth": depth,
                             "subagent_type": subagent_type})

    if _run:
        _run.push_event({"type": "subagent_start", "agent_id": sub_id,
                         "subagent_type": subagent_type,
                         "description": description or task[:30], "depth": depth})

    for ev in run_agent(agent, task, run=sub_run, event_sink=sink):
        ...          # 仍然只把 conclusion 作为工具返回值给主 Agent

    if _run:
        _run.push_event({"type": "subagent_done", "agent_id": sub_id,
                         "tool_calls": tool_calls,
                         "tokens": sub_run.tokens_in + sub_run.tokens_out})
```

**关键区分（必须守住）**：

| | 走哪条路 | 进入主 Agent 上下文？ |
|---|---|---|
| 子 Agent 的中间事件（thinking / tool_call / tool_result） | 旁路 `push_event` → SSE → UI | ❌ **不进** |
| 子 Agent 的最终 conclusion | 工具返回值 | ✅ 进 |

**这是本决策的全部要点**：事件流透传是给**人看的**，上下文隔离是给**模型的**。两者不能混——若把子 Agent 的 tool_result 也塞进主 Agent 上下文，第 5 课的隔离价值就归零了。

**RunContext 需要补两个字段**：`depth: int = 0`（嵌套深度，用于 UI 缩进 + 防繁殖）、`event_sink`（D2 已引入，复用）。

**前端**：`ToolTimeline` 按 `depth` 缩进渲染，`agent_id` 分组折叠；`subagent_start` / `subagent_done` 渲染为可折叠的分组头。

### D5｜`agent_audit` 表只定义在 `backend/app/db.py`

**决策**：确认只在 `backend/app/db.py` 定义，**不**加入 `data_collector/collector/db.py`。

**理由**：CLAUDE.md §8.1 要求双定义同步的是「7 张观测表 + `realtime_metrics`」共 8 张——因为这 8 张表由 collector 写、backend 读，存在跨进程的结构契约。`agent_audit` 只由 backend 的 Agent 运行时写入与读取，collector 完全不涉及，**不属于契约同构范围**。

同理，`agent_memory` / `chat_messages` / `scan_reports` / `governance_plans` 也只在 backend 定义——`agent_audit` 与它们同类。

**实施时在 `db.py` 的建表处加一行注释**说明这一点，避免后人误以为漏改了 collector。

---

## 10. 实施记录

### Part A（3 个 Bug）—— 已完成

| Bug | 改动文件 | 核心改动 |
|---|---|---|
| Bug 1 压缩切断配对 | `harness/context.py` | 新增 `_safe_tail_start()` 把切点前推到非 tool 消息；`compact()` 改用它；新增 `assert_api_invariants()` 开发期护栏（`HARNESS_STRICT=1` 启用）；新增 `force_compact()` 备 P0-3 用 |
| Bug 2 步数耗尽丢结论 | `harness/loop.py` | 新增 `last_thinking` 累积；`for...else` 改为注入收口消息 + `tools=None` 强制出文本，失败则回落到 `last_thinking` |
| Bug 3 记忆挤占 | `harness/memory.py`、`tools/remediation_tools.py` | `recall()` 改 scope 配额（conclusion 3 / governance 3 / preference 2）+ 关键词优先；新增 `prune()`；`_record_governance` 改用 `memory.remember()` 的 upsert，key 按资源合并 |

同时修正 `total_chars()` 漏算 `tool_calls` 的问题（属 P1-3 范围，但与 Bug 1 同文件同函数，一并处理）。

### 实施中踩到的坑：把「状态标记」当成「可淘汰缓存」

修 Bug 3 时我先写了一个 `prune()` 对**所有** scope 截断到 20 条，结果引入了一个比原 bug 更严重的回归：

`rules/builtin.py` 的 `_has_governance_prefix()` 在 **static 模式**下靠 `agent_memory` 里
scope=`governance` 的条目判定 finding 是否 `resolved_by_governance`。也就是说——

> **governance 条目不只是"给 LLM 看的记忆"，它同时是承载业务状态的标记。**

prune 删掉旧标记 ⇒ 已治理的风险翻回 `open` ⇒ 直接破坏 CLAUDE.md §9 的
"全量治理后 0 open / 11 resolved" 验收基准。而且这个故障有延迟性：只在
治理动作累计超过 20 次的长会话里才显现，演示时极难定位。

**这正是原理教程第 4 课那条教训的现场复现**：缓存（可淘汰）与状态记录（不可淘汰）
是两种不同的数据结构，混用必出问题（Claude Code 用 `loadedNestedMemoryPaths`
独立于 LRU 的 `readFileState` 做去重，就是为了避开同一个坑）。

**修正**：

```python
# ⚠️ 不可 prune 的 scope
NO_PRUNE_SCOPES = frozenset({"governance"})
```

并厘清了两层的职责边界：

| 层 | 问题 | 解法 |
|---|---|---|
| **recall 层**（注入提示词的名额） | governance 挤掉 conclusion | `SCOPE_QUOTA` 配额 ✅ |
| **持久化层**（库里的行数） | 无限增长 | key 资源级 + upsert 天然有界，**不需要也不应该 prune** ✅ |

原始的"拥挤"问题**完全是 recall 层的问题**，配额已足够解决；持久化层根本不该动。

配套加了 3 条回归护栏用例：`governance` 免于 prune、其他 scope 仍受约束、
upsert 天然有界（100 次治理 / 3 资源 ×2 动作 → 6 条）。

### 测试与验收结果

新增 `backend/tests/test_harness_fixes.py`（零依赖，`python tests/test_harness_fixes.py` 直接运行，
用临时 SQLite 完全隔离，不碰真实库）。

**单元/回归测试：17 项全部通过**

| 分组 | 用例 |
|---|---|
| Bug 1（8 项） | 复现旧切法缺陷、`_safe_tail_start` 前移、基础压缩配对、6 个并行 tool_calls、12 步×3 并行（压缩触发 2 次）、护栏检出孤立消息、`force_compact`、`total_chars` 计入 tool_calls |
| Bug 2（3 项） | 步数耗尽成功收口（13 次 LLM 调用）、收口失败回落 `last_thinking`、正常终态未受影响 |
| Bug 3（6 项） | 治理记录 upsert、recall 配额生效、**governance 免于 prune**、preference 仍受约束、upsert 天然有界、`memory_prompt` 保住诊断结论 |

**Bug 1 真实性验证**（独立脚本对比旧/新切法）

| 单条 assistant 的 tool_calls 数 | 旧切法 `messages[-6:]` | 新切法 `_safe_tail_start()` |
|---|---|---|
| 3 | ✅ 配对完好 | ✅ |
| **6** | ❌ 断裂（孤立 `tool(p0)`）| ✅ |
| **8** | ❌ 断裂（孤立 `tool(p2)`）| ✅ |

确认为**真实的高概率线上崩溃**，不是理论风险。

**端到端回归（static 模式，对照 CLAUDE.md §9）**

| 项 | 结果 |
|---|---|
| 采集行数 | ✅ 7 张表全部命中基准（3904 / 11939 / 9246 / 395 / 50 / 40 / 35）|
| 拓扑边数 | ✅ 9 |
| 风险扫描 | ✅ open=11（P1 6 / P2 5），11 条 finding 逐条核对 |
| 治理闭环 | ✅ 全量治理后 **0 open / 11 resolved** |
| governance 标记 | ✅ 13 条全部保留，未被 prune |
| 离线脚本流 | ✅ 5 条全通（data_ingest / topology / risk_scan / fault_diagnose / full_checkup）|
| `build_agent` | ✅ 5 个 Agent 全部正常组装（含 `memory_prompt` 新配额逻辑）|
| `handle_message` | ✅ 事件序列 `intent → agent_start → tool_call → tool_result → answer → done` |
| `compileall` | ✅ 无语法错误 |
| `HARNESS_STRICT` | ✅ 默认 False，生产路径不受护栏影响 |

**两处待确认的既有行为**（与本次改动无关，实施时发现）：

1. `create_pdb` 的参数名是 `app` 而非 `name`，与其他治理工具（`patch_deployment` 用 `name`）
   不一致。LLM 容易传错 → 建议在 P0-1 打标时统一，或在工具描述里显式强调。
2. **CAP-003（命名空间 CPU 超卖）与 HA-002（单副本）的治理动作相互冲突**：
   `set_replicas` 扩副本会**推高** CPU 超卖率（实测 167.9% → 179.5%），
   必须再下调各 Deployment 的 `cpu_limit` 才能同时消除两条风险。
   这是真实运维里的治理顺序依赖，但目前 Agent 的 Skill 文档
   （`app/skills/risk_governance.md`）未提及。**建议补进 Skill 文档**——
   否则模型只会逐条治理，最后卡在 CAP-003 上反复尝试。这也正好印证了
   CLAUDE.md §8.3「改 Agent 行为优先改 skills/*.md」的做法。

### Part B 第 2 步（P0-2 + P0-3）—— 已完成

| 项 | 改动文件 | 核心改动 |
|---|---|---|
| P0-2 RunContext + abort | 新增 `harness/runctx.py`；改 `loop.py` `scheduler.py` `main.py` `config.py` | `RunContext`（中断 Event / 三态模式 / token+时间预算 / 熔断计数 / `event_sink` / `child()`）+ 活跃会话注册表；loop 两处中断检查点；`main.py` 断开检测 + `finally` 兜底置位；新增 `POST /api/chat/stop`；`/api/chat` 支持 `mode` 参数 |
| P0-3 重试与自愈 | `llm.py`、`loop.py` | `chat_with_retry` 指数退避+抖动、错误三分类（`ContextTooLong` / retryable / fatal）、退避期间可中断、usage 自动记账；loop 侧上下文超长→紧急压缩重试（熔断 1 次）、连续失败→模型降级（只降 1 次） |

**关键设计点**（都来自原理教程第 2 课）：

- 所有熔断计数器（`llm_failures` / `emergency_compacts` / `model_downgraded`）都存在 `RunContext`
  里而非 loop 局部变量 —— 否则跨恢复路径会被重置，护栏失效导致无限重试。
- `main.py` 的 `q.get(timeout=1.0)` + `queue.Empty` 是必需的：原来的阻塞 `q.get()` 会让
  `is_disconnected()` 检查永远得不到执行机会。
- 中断时必须补齐**剩余全部** `tool_call` 的结果（`_fill_remaining`），否则下一轮 API 400。
- `run.abort.set()` 放在 SSE 生成器的 `finally` 里 —— 任何退出路径（断开 / 异常 / 正常结束）
  都置位，这是比显式 `is_disconnected()` 检查更可靠的第二道防线（实测正是它在起作用，见下）。
- `scheduler` 中断时不写 assistant 记忆：半截结论无沉淀价值且会污染后续会话。

**测试结果：20/20 通过**（新增 `backend/tests/test_harness_step2.py`）

| 分组 | 用例 |
|---|---|
| P0-2（9 项） | should_stop 三类触发、非法模式回落、开跑前中断（0 次 LLM 调用）、**工具执行中中断且配对补齐**、token 预算提前停止、usage 事件、注册表+`child()`、`event_sink` 全量透传、`push_event` 吞异常 |
| P0-3（11 项） | 错误分级 6 样例、重试后成功（计数归零+记账）、fatal 不重试、超长不重试而抛出、**退避期间可中断（0.51s 响应）**、超长自愈、**紧急压缩熔断**、模型降级、降级只 1 次、**同名模型时降级空转如实报错**、中断不写记忆 |

**真实后端端到端**（`uvicorn` + 真实 qwen3.8-max）

| 用例 | 结果 |
|---|---|
| `POST /api/chat/stop` 中断多步任务 | ✅ 3.0s 内收到 `aborted`，事件序列 `intent → aborted → done` |
| 客户端主动断开 | ✅ 2s 后查询会话已被清理（`active=[]`），worker 已收到 abort |
| `mode=readonly` 参数 | ✅ 被接受，正常产出答案（705 字符） |
| `usage` 事件 | ✅ `in=23754 out=1235 ¥0.5492 21%` |
| stop 不存在的会话 | ✅ 优雅返回 `{stopped: false, reason: "无活跃会话"}` |
| 后端异常/报错 | ✅ 0 条 |
| 第 1 步 17 项 + 第 2 步 20 项 | ✅ 37/37 全通 |

**实测发现：断开检测走的是 `finally` 兜底而非显式分支**

用例 ② 中日志未出现「客户端断开，中止会话」，说明客户端断连时 Starlette 会直接关闭 SSE
生成器（抛出 `ClientDisconnect`/`anyio` 异常），控制流不经过 `is_disconnected()` 判断就进入
`finally`。功能结果正确（abort 已置位、会话已注销、worker 停止），但那条显式检查在实测中
基本不触发。**结论：`finally` 兜底才是真正起作用的机制，显式检查只是冗余保险**——
这条冗余值得保留（不同 ASGI 服务器行为可能不同），但不能依赖它的日志来判断是否生效。

### 实施中发现的第二个配置问题：模型降级在当前 .env 下是空转的

`backend/.env` 把两个模型设成了同一个：

```
LLM_MODEL=qwen3.8-max
LLM_MODEL_FAST=qwen3.8-max
```

后果有两条：

1. **P0-3 的模型降级逻辑永远不会触发**（`model != config.LLM_MODEL_FAST` 恒为假）。
   代码路径本身是对的（已用显式模型名的用例验证），但在当前配置下等于没有这层保护。
   已补 `test_downgrade_inert_when_models_identical` 把这个行为固化下来，
   避免误以为「已经有降级保护」。
2. **P1-3 的分模型路由也失去意义**：原计划让 `data` / `topology` 走快模型省成本，
   但 FAST 就是 MAX，省不下来。

**建议**：把 `LLM_MODEL_FAST` 改成真正更快更便宜、且与主模型不同可用性域的模型
（如 `qwen-turbo` / `qwen-plus`）。这样降级保护与成本优化才同时成立。
若刻意要求全链路同模型（例如为了输出风格一致），则应在 P1-3 里去掉分模型路由，
并明确记录「不做模型降级」这个取舍，而不是留一段永不执行的代码。

### 待做的配套前端改动（第 2 步引入的新事件）

后端已推送但前端尚未消费的事件类型：

| 事件 | 建议 UI |
|---|---|
| `aborted` | 灰色提示条「已中断：<reason>」|
| `compacted` | 折叠提示「上下文已压缩（释放 N 字符）」|
| `model_fallback` | 黄色警告条「已切换到 <model>」|
| `usage` | 底部状态栏 tokens / ¥ / 预算百分比 |
| `permission_expiring` | （P0-1 时一并做）确认卡片倒计时 |

另需在输入框旁加「停止」按钮（调 `POST /api/chat/stop?session_id=xxx`）与模式切换下拉
（`readonly` / `confirm` / `auto`，随 `/api/chat` 的 `mode` 字段提交）。

### 插曲：A（模型配置）+ B（Skill 治理顺序）—— 已完成

**A｜LLM_MODEL_FAST 修正**

探测 `token-plan` 端点发现它其实支持 11 个模型（不止 `qwen3.8-max`），含明确的快速档。
选定 `qwen3.6-flash`，并**验证过它支持 function calling**（关键：降级路径会带 tools 调用，
不支持的话降级只是换一种失败方式）：

| 模型 | 纯文本 | function calling |
|---|---|---|
| qwen3.6-flash | ✅ 1.3s | ✅ |
| qwen3.7-plus | ✅ 2.3s | ✅ |

改了 `.env` 与 `.env.example`（后者进 git，附「⚠️ 必须与 LLM_MODEL 不同否则是死代码」说明）。
验证：降级判定 `有效=True`；意图识别走快模型，3 个样例分类全对。

**B｜Skill 补治理顺序依赖**

先用**规则自身的口径**量化冲突（第一版脚本口径错了 —— 算了全部 Pod，而 CAP-003 只算
`default` 命名空间，会给模型错数字）：

| 状态 | limit 合计 | 超卖率 |
|---|---|---|
| 初始 | 39300m / 23400m | 167.95% |
| order-service 副本 1→3 后 | 47300m | **202.14%** |

**+8000m / +34.2 个百分点** —— 先治 CAP-003 再扩副本，那 8000m 下调完全白做。

`risk_governance.md` 29 → 75 行，加了：顺序依赖专节（含量化证据与命名空间口径）、
下调幅度算法（引导模型直接读 finding 的 `evidence.sum_pod_cpu_limit_m` /
`sum_node_allocatable_m`，已验证字段真实存在）、两批治理流程、
纠正 `create_pdb(app=...)` 参数名。5 项注入检查全过，RiskAgent 提示词 1925 → 3236 字符。

> 提示词涨 68%。目前 3 个 Skill 共 133 行尚可，再堆内容就该做 P2-1（渐进式披露）。

---

### Part B 第 3 步（P0-1 权限门禁 + P1-4 审计）—— 已完成

| 项 | 文件 | 核心改动 |
|---|---|---|
| 工具安全属性 | `tools/registry.py` 重写；4 个工具模块打标 | `ToolSpec` dataclass（三级写入语义 + 并发/结果预算 + 3 个可选钩子）；`get_spec()` / `describe_tools()`；18 个工具全部打标 |
| 权限决策 | 新增 `harness/permissions.py` | 四层门禁 + 三态模式 + `approval_key` 资源级批准粒度 |
| 阻塞式确认（方案 A） | 新增 `harness/approvals.py` | 两段式等待 + 超时前提醒 + `PERMISSION_AUTO_APPROVE` 逃生阀 + `cancel_all` |
| 审计 | 新增 `harness/audit.py`；`db.py` 加 `agent_audit` 表 | 记录 session/agent/tool/args/摘要/决策/依据层/模式/破坏性/结果/耗时；`query()` + `summary()` |
| 门禁接入 | `harness/loop.py` `_execute_one()` | 四步链路：参数校验（回模型）→ 权限决策（升级用户）→ 执行 → 审计 |
| API | `main.py` | `POST /api/chat/approve`、`GET /api/chat/pending-approvals`、`GET /api/audit`；`/api/status` 暴露安全态与工具安全矩阵 |
| 配置 | `config.py` | `DEFAULT_PERMISSION_MODE` / `APPROVAL_TIMEOUT_S` / `APPROVAL_WARN_BEFORE_S` / `PERMISSION_AUTO_APPROVE` |

**工具安全矩阵实测**（`/api/status` 的 `tool_safety`）：18 个工具 = 只读 10 / 写业务表 2 /
破坏性 5 / 可并行 10。`create_risk_rule` 正确落在「三不沾但需确认」（配置变更）。

**测试结果：23/23 通过**（新增 `backend/tests/test_harness_step3.py`）

| 分组 | 用例 |
|---|---|
| 权限决策（10） | **决策矩阵 10 工具×3 模式=30 组合全对**、auto 白名单语义、Bug 4 回归、未知工具拒绝、拒绝原因可操作、ingest_data live 拒绝、批准粒度、会话内批准、判定函数异常 fail-closed、治理工具打标护栏 |
| 阻塞确认（8） | 批准后执行、拒绝不执行且信息回模型、**超时拒绝+提醒**、remember 免二次确认、readonly 端到端拦截、**readonly 下扫描仍可执行（D1）**、逃生阀可追溯、**中断唤醒挂起确认无线程泄漏** |
| 审计（5） | 11 个字段完整、只读工具也记录、破坏性筛选与概览、**审计失败不搞挂 Agent**、**三条拒绝路径配对不变量** |

**真实后端端到端**（灌好数据后跑，避免空库干扰）

| 场景 | 模式 | 决策 | 依据 | 结果 |
|---|---|---|---|---|
| 扩副本 | `readonly` | **deny** | mode | denied（不弹卡片，直接拦） |
| 风险扫描 | `readonly` | allow | mode | ok（D1 生效） |
| 扩副本 | `confirm` | **allow** | user | **ok**（弹卡片→批准→执行） |
| 创建 PDB | `confirm` | ask | tool | **rejected**（弹卡片→拒绝→未执行） |

审计表精确记录三次破坏性尝试的 `run_mode / decision / reason_type / result_status`。

**全量回归**：三步单测 17+20+23 = **60/60**；static 验收 ✅；治理闭环 0 open / 11 resolved ✅；
离线路径 ✅；`compileall` ✅。

#### 实施中发现并修正的设计缺陷：auto 模式原本是黑名单

初版 `decide()` 的 auto 放行条件写成 `not spec.is_destructive`（黑名单），
测试立刻暴露 `create_risk_rule/auto: 期望 ask 实际 allow`。两个问题：

1. **语义自相矛盾**：`create_risk_rule` 是配置变更（改变后续所有扫描行为），
   readonly 模式已把它和治理动作同等对待（拒绝），auto 模式却按「普通非破坏性写」放行。
2. **对未来新增工具不安全**：黑名单下，只要有人新加工具时忘标 `is_destructive`，
   auto 模式就会自动放行它。

改为**白名单**：`mode == "auto" and spec.writes_business_data and not spec.is_destructive`
—— 只自动放行明确标记「只写本平台业务表」的工具，未分类的一律仍需确认。

> 对应原理教程第 1 课 fail-closed 与第 5 课「监督强度决定权限严格度」：
> auto 模式是无人监督场景，必须用工具白名单而非黑名单。
> 已加 `test_auto_mode_uses_whitelist_not_blacklist` 固化，含「模拟新增工具忘标属性」
> 的用例，确认三种模式下都 fail-closed。

#### Bug 4（既有缺陷）：`sql_query` 查不了 `k8s_resources` / `k8s_events`

端到端演示的日志里出现 `不允许访问表: ['k']`，追查发现表名提取正则是：

```python
tables = set(re.findall(r"\b(?:from|join)\s+([a-z_]+)", stmt, re.I))
```

字符类 `[a-z_]+` **不含数字**，`k8s_resources` 被截成 `k`，再对照白名单必然失败。

**后果**：`sql_query`（Agent 的兜底自由查询工具）**永远无法查询 `k8s_resources` 与
`k8s_events`** —— 恰好是配置类风险最重要的两个数据源。实测中模型连试 3 次全被拒，
只能放弃或绕路，白烧 token 且降低结论质量。

**修复**：字符类改为 `[a-z0-9_]+`，并在错误信息里附上可用表清单（帮模型自我纠正）。
验证：4 条含数字表名/带别名 JOIN 的查询全部放行；3 条越权查询（白名单外、非 SELECT、
`sqlite_master`）仍被正确拒绝 —— 防护未削弱。修复后端到端演示中模型的工具序列明显变短
（不再反复重试 sql_query）。

已加 `test_bug4_sql_query_can_reach_numeric_tables` 回归用例。

#### 逃生阀的使用与风险

```bash
PERMISSION_AUTO_APPROVE=1   # 所有 ask 自动批准，不等待
```

- 用途：前端确认卡片尚未实现期间跑通完整演示。
- 每次自动批准都会打 `logger.warning`，并写审计 `reason_type=auto_approve`，事后可查。
- `/api/status` 的 `permission.auto_approve` 会暴露开关状态，**前端应据此显示醒目警示条**。
- ⚠️ 打开时等于关闭「人工确认」这道防线，正式演示/生产务必关闭。

#### 第 3 步引入的新 SSE 事件（前端待消费，即 C）

| 事件 | 载荷 | 建议 UI |
|---|---|---|
| `permission_request` | `request_id / tool / args / summary / is_destructive / timeout_s` | 确认卡片：摘要 + 参数详情 + 批准/拒绝 + 「本会话都允许」勾选 + 倒计时 |
| `permission_expiring` | `request_id / summary / seconds_left` | 卡片变红提示即将超时 |
| `permission_granted` | `request_id / tool / by` | 卡片折叠为「已批准（by=user/auto_approve）」 |

前端配套还需：模式切换下拉（读 `/api/status` 的 `permission.modes`）、逃生阀警示条、
「操作审计」面板（消费 `/api/audit`）、以及第 2 步遗留的 `aborted` / `usage` /
`compacted` / `model_fallback` 事件与「停止」按钮。

---

### C（前端接入）—— 已完成

一次性把第 2、3 步引入的全部能力接到界面上，避免前端改两遍。

| 项 | 文件 | 内容 |
|---|---|---|
| 类型 | `types/index.ts` | 新增 8 个事件类型、`PermissionMode`、`PendingApproval`、`UsageInfo`、`ToolSafety`、`PermissionStatus`、`AuditRecord/Summary/Data`；`ChatEvent` 补 15 个字段 |
| API | `services/api.ts` | `streamChat` 加 `mode` 参数；新增 `stopChat` / `approveTool` / `fetchAudit` |
| 状态 | `hooks/useChat.ts` | 新增 `pending` / `usage` / `mode` 状态与 `stop()` / `respond()`；`handleEvent` 分发 8 类新事件 |
| 确认卡片 | 新增 `components/PermissionCard/` | 黄色卡片：摘要 + 参数表 + 变更集群红标 + **本地倒计时** + 「本会话不再询问」勾选 + 批准/拒绝；`expiring` 时变红 |
| 审计面板 | 新增 `components/AuditPanel/` | 表格（时间/动作/模式/批准来源/结果/耗时）+ 概览统计 + 「仅看变更类」开关 + 刷新；`reason_type=auto_approve` 标红 |
| 轨迹 | `components/ToolTimeline/` | 渲染 `permission_request` / `permission_granted` / `aborted` / `compacted` / `model_fallback`；工具结果显示耗时 |
| 主界面 | `App.tsx` + `App.module.css` | 三态模式 `Segmented`（带 Tooltip 说明）、发送↔**停止**按钮切换、用量条、逃生阀 `Alert` 警示条、审计面板挂进控制台 Tab |

改动量 `+498 / -63`，新增 2 个组件（各三段式 `index.tsx` + `index.module.css` + `types.ts`）。

**遵循的项目约定**：CSS Modules + `var(--x, fallback)` 语义变量、无 `any`、无 inline style
（原有的 `style={{cursor:'pointer'}}` 也顺手改成了 `.quickTag` 类）、未引第二套 UI 库。

**构建验证**：`tsc --noEmit` 无错误；`npm run build` 成功（chunk 过大警告是既有情况）。

**浏览器实测（7 项全通）**

| 验证 | 结果 |
|---|---|
| 模式切换器三选项 + Tooltip | ✅ 默认「✋ 确认执行」，Tooltip 正常 |
| readonly 下治理被拦 | ✅ 轨迹显示权限拒绝原文，**不弹卡片** |
| **confirm 下弹确认卡片** | ✅ 标题/红标签/倒计时/摘要/参数表/勾选框/双按钮全部就位，倒计时递减；批准后执行成功 |
| 停止按钮 | ✅ 发送按钮变红「停止」，点击后立即中断 |
| 用量显示 | ✅ `1,089 tokens · ¥0.0236 · 预算 1% · 6.6s` |
| 操作审计面板 | ✅ 表格字段与中文映射正确，「仅看变更类」开关生效（3 行 ↔ 5 行） |
| Tab 切换 | ✅ 三 Tab 来回切换正常，**未复现 CLAUDE.md §6 记录的 CSS 坑** |

#### 浏览器实测暴露的两个后端缺陷（已修）

**缺陷 1｜审计归因失真：按「停止」被记成「用户拒绝」**

实测中按下「停止」后，审计里出现 `结果=用户拒绝 / 批准来源=工具自检`。但实际发生的是
"会话中断导致挂起的确认被取消"，不是用户主动拒绝。对面向合规的审计功能，**归因错误
比没有记录更糟**——事后复盘会得出错误结论。

原实现只按 `timeout / rejected` 二分。修复：`approvals.wait()` 返回值加 `cancelled` 字段，
`cancel_session()` 置位它，`loop.py` 三分处理：

```python
if verdict.get("cancelled"):   status, msg = audit.ABORTED,  "会话已中断，操作未执行"
elif verdict.get("timeout"):   status, msg = audit.TIMEOUT,  "确认超时，操作未执行"
else:                          status, msg = audit.REJECTED, "用户拒绝了此操作"
```

**缺陷 2｜`cancel_all()` 是全局的，会误伤其他会话**

真实链路验证时，后端日志出现自相矛盾的一行：

```
手动中止会话 v-abort2  ←→  已取消 1 个待确认请求
```

取消掉的其实是**另一个会话 `v-abort`** 的待确认。根因：`request_id` 用的是 LLM 的
tool_call id（`call_17e1...`），**不含任何会话信息**，所以原先 `cancel_all(prefix)` 的
prefix 参数形同虚设，实际总是全局取消。

后果：多用户/多标签页场景下，A 点「停止」会让 B 正在等待的确认卡片被静默拒绝。

修复：新增 `_OWNERS` 映射记录 `request_id → session_id`；`wait()` 登记归属；
新增 `cancel_session(session_id)` 只取消本会话的；`pending(session_id)` 支持按会话过滤；
`cancel_all()` 保留但注释明确它只用于进程退出等全局场景。`main.py` 的 `/api/chat/stop`
改用 `cancel_session` 并在响应里回传 `cancelled_approvals` 数量。

> 这个 bug 只有跑真实多会话链路才会暴露——单测里每个用例都是单会话，
> 而浏览器验证恰好留下了一个未清理的挂起请求，才把它撞了出来。
> **说明单测覆盖率高不等于没有并发/多实例层面的缺陷。**

两项修复都补了回归用例（`test_abort_records_aborted_not_rejected`、
`test_cancel_is_session_scoped`），step3 从 23 → 25 项。

**修复后浏览器复验（3 项全通，干净数据库）**

| 验证 | 结果 |
|---|---|
| 停止等待中的确认 | ✅ 卡片消失，轨迹「⏹️ 已中断：用户中断」，提示「**会话已中断，操作未执行**」 |
| 审计归因 | ✅ 结果列显示「**已中断**」（不再是"用户拒绝"） |
| 批准后正常执行（回归） | ✅ 两个动作均「用户批准 / 已执行」，耗时 13ms / 4ms |

**当前累计测试**：三步单测 17 + 20 + 25 = **62/62**；static 验收 ✅；
治理闭环 0 open / 11 resolved ✅；离线路径 ✅；`compileall` ✅；前端 `tsc` + `build` ✅。

---

### 逃生阀移除 —— 已完成

前端确认卡片上线后，`PERMISSION_AUTO_APPROVE` 已无用途，且**留着等于给"绕过人工确认"
开了一个改环境变量就能打开的后门**。已从 `config.py` / `approvals.py` / `main.py`
（`/api/status` 的 `permission.auto_approve`）/ 前端 `Alert` 警示条中移除。

两处刻意保留：
- `permissions.REASON_AUTO_APPROVE` 常量与前端 `AuditPanel` 的中文映射 ——
  历史审计记录里已存在 `reason_type=auto_approve`，删掉映射会让旧记录显示成裸英文。
  已在两处加注释标明这是历史值，新记录不再产生。
- 原用例 `test_auto_approve_escape_hatch` 改写为 **`test_no_env_backdoor_bypasses_confirmation`**：
  断言 `config` 上不存在该属性，且**即使设置同名环境变量也必须照常走确认流程**（超时按拒绝）。
  把"没有环境变量后门"这个安全属性固化下来，防止日后被重新引入。

---

## 11. 第 4 步实施记录（P0-4 结果落盘 · P1-1 只读工具并行）

### P0-4 结构感知预览 + 落盘

新增 `harness/tool_results.py`：

| 能力 | 说明 |
|---|---|
| `persist_and_preview` | 超限落盘，返回**结构感知预览 + 取回说明**；未超限原样返回 |
| `_structural_preview` | 数组字段压成 `{"_total": N, "_sample": [前 3 条]}`，**字段结构完整保留** |
| 样本逐级降档 | 样本本身过大时按 3→2→1→0 递减，保证**预览始终是合法 JSON** 而非被切坏的半段 |
| 幂等 | 同 `tool_call_id` 内容确定，重试不重复写盘 |
| `resolve_in_store` | 用 `Path.relative_to` 而非 `startswith` 限制目录（macOS 上 `/var`→`/private/var` 符号链接会让字符串前缀比较误判） |
| `prune_old` / `cleanup` | 过期清理。落盘目录是**缓存**语义，可随意淘汰 —— 与 `memory.py` 里 governance 记录的"状态标记"语义相反 |

配套 `read_tool_result` 工具（**必须有，否则落盘等于丢失**）：分页读取、`has_more` 标记、
文件被清理时引导改用 `sql_query`。`.tool_results/` 已入 `backend/.gitignore`。

**实测效果**：61280 字符的 487 条日志 → **783 字符**，且模型能看到
`_total=487` 与全部 5 个字段名。旧版硬截断只能看到前十几条、JSON 还是断的。

#### 顺带修掉一个"标了却没接线"的缺陷

第 3 步给 18 个工具标注了 `max_result_chars`（`get_k8s_resource`/`run_risk_scan` 8000，
`query_logs`/`query_traces`/`sql_query` 6000…），但 `add_tool_result` 一直用全局
`TOOL_RESULT_MAX_CHARS=2000` —— **标注从未被消费，所有工具实际都被砍到 2000**。

现在 `loop.py` 传 `limit=spec.max_result_chars`。用例 `test_per_tool_limit_is_honored`
的判据设计值得记一下：**不能用内容长度判定**（结构预览会把内容压得远小于任何 limit，
长度反映不出 limit 取了哪个值），改为造一个**大小介于全局默认与工具上限之间**的结果，
用"有没有落盘"来判定。

### P1-1 只读工具并行

`_partition()` 把一轮 `tool_calls` 切成批次，**只有连续的「只读 且 concurrency_safe」
工具才合批**，一旦遇到写类工具就单独成批：

```
[查指标, 查日志, 扩副本, 查拓扑, 查风险]
    → [[查指标, 查日志], [扩副本], [查拓扑, 查风险]]
```

这样保证顺序语义不被打乱（写操作之前的读都已完成、之后的读看到写后状态）。

**关键设计决策：并行只跑工具本身，写操作全部延后到串行阶段。**

`_execute_one` 新增 `deferred` 参数，`_Deferred` 收集"写上下文 + 写审计"两类动作，
由 `_execute_batch` 在串行阶段按原顺序 flush。两个理由：

1. **保序** —— 乱序写回会让相同输入产生不同上下文，问题无法复现；
2. **SQLite 并发写会锁竞争，而审计是合规功能不允许丢记录**。

并行 worker 里只留 `registry.execute`（纯只读查询，并发读安全）。
`ThreadPoolExecutor.map` 按输入顺序返回结果，天然满足保序。

#### 并行改造引入的回归（已修 + 已固化）

中断检查点原本在**每个工具**前，改成**每批**前之后，一批 N 个工具会在中断后全部跑完
（`test_harness_step2` 立刻报「中断后不应继续执行工具，实际执行了 3 个」）。

并行批一旦提交到线程池就无法逐个撤销，所以在 `_execute_one` 的**真正执行前**加了
第三道中断检查点。用例 `test_abort_stops_parallel_batch` 固化：4 个工具的批里
中断后只跑 1 个，其余 3 个标记中断，配对仍完整。

#### 一个不在代码里的瓶颈：模型不发并行调用

代码写好后端到端跑，日志里**一次并行都没有** —— 模型每步只发一个 `tool_call`。
排查两个方向：

1. **不是 API 参数问题**。实测该端点默认就支持并行 tool_calls，不传
   `parallel_tool_calls` 也能一次返回 3 个。
2. **是提示词在反向引导**。`_BASE_PROMPT` 的准则 3 原文是"需要多步才能完成时，
   **逐步调用工具**"—— 直接在教模型串行。

改写为【并行取数】准则（互不依赖的只读数据一次性发起，只有参数依赖才分步），
并新增【超长结果】准则（先看 `_total` 判断规模 → `sql_query` 精查 → 必要时
`read_tool_result`）。改后主 Agent 场景从 0 次并行变成 2 次。

> **能力就位 ≠ 收益到手。** 主 Agent 场景下模型的并行倾向仍然有限（偏好一次 1-2 个），
> 即使明说"这四项互不依赖"。P1-1 的实际收益取决于模型行为，这一点不应夸大。
> 真正的高收益场景出现在子 Agent 里（见下）。

**第 4 步测试**：`test_harness_step4.py` **20/20**。

---

## 12. 第 5 步实施记录（P1-2 dispatch_agent 子 Agent · P1-3 分模型路由）

### P1-2 工具化的子 Agent

新增 `tools/agent_tools.py` 的 `dispatch_agent`，四条**机制性**约束：

| 约束 | 实现 | 为什么不能只靠提示词 |
|---|---|---|
| 只读 | 在代码里过滤掉全部非只读工具 | 提示词只是建议；实测 `risk` 子 Agent 被摘掉 6 个治理工具，否则它能绕过主流程确认直接改集群 |
| 禁繁殖 | 工具集里摘掉 `dispatch_agent` 自身 | 防无限递归 |
| 深度护栏 | `SUBAGENT_MAX_DEPTH=2`，达上限拒绝派发 | 第二道防线，即使过滤被绕过也不会无限嵌套 |
| 独立预算 | `40k token / 120s` | 实测 DiagnoseAgent 单轮用了 **39703 tokens**，逼近上限 —— 这道护栏是必要的，不是摆设 |

**`run` 注入通道**：`dispatch_agent` 需要父 `RunContext` 才能共享中断信号与事件通道，
但它是经 `registry.execute` 调用的。方案是给 `ToolSpec` 加 `needs_run` 标记，
`execute(name, args, run=)` 只对声明了该标记的工具注入 `_run` —— 其余 19 个工具签名不受影响。

### 事件流透传（评审决策 D4）

子 `run` 由 `parent.child()` 派生，继承 `event_sink`、共享 `abort`。子 Agent 的
`thinking` / `tool_call` / `tool_result` 等事件打上 `subagent` / `depth` / `description`
标记后冒到前端；额外新增 `subagent_start` / `subagent_done` 两个事件。

前端 `ToolTimeline` 相应地把带 `subagent` 标记的步骤**缩进一级 + 加归属徽标**，
与主 Agent 的步骤区分开。

**真实端到端实测**（一句话让主 Agent 派两个子 Agent）：

| 项 | 结果 |
|---|---|
| 派发 | 主 Agent 自主派出 `DiagnoseAgent`（order-service 故障诊断）+ `RiskAgent`（全局风险汇总） |
| 子 Agent 工作量 | 11 次 / 15 次工具调用，39703 / 28074 tokens |
| 事件透传 | **62 条**，全部带 `subagent` 归属与 `depth=1` |
| 主上下文占用 | 只有 2 条结论文本，26 次工具调用的中间数据全留在子上下文 |
| 主 Agent 输出 | 2246 字符综合报告 |

#### 意外发现：子 Agent 里模型的并行倾向强得多

同一轮日志里的并行批次：

```
并行执行 4 个只读工具（4 线程）耗时 66ms: [api_perf_stats, query_metrics, query_logs, get_topology]
并行执行 10 个只读工具（4 线程）耗时  7ms: [get_risk_report, sql_query, get_k8s_resource ×4, sql_query ×4]
并行执行 5 个只读工具（4 线程）耗时 22ms: [sql_query ×5]
```

**一批 10 个**，而主 Agent 场景只有 2 个。原因大概是子 Agent 的任务更聚焦、
需要一次性拉齐多份数据再下结论。**P1-1 与 P1-2 是协同增效的** ——
并行的真正价值在子 Agent 里才显现出来。

### P1-3 分模型路由 + token 估算

`AGENT_SPECS` 每个 Agent 显式指定 `model`（用例 `test_all_specs_have_model` 保证
新增 Agent 不会漏配而默默走贵模型）：

| Agent | 模型 | 理由 |
|---|---|---|
| DataAgent / TopologyAgent | `qwen3.6-flash` | 采集汇报、拓扑梳理是结构化任务，规则性强 |
| RiskAgent | `qwen3.8-max` | 治理决策不可逆，需强推理 |
| DiagnoseAgent / OpsAgent | `qwen3.8-max` | 根因推理 / 兜底对话 |

`loop.py` 早已读 `agent.get("model")`，所以只需填 spec；用例
`test_agent_model_actually_used_by_loop` 验证它**真的传到了 LLM 调用**而非只写在配置里。

新增 `ContextManager.est_tokens()`：中文 ~1 token/字、英文与 JSON ~4 字符/token，
末尾乘 **4/3 保守系数** —— 低估的代价（撞上限、整轮请求白费、还要紧急压缩重试）
远大于高估的代价（早压缩一点）。实测 1000 汉字估为 1333 tokens。

### 第 5 步引入的回归（已修）

`registry.execute` 加了 `run=` 参数后，4 个测试文件里 **26 处 stub 签名**
（`lambda n, a: ...`）不接受该关键字，导致 82/82 一度掉到 81 项里 20 项失败。
批量补 `run=None` 后恢复。

> 这是**同一类坑的第二次出现**（第 2 步是 `llm.chat` → `chat_with_retry` 的 stub 漂移）。
> 教训：**改动被广泛 stub 的函数签名时，必须同步扫一遍测试里的替身**。

**第 5 步测试**：`test_harness_step5.py` **19/19**。

---

## 13. 当前总状态

| 项 | 结果 |
|---|---|
| 单元测试 | `fixes` 17 + `step2` 20 + `step3` 25 + `step4` 20 + `step5` 19 = **101/101** |
| static 验收 | ✅ 7 表全中基准、拓扑 9 边、风险 11 条 |
| 治理闭环 | ✅ 0 open / 11 resolved |
| 离线路径 | ✅ 5 条全通 |
| 编译 | ✅ `compileall` |
| 前端 | ✅ `tsc --noEmit` + `npm run build` |
| 工具总数 | 20（新增 `read_tool_result`、`dispatch_agent`） |

**已完成**：Part A 全部 3 个 bug + Bug 4 · P0-1 权限门禁 · P0-4 结果落盘 ·
P1-1 只读并行 · P1-2 子 Agent（含事件透传）· P1-3 分模型路由 · P1-4 审计 ·
第 2 步可中断与自愈 · C 前端全量接入。

**未做**：Part D 的 P2 项（8 项，属于打磨性质）。

---

## 14. 第 6 步实施记录（Part D · P2 打磨项）

8 项 P2 按「收益/成本」排序落地。其中 P2-8 与 P2-4 在前序步骤中已顺带完成，
本步核对确认；P2-1（Skill 渐进式披露）与 P2-2（Agent 定义外移 markdown）暂未做
—— 前者收益随 Skill 数量增长才显现（目前 3 篇），后者工程量大且收益取决于
是否真有非程序员来加 Agent。

### P2-3 volatile 内容后置

原顺序把 `{memory}` 放在 `{skill}` 之后、`live_note` 之前，导致 memory 之后的内容
跟着失去缓存资格。改为**稳定内容全部在前、volatile 收尾**：

```
role + 集群概况 + 工作准则 + skill + live_note   ← 同一 Agent 内固定
memory                                          ← 随 user_query 与库内记忆变化
```

实测同一 Agent 两次不同 query 的**公共前缀 3734 字符，占提示词 94%**
（Skill 文档整篇都在稳定前缀里）。用例断言"记忆之后不允许再有任何内容"。

### P2-5 跨轮工具轨迹

**文档原描述有误**：方案里写"丢了表里已存的 `tool_calls_json`"，核实后发现
`save_chat` 虽支持该参数，但**两个调用点都没传**，表里一直是空数组。所以要做两头：

1. `scheduler` 收集本轮 `tool_call` 事件（上限 `_TRACE_MAX_CALLS=12`，只记名字与参数
   不记结果）随回答一起落库；
2. `recent_chat` 读出来渲染成摘要。

**关键约束：绝不能把 `tool_calls` 结构化还原回 assistant 消息。** 历史表里没存工具
结果，还原结构就会造出"有 tool_calls 但无对应 role=tool 响应"的非法序列，下一轮
API 直接 400（Bug 1 同类）。改为摘要成一行文本：

```
[上一轮我调用过：query_traces(trace_id=tr000123); get_topology()]
```

模型照样能支持"刚才那个 trace 再看看"，而配对不变量分毫不受影响。
用例 `test_history_never_carries_structured_tool_calls` 把这条约束固化。

### P2-6 意图识别结构化输出

先探测端点能力，结论决定实现路径：

| 方案 | 结果 |
|---|---|
| `response_format={"type":"json_object"}` | ✅ 支持 |
| `tool_choice` 强制指定函数 | ❌ `invalid_parameter_error`（端点不支持） |

所以走 `response_format`。但**保留了三层兜底**：json_mode 只保证"是合法 JSON"，
不保证 schema 正确 —— 模型仍可能包一层 `{"result": {...}}`。三层全失败才降级关键词，
意图识别绝不能因模型抽风而整个哑掉。

实体值统一转字符串并丢弃空值，避免 `None`/嵌套结构流到下游拼提示词。

真实实测 6 个场景全部正确路由（含 `remediation` 与 `chat` 的区分），
7 个健壮性用例（多包一层 / 非纯 JSON / 非法 intent / 垃圾输入）全对。

### P2-7 自主预诊断（最有展示价值的一项）

定时扫描发现**新增 P1** 时，自动派一个只读 `diagnose` 子 Agent 预分析根因。

**三道闸门，缺一不可** —— 定时扫描每 `RISK_SCAN_INTERVAL_S` 跑一次，
每个子 Agent 可达 40k token，不设闸门会把预算烧穿：

1. 只处理**新增**且**P1**（P2 多为容量类，不值得逐个诊断）
2. 同一 `finding_key` 只诊断一次（查库去重）
3. 单轮上限 `PREDIAG_MAX_PER_SCAN=2`

结果落**独立的 `prediagnosis` 表**而非复用 `agent_memory` —— `memory_prompt` 会把
recall 到的记忆全部注入系统提示词，长篇诊断报告塞进去会挤爆上下文，
而且大多数轮次并不需要它。新增 `GET /api/prediagnosis` 与前端 `PrediagnosisPanel`
（可折叠 + Markdown 渲染 + 状态标签 + 用量元信息）。

**真实实测**：6 个 P1 里按限流只诊断 2 个；第一个产出完整根因分析
（11 次工具调用 / 34627 tokens / 90s），报告含症状、根因链、因果链总结、治理方案。

#### 实测暴露的两个缺陷（已修）

**缺陷 1｜超时被中断却记成 `ok`**

第二个子 Agent 跑到 157s 超出 120s 预算被中断，结论只有
`[子 Agent aborted] 超出时间预算`，但 `status` 记的是 `ok` —— 因为判据是
`"error" not in out`，而 aborted 路径不产生 `error` 字段。

**残缺的结论被标成成功，比没有结论更危险**：值班的人会以为已经有可用的根因分析。
修法：`dispatch_agent` 显式返回 `status`（`ok`/`error`/`aborted`），
调用方不再靠"文本里有没有 error 字样"猜。

**缺陷 2｜`sql_query` 报错不给列名，模型只能反复猜**

实测日志里模型写了 `AVG(avg_value)`，而真实列是 `avg`/`max`/`min`。
原实现只回 `no such column: avg_value`，模型下一步还是猜
（我自己接着试 `value` 也猜错了）。

现在报错附上**涉及表的真实列名**：

```json
{"error": "SQL 执行失败: no such column: avg_value",
 "table_columns": {"metrics": ["id","namespace","metric_name","dims_json","ts","avg","max","min"]},
 "hint": "请按上面的真实列名重写查询"}
```

列名从 **SQLAlchemy metadata** 读而非 `SELECT` 一行推断 —— 第一版用了后者，
测试立刻在空表上失败：**表为空时恰恰是最需要列名提示的时候**（新库、刚部署、
采集还没跑），靠数据行推断那时正好失效。这是错误信息「可操作性」的价值：
同样一句报错，附上列名模型下一步就能自我纠正。

### P2-8 / P2-4 核对

- **P2-8**：`ensure_loaded` 的注释在第 3 步重写 registry 时已加（说明
  `governance_tools` 刻意不注册为 LLM 工具，只供 `/api/governance/*` 直调）。
  补用例断言注释存在、且它确实不在 `list_tools()` 里。
- **P2-4**：按评审决策 D3，`_run_scripted` 保留为**显式离线降级路径**并复用为
  测试基线，未移入 `tests/`。补用例断言它只在 `llm.available()` 为假时触发。

**第 6 步测试**：`test_harness_step6.py` **21/21**。

---

## 15. 最终状态

| 项 | 结果 |
|---|---|
| 单元测试 | 17 + 20 + 25 + 20 + 19 + 21 = **122/122** |
| static 验收 | ✅ |
| 治理闭环 | ✅ 0 open / 11 resolved |
| 离线路径 | ✅ |
| 编译 / 前端 | ✅ `compileall` · `tsc --noEmit` · `npm run build` |
| 工具总数 | 20 |
| 数据库表 | 17（新增 `prediagnosis`） |

**已完成**：Part A 全部 bug（含 Bug 4）· P0-1 权限门禁 · P0-4 结果落盘 ·
P1-1 只读并行 · P1-2 子 Agent · P1-3 分模型路由 · P1-4 审计 · 第 2 步可中断与自愈 ·
C 前端全量接入 · P2 中的 P2-3/4/5/6/7/8。

**未做**：P2-1（Skill 渐进式披露）· P2-2（Agent 定义外移 markdown）。
两者都属于"随规模增长才有收益"的重构，当前 3 篇 Skill / 5 个 Agent 的体量下
收益不明显，留待需要时再做。

---

## 16. 第 7 步实施记录（P2-1 Skill 渐进式披露 · P2-2 Agent 定义外移）

### 先做了一轮职责地图对齐

用 [awesome-LLM-AIOps](https://github.com/Jun-jie-Huang/awesome-LLM-AIOps) 的任务分类当标尺
对照实际覆盖，找出的缺口不是"Skill 数量少"，而是**有整类能力没有方法论**：

| AIOps 任务域 | 改造前 | 改造后 |
|---|---|---|
| 1.3 根因分析 | ✅ fault_diagnosis | ✅ |
| 1.4 事件缓解 | ⚠️ 一篇混装 HA/CAP/DB/API 四个域 | ✅ 总纲 + 4 篇细则 |
| 1.6 运维问答 | ❌ OpsAgent 零 Skill | ✅ sql_analytics |
| 2.1/2.2 日志分析 | ❌ 有工具无方法论 | ✅ log_analysis |
| 1.2 事件通报 | ❌ 整类缺失 | ✅ incident_report |
| 1.5 复盘 | ❌ 整类缺失 | ✅ postmortem |
| 数据采集 | ❌ DataAgent 零 Skill | ✅ data_ingestion |

**关于外部可复用资源的结论（与预期不同）**：查了 anthropics/skills、awesome-ai-sre、
awesome-LLM-AIOps、RunbookHermes 之后判断 —— Skill **内容**基本无法直接复用，
因为它的价值密度恰恰在于绑定具体工具签名与数据模型（一篇通用 incident response skill
不可能知道 `patch_deployment(action='set_cpu_limit')` 或 `metrics` 表的列叫 `avg` 不叫
`avg_value`）。能复用的是**结构规范**，而权威来源不在网上而在本地 CC 源码：
`CC/src/skills/loadSkillsDir.ts:239-256` 给出真实 frontmatter 字段
（`name`/`description`/`when_to_use`/`allowed-tools`/`model`/`version`），
且 `estimateSkillTokens` 只统计 name+description+whenToUse —— 印证了 L1 的边界。
（官方 spec 与 agentskills.io 分别 404 / 403，抓不到。）

### P2-1 三层披露

新增 `harness/skills.py`：

    L1  frontmatter（name/description/when_to_use）→ 常驻系统提示词
    L2  SKILL.md 正文                              → load_skill(name)
    L3  references/*.md                            → load_skill(name, reference=...)

支持两种布局（单文件 `xxx.md` 与目录 `xxx/SKILL.md` + `references/`），
新增 `load_skill` 工具（第 21 个），`reference` 只接受已登记文件名 —— 天然挡掉路径穿越。

**一个刻意的偏离**：Agent 的**主 Skill 仍注入正文**，而非只给 L1。
理由是 RiskAgent 每轮都要用 risk_governance，若强制先调一次 load_skill
等于给每个治理请求多加一轮 LLM 往返。细节下沉到 references 后主体已很薄，代价很小。

**收益量化（实测）**

| 项 | 数值 |
|---|---|
| Skill 资产总量 | 8 篇，正文 16063 + 细则 7414 = **23477 字符** |
| L1 目录 | **1009 字符（压缩至 4%）** |
| RiskAgent 常驻 Skill 内容 | 主 Skill 2974 + 其余 7 篇目录 840 = 3814 |
| 旧机制若 8 篇全文常驻 | 约 23477 字符 |

> 诚实说明：单看 RiskAgent，提示词从改造前 3831 → 5248 字符是**变大**的
> （多了 Agent 正文与 Skill 目录）。收益体现在**新增的 5 篇 Skill 几乎不占常驻成本**
> ——若按旧机制，8 篇全文会把提示词推到两万字符以上。

### P2-2 Agent 定义外移

`AGENT_SPECS` 从 Python dict 移到 `app/agents/defs/*.md`，frontmatter 沿用 CC 字段命名。
`model` 用 **`primary`/`fast` 语义档位**而非硬编码模型名 —— 换模型只改 config，
不动 7 个定义文件。`AGENT_SPECS` 与 `build_agent` 的名字与结构保持不变，
`dispatch_agent`/`scheduler`/既有测试**零改动**（127 项旧测试全过即为证明）。

新增 `reload_defs()`：改完 md 不必重启进程。

**新增两个专家子 Agent**（判据是"中间数据量大到该隔离上下文"，不是凑数量）：

| Agent | 为什么值得单独建 |
|---|---|
| `CapacityAgent` | CAP 域要跨 `k8s_resources` × `metrics` 做大量聚合，超卖率反算需多轮查询 |
| `DBOpsAgent` | 慢查询分析要拉全量 `slow_logs` 再聚合，典型的"中间数据大、只回传结论" |

刻意**没有**建 `reporter`（通报/复盘）：它只是把已有结论换个格式输出，
没有大量中间数据，派发开销会大于收益 —— 用 Skill + OpsAgent 即可。
两个新 Agent 的工具集**全为只读**（用例强制校验），治理动作仍归 RiskAgent。

### 端到端验证：模型真的会用吗

机制做对但模型不用就等于白做。实测提问「CPU 超卖率超标了，怎么算下调幅度、
为什么放最后治」，模型的**第一个动作**就是：

```
load_skill({"name": "risk_governance", "reference": "capacity_rules"})
```

回答里出现了**只存在于该细则**的 `35100`（目标合计）、`34.2`（实测抬升百分点）、
副本数换算 —— 证明它确实读到并用上了，不是凭记忆编的。

**第 7 步测试**：`test_harness_step7.py` **17/17**，含"frontmatter 完整性"
"L1 必须压缩到 1/8 以下""细则不得泄漏进常驻提示词""定义引用的工具/Skill 必须存在"
"model 档位语义解析""build_agent 契约不变""memory 仍在末尾"等强约束。

---

## 17. 总状态

| 项 | 结果 |
|---|---|
| 单元测试 | backend 144（17+20+25+20+19+21+17+5）+ mock_server 17 + collector 16 = **177/177** |
| Skill | **8 篇**（含 4 篇 references），全部有完整 frontmatter |
| Agent | **7 个**，全部外置 markdown 定义 |
| 工具 | **21 个** |
| 数据库表 | 17 |

**未做**：无。原方案 Part A/B/C/D 全部项已落地。

---

## 18. 第 8 步实施记录（Skill 准确性常驻测试 · Skill/Agent 资产面板）

第 7 步只保证了 Skill「有描述」，没保证「描述是对的」。而 8 篇 Skill + 7 个 Agent 定义里
密集出现着**对代码的引用**：工具名、参数名、枚举值、表名、列名、evidence 字段名、
规则编号、阈值常量。这些引用漂移的症状是模型照旧名字调用，
只在真实对话里才暴露，任何既有测试都不会红。

### 18.1 把「文档正确性」变成机械对账

新增 `tests/test_skill_accuracy.py`（22 项）。每类引用都指定唯一权威来源：

| 引用类型 | 权威来源 |
|---|---|
| 工具名 / 参数名 / 枚举值 | `tools/registry` 注册表 |
| 表名 / 列名 | `db.metadata` |
| 可查询表白名单 | `data_tools._ALLOWED_TABLES` |
| evidence 字段名 | AST 解析 `rules/builtin.py` 的 `"evidence": {...}` 字面量（25 个键） |
| 规则编号 / 阈值 | `BUILTIN_RULE_META` 与阈值常量 |
| 全量采集的表清单 | AST 解析 `ingest/pipeline.run_full_ingest` 里的循环元组 |
| Skill / reference 名 | `skills.discover()` |
| 可派发子 Agent | `agents.base.dispatchable_keys()` |

几个实现上的取舍：

- **阈值断言的期望值必须由常量算出**（`lambda: f"{CPU_OVERSALE_THRESHOLD:.0f}%"`），
  不能硬编码 `"150%"` —— 否则改了常量测试照样绿，等于没测。
- **SQL 示例的列名校验**：允许集 = SQL 关键字/函数 ∪ 涉及表的真实列 ∪ 语句内 `AS` 定义的别名。
  两类需要跳过的语句靠**块内注释标记**识别而非文件名硬编码：`-- ❌`（故意写错的反例，
  `-- ✅` 复位）与 `-- [外部SQL]`（跑在被管 RDS 业务库上，表不在白名单）。
  反例还要反向断言"它仍然是真的错"—— 若代码后来真加了 `avg_value` 列，那段警告就该删掉。
- **兜底对账**：正文里每个反引号标识符都必须能在某个权威来源找到，
  找不到就报错并要求登记进 `_KNOWN_OTHER`（当前 18 个，每条注明来源）。
  前面几条测的是「已知类别」，这条测的是「有没有漏掉的类别」。

**变异测试验证有效性**（机制做了但抓不到就是白做）：人为注入 8 类漂移，全部被抓到 ——
列名改名、阈值常量改动、evidence 字段改名、工具参数名写错、枚举值写错、
新 Agent 忘标 `dispatchable`、SQL 查了白名单外的表、Skill 引用了下线的工具。

> ⚠️ 变异脚本的还原步骤最初写成 `git checkout -- app/`，把同一目录下**所有未提交的正式改动**
> 一并冲掉，7 处修改重做。验证类脚本的还原一律用临时目录备份，不要用 git 兜底。

### 18.2 测试当场抓出的三个真问题

| 问题 | 性质 | 修法 |
|---|---|---|
| `dispatch_agent` 的 `SUBAGENT_TYPES` 硬编码为 `["topology","risk","diagnose"]` | **静默失效**：第 7 步新建的 capacity/dbops 定义、Skill、测试全就位，却永远派不出去 | defs 增加 `dispatchable` 字段（默认 false，fail-closed），枚举与工具描述都由定义文件生成；`reload_defs()` 一并刷新 |
| `realtime_metrics` 写在 Skill 的表清单里，却不在 `sql_query` 白名单 | 能力缺口：live 模式下最新的分实例水位模型查不到 | 加入白名单（11 张），补 `sql_query` 描述与 Skill 的「`metrics` 还是 `realtime_metrics`」选表指引 |
| `postmortem.md` 拿 `DB-003` 当新规则示例 | 与自身约定冲突（总纲规定用户自建规则用 `AI-xxx` 编号），且会让模型以为 DB-003 存在 | 改为 `AI-002` |

另外把 `agents.base` 里 `build_agent` 与 `describe` 各算一遍的工具集提取成
`effective_tools()`（live 摘 `ingest_data`、统一补 `load_skill`），并加用例断言两者相等 ——
否则前端面板会显示模型手里根本没有的工具。

### 18.3 Skill/Agent 资产面板

后端：`skills.describe()` 补 L1/L2/L3 各层字符数，新增 `skills.stats()`；
新增 `GET /api/skill/{name}?reference=`（与 `load_skill` 同一条读取路径，
白名单校验在 skills 模块，路径穿越天然被挡）。

前端 `AssetPanel`（三段式）：

- 标题栏给出**渐进式披露的硬指标**：8 篇 Skill（含 4 篇细则）· 全文 23,953 字符 ·
  常驻目录仅 1,009（**4.2%**）；
- Skill 侧点开可读**模型实际看到的正文与细则**（按需拉取 + 前端缓存），每篇标注 L1/L2/L3 体量；
- Agent 侧显示档位（主/快模型）、主 Skill、是否可派发、工具数与其中变更类数量，
  工具标签按安全属性四色区分：蓝=只读 / 橙=只写业务表 / **金=需确认** / 红=破坏性。
  金色这一档不能和橙色合并 —— `create_risk_rule` 非破坏性但刻意不进 auto 白名单，
  合并显示会让人误以为无人值守时会被自动放行。

浏览器实测 6/6 通过：统计文案、L2/L3 懒加载、`create_risk_rule` 的金色标签与提示、
live 模式下 DataAgent 不显示 `ingest_data`（3 个工具 · 全只读）、无 JS 报错、
`/api/skill/*` 全 200。

另外真机验证了新专家 Agent 现在真的可达：直接派 `capacity` 子 Agent，
2 次工具调用、10,807 token 得出结论，且**用的是 finding 的 evidence 字段**
（`sum_pod_cpu_limit_m=39000` / `sum_node_allocatable_m=23400` / 目标 35100 / 需下调 3900m）——
正是 `capacity_rules` 细则教它的省查询路径。

---

## 19. 总状态

| 项 | 结果 |
|---|---|
| 单元测试 | backend 166（17+20+25+20+19+21+17+5+22）+ mock_server 17 + collector 16 = **199/199** |
| Skill | **8 篇**（含 4 篇细则），frontmatter 完整且内容与代码逐项对账 |
| Agent | **7 个**，外置 markdown 定义，5 个可派发 |
| 工具 | **21 个**，安全属性四类语义 |
| API | 20 个 |
| 数据库表 | 17 |
| 前端面板 | 6 个（实时图表 / 治理 / 预诊断 / 审计 / **资产** / 控制） |

**未做**：无。原方案 Part A/B/C/D 全部落地，并补齐了文档准确性防线与资产可视化。

---

## 20. 第 9 步方案（Part E：机制完整性）

第 8 步收尾时盘出五个仍然缺的机制。它们不是"再加个功能"，而是**当前 Harness 与成熟
coding agent 之间真正的能力差**。本节先把五项的设计与取舍写清楚，
再分批实施（E-1 / E-2 本步落地，E-3 ~ E-5 排后）。

| 编号 | 缺口 | 症状 | 本步 |
|---|---|---|---|
| E-1 | 无 CI | 199 项测试全靠手工跑，写了不跑等于没有 | ✅ 实施 |
| E-2 | 无 Plan / 续跑 | 步数耗尽只能截断，用户得自己重新组织问题 | ✅ 实施 |
| E-3 | 压缩是被动的 | 只有撞上限时的紧急压缩，没有主动分级压缩 | 方案 |
| E-4 | 无 verifier / 反思环 | 结论正确性只靠提示词硬约束 | 方案 |
| E-5 | 并行子 Agent 未实测 | 机制标记齐全，但没有真实用例证明会并行 | 方案 |

### 20.1 E-1 CI：先解决"能不能一条命令跑完"

**载体的现实约束**（先说清，避免做出跑不起来的东西）：

- remote 是内部 GitLab（`gitlab.example.com`），当前**权限未恢复**，
  且是否有可用 shared runner 未知 —— 所以 `.gitlab-ci.yml` 写了也**无法验证**。
- `core.hooksPath` 已被公司的阿里云 AK 泄露扫描钩子占用
  （`~/.aliyunAKScanHook/hooks`，里面已有 `pre-push`）。
  **绝不能改 hooksPath 或往那个目录塞东西** —— 那是全局共享的，
  改了会影响这台机器上所有仓库，而且会破坏 AK 扫描这道安全防线。

所以 CI 分成两层，价值权重完全不同：

1. **`scripts/run_tests.sh`（本步核心，可立即验证）**：一条命令跑完
   backend 9 个测试文件 + mock_server + data_collector + 前端 `tsc --noEmit` + `build`，
   汇总成一张表，任一项失败即非零退出。它是"CI 的执行体"，
   有没有平台都能用，也是 `.gitlab-ci.yml` 唯一要调的东西。
   三个硬要求：
   - **必须带数据库隔离闸门自检**：脚本先确认各测试文件都会连 SQLite，
     再开始跑（§8.6 那次误删线上库的事故，根因就是测试连到了真库）；
   - **不依赖当前 shell 的 cwd 与环境变量**：脚本自己定位仓库根、自己用 `.venv`；
   - **失败要能定位**：输出失败文件名与它的最后 20 行，而不是只报一个非零码。
2. **`.gitlab-ci.yml`（本步提交，标注未验证）**：单 stage 调用上面的脚本。
   权限恢复后第一次跑通再把"未验证"的标注去掉 —— 不要提前声称它可用。

**刻意不做**：pre-push 钩子。理由见上（hooksPath 被占用）。
替代做法是把 `bash scripts/run_tests.sh` 写进 CLAUDE.md 的提交前自查项。

### 20.2 E-2 Plan 与续跑：把"截断"改成"接着做"

**现状的具体问题**：`MAX_AGENT_STEPS=12` 用尽后走 `_wrap_up()` ——
追加一句"停止调工具、给阶段性结论"，然后返回
`> ⚠️ 已达最大执行步数（12），以下为阶段性结论`。
用户要继续只能自己重新描述一遍还差什么。全链路体检这类任务很容易撞到这个上限。

设计成三件互相咬合的事，缺一件都不完整：

#### (a) 显式任务清单：`update_plan` 工具

模型自己维护步骤清单，存在 `RunContext.plan` 上（内存，不落库）。

```python
update_plan(steps=[
  {"title": "圈定病灶接口", "status": "done"},
  {"title": "抓一条问题链路", "status": "in_progress"},
  {"title": "多源收口验证", "status": "pending"},
])
```

安全属性：`is_read_only=True`（对被管系统无任何副作用）、
`concurrency_safe=False`（改共享状态，不该进并行批）、`needs_run=True`（要写 run 并推事件）。
每次更新推 `plan_update` 事件给前端。

为什么值得单独建一个工具、而不是让模型把计划写在 thinking 里：
**计划要能被程序读到**。续跑要判断"还剩什么"，前端要显示进度，
写在自由文本里这两件都做不到。

#### (b) 分段执行 + 交接摘要（handoff）

把 `for step in range(MAX_AGENT_STEPS)` 外面再套一层"段"循环：

```
段 1：12 步 → 步数耗尽 → 生成 handoff → 重置上下文 → 段 2：12 步 → …
```

**handoff 不是 wrap_up。** wrap_up 要的是"给用户看的阶段性结论"，
handoff 要的是"给下一段自己看的交接单"，内容要求不同：
已确认的事实与数值（带出处）、已完成/未完成的步骤、**下一步具体调什么工具查什么**。

`ContextManager.reset_for_continuation(handoff)`：保留 system 消息，
丢掉全部历史轮次，塞入交接摘要作为新的起点。
这顺带把上下文压回了小体量 —— 是 E-3 的一个特例（在明确边界上做压缩，
对应 CC 的 compact boundary 思想）。

#### (c) 续跑的闸门：预算不重置，只重置步数

这是整个设计里最关键的一条约束：

> **步数是"单段"上限，token/墙钟预算是"整轮"上限。续跑重置前者，绝不重置后者。**

因此 `run.should_stop()` 的语义完全不变，续跑天然被 `RUN_MAX_TOKENS=120000` /
`RUN_MAX_WALL_S=300` 兜住，不会因为加了续跑就失去硬上限。

另外两道闸门：
- `RUN_MAX_CONTINUATIONS`（默认 2）—— 最多 3 段，防止在无效循环里反复续跑；
- **必须有"任务未完成"的明确信号才自动续跑**：即 plan 里存在
  非 `done` 的步骤。模型没维护 plan 时**不自动续跑**（不去猜），
  改为在 `answer` 里带上"可继续"的提示，由用户决定。

  为什么不靠"再问一次模型是否完成"来判断：那是一次额外的 LLM 往返，
  而且模型对"你做完了吗"的回答并不比 plan 状态更可靠 —— 用结构化信号，不用自然语言猜测。

#### 验证方式

- 用例层面：伪造一个"永远调工具"的 stub 让步数必然耗尽，断言
  (1) 无 plan 时不续跑、(2) 有未完成 plan 时续跑且段数不超上限、
  (3) 续跑不重置 token 预算、(4) 续跑后上下文体量显著下降、
  (5) 超预算时即使有未完成 plan 也不续跑（预算优先于续跑）。
- 真机层面：给一个必然超 12 步的任务（全链路体检 + 逐条治理），看是否自动进入第 2 段。

### 20.3 E-3 主动分级压缩（方案）

现状只有一档：撞到 `ContextTooLong` 时 `force_compact(keep_tail=4)`，
`max_emergency_compacts=1`，再超就让用户开新会话。这是**被动**的 —— 等报错才动手，
而报错时往往已经浪费了一次完整的 LLM 往返。

拟做三档（对应 CC 的四档阶梯，去掉本项目用不上的那一档）：

| 档 | 触发 | 动作 | 代价 |
|---|---|---|---|
| 微压缩 | `est_tokens` 超软阈值（如 60%） | 只把**最老的超长工具结果**替换成落盘指针（`<persisted-output>` 已有基建） | 零 LLM 调用 |
| 主动 compact | 超 75% | 调一次快模型把最早 N 轮压成结构化摘要（保留数值与资源名） | 一次 fast 模型往返 |
| 紧急压缩 | `ContextTooLong` | 现状逻辑（保留） | 一次浪费的往返 |

关键取舍：**微压缩优先复用 `tool_results` 的落盘基建**，因为数据本来就在磁盘上，
换成指针后模型仍能用 `read_tool_result` 取回 —— 信息不丢失，只是变成按需。
主动 compact 用**快模型**而非主模型：摘要是轻结构化任务，用主模型是浪费。

风险：压缩摘要丢掉数值就等于毁掉结论（本项目所有结论都必须有数值出处）。
所以摘要提示词必须硬要求"逐字保留所有数字、资源名、trace_id"，
并加用例：压缩前后关键数值集合不变。

### 20.4 E-4 verifier / 反思环（方案）

现状：结论正确性靠系统提示词第 1 条"一切结论必须来自工具返回，禁止编造"。
这是**约束**，不是**校验** —— 模型违反了没人发现。

拟做**轻量事实核对**，而不是通用反思（通用反思会翻倍 token 且收益不确定）：

- 在 `answer` 事件产出前，抽取回答里的**数值与资源名**，
  与本轮所有工具结果做**字面比对**，比不上的标记为"未在工具结果中出现"。
- 命中时的处置分两档：
  - 事件层面推 `verify_warning`（前端在回答上方显示黄条），**不阻断**；
  - 若命中数量超阈值，追加一次"请修正未经证实的数值"的自纠正往返（上限 1 次）。

为什么不做 LLM-as-judge：本项目的结论是**可机械核对**的（数字要么出现在工具
结果里，要么没有），字面比对比让另一个模型判断更可靠也更便宜。
局限要写清：模型做了单位换算或聚合（如把 39300m 说成 39.3 核）时会误报，
所以第一版只报警不阻断，并把归一化规则（毫核/核、秒/毫秒、百分比）作为已知待补项。

### 20.5 E-5 并行子 Agent 实测（方案）

`dispatch_agent` 已标 `is_read_only=True, concurrency_safe=True`，
`_partition()` 会把连续的多个 dispatch_agent 合成一批并发跑 ——
**机制齐全但没有任何用例证明它真的并行**。这属于"以为有、实际没验证"的一类风险。

拟补两个用例：
1. **确实并发**：stub 两个子 Agent 各 sleep 1s，断言总耗时 < 1.5s（串行会是 2s+）；
2. **并发下的隔离**：两个子 Agent 各自的 `tokens` 分别记在自己的 sub_run 上，
   父 run 的用量是两者之和；事件里的 `subagent` 标记不串。

再补一条真机验证：同一轮里派 `capacity` + `dbops` 两个专家，
看事件时间线上两条 `subagent_start` 是否几乎同时出现。

**已知的实现风险**（实测前先记下来）：子 Agent 内部会写审计与落盘，
而 `_execute_batch` 的并行分支刻意把写操作延后到串行阶段
（`_Deferred`）——但那只覆盖 loop 自己的写，子 Agent 内部 loop 的写不在此列。
若实测发现 SQLite 写锁竞争，处置方向是给子 Agent 的审计写加重试，
而不是取消并行。

---

## 21. 第 9 步实施记录（E-1 CI · E-2 Plan 与分段续跑）

### 21.1 E-1：CI 执行体落地

新增 `scripts/run_tests.sh`（191 行）与 `.gitlab-ci.yml`。

**跑之前先做数据库隔离静态自检**：会连库的测试文件必须把连接串钉到 sqlite 且带运行时闸门，
不过就 `exit 3` 中止。判定"是否会连库"的依据是**有没有 import db 模块** ——
第一版一律要求，把 `test_mock_server.py`（纯内存世界模拟器，没有数据库概念）也误报了。
自检顺带发现 `test_schema_sync.py` 确实缺闸门：它只读 metadata，但
`collector.config` 在导入期就 `build_db_url()`，默认值指向线上 RDS ——
将来任何人加一行 `init_db()` 就会打到真库。已补上。

`.gitlab-ci.yml` 头部保留了"未经 runner 验证"标注：remote 权限未恢复、runner 可用性未知，
写了也无法验证。**刻意不做 pre-push 钩子** —— `core.hooksPath` 被公司的 AK 扫描钩子占用，
往那里塞东西会影响本机所有仓库并破坏那道安全防线。

**脚本自身的两个缺陷是在验证过程中暴露的**，都值得记：

| 缺陷 | 症状 | 根因 |
|---|---|---|
| `$base（` / `$code）` | `base（...: unbound variable`，报错与真实原因毫无关联 | 紧邻中文全角括号时 bash 把非 ASCII 字节当成变量名的一部分 |
| 测试正常失败被报成 CRASH | 输出自相矛盾：「0 失败」但列出了失败项 | 按退出码优先判定，而套件失败时 `sys.exit(1)`，日志里的失败计数被丢弃 |

第一条是**同类坑的第三次**（前两次是 `start_all.sh` 的 `$FRONTEND_HOST，`）。
顺手扫了全部脚本，又在 `start_all.sh:28`、`stop_all.sh:18/39` 找到 3 处存量隐患
（都在错误分支上，一直没被触发）。现已做成常驻用例
`test_no_bare_var_before_cjk_in_scripts`，不再靠人记得。

第二条修法是**先解析日志、再看退出码**：退出码只用来区分"跑完了但有失败"与"根本没跑起来"。

用变异法验证脚本真的会红（改 `db.py` 的列名 → 退出码 1、失败项与失败用例名都给出）。
**这次还原用的是临时目录 `cp` 备份，不是 `git checkout`** —— 上一步刚因为
`git checkout -- app/` 冲掉 7 处未提交改动，本步又误用一次冲掉 1 处，教训已写进 CLAUDE.md §8.6。

### 21.2 E-2：三件咬合的事

**(a) `update_plan` 工具**（第 22 个）。安全属性 `is_read_only=True` +
`concurrency_safe=False`：对被管系统零副作用，但它写共享状态，进并行批会竞态
（用例直接断言 `_partition()` 不会把它并入批）。输入来自模型，做归一化：
非法 status 归 `pending`、空 title 丢弃、非 dict 丢弃。
没有 `run` 时**明确报错**而不是静默丢弃 —— 静默的后果是模型以为登记成功、
而续跑判断永远拿不到清单。

**(b) 分段执行 + 交接摘要**。`loop.run_agent` 外层套了一层"段"循环，
`ContextManager.reset_for_continuation()` 只留 system + 交接摘要
（实测上下文 10163 → 51 字符；scratchpad 刻意保留，它是跨 Agent 的结构化结论）。

`_handoff()` 与 `_wrap_up()` 是两个不同的东西，混为一谈就会做错：

| | `_wrap_up` | `_handoff` |
|---|---|---|
| 读者 | 用户 | 下一段的自己 |
| 要什么 | 阶段性结论 + 哪些证据不足 | 已确认的数值（逐字保留）+ 下一步调什么工具 |
| 失败时 | 回落到最近的 thinking | **放弃续跑**（空交接单重开一段＝把花掉的 token 再烧一遍） |

交接单末尾会附一份 `[任务清单快照]`——结构化事实不依赖模型有没有在摘要里写全。

**(c) 三道闸门**（`can_continue()`，顺序有意义）：

1. **预算优先**：超 token/墙钟或已中断，绝不续跑。
   > ★ 续跑**只重置步数、绝不重置预算**。整轮硬上限始终是 `RUN_MAX_TOKENS` /
   > `RUN_MAX_WALL_S`，加了续跑不会失去兜底。用例 `test_budget_beats_continuation`
   > 把这条钉死。
2. **次数上限** `RUN_MAX_CONTINUATIONS=2`（最多 3 段）。
3. **必须有结构化的未完成信号**（plan 里有非 `done` 项）。没维护清单就不续跑，
   也不去问模型"你做完了吗"—— 自然语言回答不比结构化状态更可靠。

子 Agent `max_continuations=0` 且不继承 plan：它只做一个自包含子任务。

顺带把 `MAX_AGENT_STEPS` 从硬编码改成可配（与 `RUN_MAX_CONTINUATIONS` 口径一致，
也便于把它调小来压测续跑）。

### 21.3 真机验证暴露的一个设计错误

第一版把 `plan_update` 写在 `update_plan` 工具里用 `run.push_event` 旁路推送。
真机验证时**模型调了 4 次 `update_plan`，脚本收到 0 个事件** ——
旁路要有 `event_sink` 才生效，而离线脚本与子 Agent 常常没有。
HTTP 路径能收到只是因为 `main.py` 恰好注入了。

改为由 `loop` 在工具执行后**比对 plan 快照、走主通道 yield**：有序、无条件到达、
且清单没变化时不重复推（否则时间线会被噪声淹掉）。
用例 `test_plan_update_event_on_main_channel` 刻意**不设 event_sink**，逼出主通道行为。

**教训**：旁路通道只适用于「阻塞等待期间无法 yield」这一种场景（权限确认倒计时）。
其余一律走主通道。已写进 CLAUDE.md §8.2 第 8 条。

### 21.4 端到端验证结果

真机（真实模型 + 真实工具，把 `MAX_AGENT_STEPS` 压到 2~3 触发续跑）：

| 验证项 | 结果 |
|---|---|
| 模型是否主动列清单 | ✅ **第一个动作就是 `update_plan`**（提示词第 7 条起作用） |
| 自动续跑 | ✅ 续跑 2 次共 3 段，每次释放 1459 / 750 字符上下文 |
| 预算未被重置 | ✅ 3 段跑完 tokens 57335 / 预算 48% / 耗时 127s |
| 达上限后的收口文案 | ✅「已用尽执行预算（3 段 × 2 步），任务清单尚余 1 项未完成」 |
| SSE 事件到达前端 | ✅ curl 抓流确认 `plan_update` 紧跟 `update_plan` 的 tool_call |
| 前端渲染 | ✅ DOM 查得 2 个 📋 条目、8 个清单行，`done` 带删除线+浅灰、`in_progress` 蓝色加粗 |

一个插曲：某次真机验证第 3 段后没看到 `answer`，起初以为丢事件；
实际是验证脚本漏打印 `aborted` 分支 + 命中 `RUN_MAX_WALL_S=300` 墙钟 ——
把墙钟放宽到 1200s 后正常。**这本身印证了"预算优先于续跑"是生效的。**

另外模型在第二次更新清单时把第一步标 done 并写入实测值
（"圈定病灶接口：POST /api/orders P99=0.333s"），说明它把清单当工作台在用，不是走形式。

### 21.5 一处被迫的契约变更

`_wrap_up` 的用户可见文案从「已达最大执行步数（12）」改为
「已用尽执行预算（3 段 × 2 步），任务清单尚余 1 项未完成」——
续跑上线后，只说"撞了步数上限"会漏掉"跑了几段、还剩什么"这两个关键信息。
`test_harness_fixes.py` 有两处断言绑定了旧字面，已同步更新并注明原因。
这两个用例顺带覆盖了"无 plan 时不续跑"这条闸门（stub 从不调 `update_plan`）。

---

## 22. 总状态

| 项 | 结果 |
|---|---|
| 单元测试 | backend 189 + mock_server 17 + collector 16 = **222/222**，`bash scripts/run_tests.sh` 一条命令跑完 |
| CI | 执行体已落地并用变异法验证；`.gitlab-ci.yml` 已提交但**未经 runner 验证** |
| Skill | 8 篇（含 4 篇细则），内容与代码逐项对账 |
| Agent | 7 个，外置 markdown 定义，5 个可派发 |
| 工具 | **22 个**（新增 `update_plan`） |
| API | 20 个 |
| 数据库表 | 17 |
| 前端面板 | 6 个；执行轨迹新增任务清单与分段续跑展示 |

**Part E 剩余**：E-3 主动分级压缩、E-4 verifier / 反思环、E-5 并行子 Agent 实测。
方案见 §20.3 ~ §20.5，尚未实施。

---

## 23. 第 10 步实施记录（E-4 结论事实核对）

### 23.1 把范围收窄到"高价值 + 低误报"

§20.4 的原方案是"抽取回答里的数值与资源名，与工具结果做字面比对"。
直接照做会得到一个**噪声制造机**：模型回答里的数字来源多样 ——
单位换算（336500μs → 336.5ms）、聚合（116 万）、四则运算（1162951 / 7 ≈ 166135）、
提示词里的阈值常量、规则编号里的数字、Markdown 序号。
其中**换算与推算恰恰是应该鼓励的行为**，硬报警等于惩罚正确做法。

所以实施时把方案拆成强度不同的两类，只让确凿的那类报警：

| 类 | 对象 | 判据 | 处置 |
|---|---|---|---|
| A1 | 长 hex 标识符（trace_id / span_id） | 不在证据池 | **报警**（模型没理由说出没见过的 hex 串，零歧义） |
| A2 | 连字符资源名 | 不在池 **且与池中某项编辑距离 ≤ 2** | **报警** |
| B | 数值 | 归一化后不在池 | **只统计不报警** |

**A2 的"距离近"条件是这次最关键的设计决定。** 原始想法"不在池里就报"会把
`read-only`、`blue-green`、`fail-closed` 这类连字符词全部报出来；
而加上距离条件后，报的只剩"**近似但不相等**"—— 也就是
`rds-mysql-03`（世界里只有 01/02）这种最危险的一类：看着像真的，实际指向不存在的实例，
照着它去治理会打错目标。

### 23.2 证据池的定义

    evidence pool = 模型在本轮里【实际看到过】的全部文本
                  = system prompt（含集群概况、Skill 全文）+ 用户输入 + 全部工具结果

这个定义让整套逻辑自洽：**模型没看到的东西，它说出来就是编的**。三条推论都落进了代码：

1. **工具结果超限落盘时只吸收预览**，不吸收全文。若吸收全文，模型没看到的数值也会
   被当成"有出处"，核对就失去意义。（用例 `test_pool_absorbs_preview_not_full_result`
   —— 第一版把要藏的数值放在顶层标量字段，结果断言失败：结构感知预览会**完整保留标量**、
   只截数组，所以要藏的东西必须放在数组靠后的元素里。）
2. **模型自己写的文本不入池**。为此给 `add_user` 加了 `evidence=False`：交接摘要、
   收口指令里的任务清单快照都走这个参数。否则模型编的 trace_id 会在下一段被
   "洗白"成有出处的事实，核对彻底失效。
3. **池只存指纹**（标识符集合 + 数值集合，不留原文），所以内存开销极小，
   且续跑重置上下文后不会丢 —— 续跑该丢的是对话历史，不该连"这个 trace_id 是真的"
   这类事实一起丢。

### 23.3 命中后的处置

- 最多追加 **1 次**自纠正往返（`VERIFY_MAX_CORRECTIONS`）。计数存在 `run` 上而非 loop
  局部：续跑会重开内层循环，放局部会被重置、护栏失效（与 `llm_failures`
  同一个理由，见 §10 的教训）。
- **自纠正刻意不给工具**：只允许改写已有结论，不允许再去查一轮 ——
  否则一次核对可能拖出十几次工具调用，成本不可控。真要补证据由用户下一轮决定。
- **预算已尽时直接放弃纠正**：预算优先于一切自愈动作（与 E-2 续跑同一条原则）。
- 纠正后仍可疑才推 `verify_warning`，前端在**回答上方**显示黄条 ——
  它是对下面那段结论可信度的限定，读者必须先看到它再看结论。

### 23.4 误报率实测（这个功能的生死线）

写了手动评估工具 `tests/_eval_verifier.py`（**刻意连真库调真模型** ——
用空 SQLite 跑不出有意义的误报率结论；它以 `_` 开头不叫 `test_*`，
不会被 `run_tests.sh` 收集，也因此不受"测试必须连 sqlite"那条纪律约束）。

真机 4 类回答实测：

| 场景 | 回答长度 | 核对量 | A 类 | B 类未命中 |
|---|---|---|---|---|
| 风险列表（表格，资源名密集） | 726 字符 | 13 资源名 / 3 数值 | 0 | 0 |
| trace 查询（含 32 位 hex ID） | 301 字符 | 1 ID / 2 资源名 / 3 数值 | 0 | 0 |
| mermaid 拓扑（资源名最密集） | 1904 字符 | 11 资源名 / 33 数值 | 0 | 0 |
| 容量核算（数值换算最多） | 1701 字符 | 14 资源名 / 20 数值 | 0 | 0 |

**A 类零误报；B 类 59 个数值全部命中** —— 归一化（毫核/核、秒/毫秒、万、百分比、
四舍五入）覆盖住了模型的实际换算行为，这比预期好。

检出能力用对照实验验证：把真机语料里的 trace_id **改一位**即被抓到。
这些真机语料已固化进 `test_harness_step10.py` 的 `test_real_answer_*` 系列 ——
以后改规则若引入误报，这几条会立刻红。

### 23.5 一个意外发现：误报的实际危害比预想更低

验证前端渲染时需要一个必然触发的告警，于是临时往 `verifier.check` 注入了假的可疑项
（用临时目录备份、验证后还原，**没有用 `git checkout`**）。
模型收到自纠正指令后的回答是：

> 经核对，我上一轮的回答只有一个数字，不包含任何 traceID/spanID，也没有引用任何资源名
> （没有 `deadbeefcafe1234...`，也没有 `rds-mysql-03`）。因此这两项"未经证实的引用"
> 在我的原回答中并不存在，无从删除或改写 —— **若强行补上反而是编造**。

**模型正确地拒绝了错误的纠正要求。** 这说明：
(1) 自纠正指令足够清晰，模型理解它被要求做什么；
(2) 模型不会因为系统说"你编造了"就盲目顺从去改动正确结论。
误报的代价因此主要是一次多余的 LLM 往返，而不是结论劣化 —— 这降低了本功能的下行风险。

### 23.6 已知边界（写清楚，不夸大）

- **B 类不报警**，所以"数值编造"这一类目前只提示、不拦截。
  约等于匹配（`116 万` vs `1162951`）没做，属已知缺口。
- A2 依赖"池里有正确的资源名"。若模型完全凭空造一个与任何真实资源都不相似的名字
  （如 `foo-bar-svc`），距离条件会让它逃过检出 —— 这是为压低误报付出的代价。
- 核对只看**字面**，不理解语义。模型把两个真实数值张冠李戴
  （把 A 服务的 P99 说成 B 服务的）无法被发现。这类问题需要的是结构化归因，
  不在本步范围内。

---

## 24. 总状态

| 项 | 结果 |
|---|---|
| 单元测试 | backend 209 + mock_server 17 + collector 16 = **242/242**，`bash scripts/run_tests.sh` 一条命令跑完 |
| CI | 执行体已落地并变异法验证；`.gitlab-ci.yml` 仍**未经 runner 验证** |
| harness 模块 | **16 个**（新增 `verifier.py`） |
| Skill | 8 篇（含 4 篇细则），内容与代码逐项对账 |
| Agent | 7 个，外置 markdown 定义，5 个可派发 |
| 工具 | 22 个 |
| API | 20 个 |
| 数据库表 | 17 |
| 前端 | 6 个面板；执行轨迹含任务清单/分段续跑/事实核对，回答上方有核对告警条 |

**Part E 剩余**：E-3 主动分级压缩（§20.3）、E-5 并行子 Agent 实测（§20.5）。

---

## 25. 第 11 步实施记录（E-5 并行子 Agent 实测）

`dispatch_agent` 早就标了 `is_read_only + concurrency_safe`，`_partition()` 也会把
连续的多个合成一批并发跑 —— **但在此之前没有任何用例或实测证明它真的并行**。
属于"以为有、实际没验证"的一类风险。本步把它变成事实，并因此抓出 3 个真缺陷。

用例刻意走**完整真实路径**（主 Agent → `_partition` → ThreadPoolExecutor →
`dispatch_agent` → `build_agent` → 子 `run_agent`），只把最外层 `llm.chat_with_retry`
换成受控替身（子 Agent 那次 sleep 0.6s），**不 stub `registry.execute`** ——
那样就绕过了被测对象本身。

### 25.1 并行确证

| 断言 | 结果 |
|---|---|
| 两个 `dispatch_agent` 合成一批 | ✅ `_partition` 返回 1 批 2 个 |
| 真并发（非串行） | ✅ 峰值并发 2，总耗时 **0.61s**（串行需 1.2s） |
| 并发度受上限约束 | ✅ 6 个子 Agent 峰值并发压在 4（`MAX_PARALLEL_TOOLS`） |
| 各子 Agent 拿到自己的提示词 | ✅ 两份提示词互不相同 |
| 透传事件归属不串 | ✅ 10 条事件，2 组 `subagent`/`description` 配对正确 |
| 并发写审计不丢 | ✅ 子 Agent 2 条 + 父派发 2 条（SQLite 上未出现锁竞争） |

真机（general Agent 派 capacity + dbops）：两个 `dispatch_agent` 的 tool_call 时间戳
**231.2s / 231.3s**（相隔 0.1s），两个子 Agent 的 `sql_query` 在时间线上交错出现，
归属标记正确 —— 并行是真的。

> §20.5 预判的"SQLite 写锁竞争"**没有发生**。原因：子 loop 的审计写虽然确实并发
> （父的 `_Deferred` 只覆盖父自己的写），但单次写入极小、且当前跑在 MySQL 上；
> SQLite 降级路径下也因 WAL + busy_timeout 扛住了。这条风险按"已评估、未复现"结案。

### 25.2 缺陷一：子 Agent 的 token 消耗不计入父预算（★ 最严重）

`child()` 给的是**独立预算**，但实现上"独立"变成了"免费"：
子 run 的用量记在自己身上，**从不回加到父**。后果有两层：

1. **`RUN_MAX_TOKENS=120000` 这条整轮硬上限被绕过**：主 Agent 并行派 3 个子 Agent
   （每个上限 40000）就能额外烧 120000，而父的计数器还停在自己那点。
   E-2 里反复强调的"预算是整轮硬上限"在这里不成立。
2. **前端用量条严重低估真实消耗**：真机实测父自身 52233 tokens、
   子 Agent 合计 126016 —— 不归集就只显示 52233（预算 13%），实际烧了 178249（45%）。

修法：`dispatch_agent` 末尾把 `sub_run` 的用量 `add_usage` 回父。
**正确语义是把两件事分开：上限独立（防单个子任务跑飞），消耗归集（保整轮封顶）。**

这一改动**与第 5 步的旧用例语义冲突** —— `test_subagent_has_independent_budget`
当时断言 `parent.tokens_in == 0`。我判断新语义对（理由如上），
把用例改名为 `test_subagent_has_independent_cap_but_charges_parent` 并在 docstring 里
写清这是刻意的契约变更与依据，而不是悄悄改掉断言。

墙钟维度天生没这个洞：父的 `started_at` 不变，子跑多久父的 elapsed 就涨多少。

### 25.3 缺陷二：子 Agent 空手而归后父会原样重试

真机实测：`DBOpsAgent` 因 `slow_logs` 是空表而反复换写法试探，
20+ 次 `sql_query`、3 次重复 `load_skill`，烧穿 40000 子预算被中断（42058 tokens）；
主 Agent 看到 `status=aborted` 后**原样重派了一次**，又烧掉 40435。

同样的输入不会有不同的结果，而子的消耗已归集到父 —— 重试等于双倍消耗总预算。
修法：非 ok 收场时返回 `retry: false` + 明确劝阻文案（告诉它改为自己查关键那一两项，
或如实汇报缺口）。成功时不带这些字段，避免占用模型注意力。

### 25.4 缺陷三：面对空数据不收手

上一条的根因不在机制而在行为：**模型把"0 行"当成"我的查询写错了"**，于是不断换写法。

先核实了数据事实（不猜）：`slow_logs` 稳态确实是 0 行，
注入 `slow_query_storm` 后 mock 侧立刻产出 19 条、`rows_examined` 126 万 / `rows_sent` 0~6
—— 所以"慢查询只在故障态产生"是事实，稳态空表**不是采集 bug**（DB-002 也不在
"稳态 7 条预埋缺陷"里）。验证完即恢复故障。

机制层面拦不住这种行为，只能在提示词里讲清。两处改动：
- `_BASE_PROMPT` 新增工作准则第 8 条：「空结果本身就是结论」——
  0 行是有效信息不是失败，确认为空就如实汇报并结束，不要反复换写法试探同一件事；
- `dbops.md` 加针对性约束：先 `COUNT(*)` 确认，为 0 直接汇报并转而给出
  RDS 连接/内存水位作为替代信息。

### 25.5 关于"旁路 vs 主通道"的一个例外

E-4 得出的规则是"能 yield 的一律走主通道"。**子 Agent 事件是唯一例外**：
它们产生在 ThreadPoolExecutor 的 worker 线程里，而主 generator 阻塞在 `ex.map()` 上，
跨线程没法 yield。所以只能走 `push_event`。

第一版用例因此全军覆没（`subagent_start`/`done`/透传事件一个都没断言到），
一开始我以为是缺陷 —— 实际是测试方法错了：必须注入 `event_sink` 并把两个来源合并。
代价是真实的：**任何不注入 `event_sink` 的调用方（离线脚本）完全看不到子 Agent 的动静。**
已把这个例外写进 CLAUDE.md §3.3 与 §8.2 第 8 条。

### 25.6 一条刻意不写严的断言

`test_abort_propagates_to_parallel_subagents` 只断言"**至少一个**子 Agent 非 ok
且整轮 aborted"，不断言"全部非 ok"。因为中断是协作式的（检查点在 loop 每步开头），
信号 set 的那一瞬间已经进入执行的那一步不会被打断 ——
实测就出现过 `['aborted', 'ok']`。写成必须全部 aborted 会得到一个 flaky 用例，
**而 flaky 测试是负资产**。连跑 3 次确认稳定。

---

## 26. 总状态

| 项 | 结果 |
|---|---|
| 单元测试 | backend 223 + mock_server 17 + collector 16 = **256/256** |
| CI | 执行体已落地并变异法验证；`.gitlab-ci.yml` 仍**未经 runner 验证** |
| harness 模块 | 16 个 |
| Skill | 8 篇（含 4 篇细则） |
| Agent | 7 个，5 个可派发，**并行派发已实测确证** |
| 工具 | 22 个 |
| 数据库表 | 17 |

**Part E 剩余**：E-3 主动分级压缩（§20.3）。

---

## 27. 第 12 步实施记录（E-3 主动分级压缩）

### 27.1 先修正对现状的判断

§20.3 写的是"现状只有一档：撞到 `ContextTooLong` 时紧急压缩……这是**被动**的"。
读代码后发现**这个前提不准确**：`compact()` 早就在每步开头按
`total_chars() > CONTEXT_MAX_CHARS` 主动触发了，紧急压缩只是撞墙后的兜底。
另外方案里"主动 compact 用快模型而非主模型"这条**已经是现状** ——
`llm.chat_text` 的默认就是 `LLM_MODEL_FAST`。

所以真实增量只剩三条（都基于代码而非假设）：

| 增量 | 现状问题 |
|---|---|
| L1 微压缩（零 LLM 档） | 一超阈值就直接花一次 LLM 摘要，而中段往往就是几条大工具结果 |
| 数值保真兜底 | 摘要保真只靠一句提示词，无任何校验 |
| 触发看 token 维度 | `est_tokens()` 早写好却没用于触发，只看字符数对中英混合偏差大 |

### 27.2 三级阶梯

压力 = **字符占比与 token 占比的较大者**。同样 24000 字符，全中文约 32000 token、
全 JSON 只有 8000 —— 只看字符会让中文为主的会话压缩得太晚。

| 档 | 触发 | 动作 | LLM 成本 |
|---|---|---|---|
| L1 微压缩 | 压力 > 0.6 | 最老的超长工具结果 → 落盘指针 | **0** |
| L2 中段摘要 | 微压缩后仍超字符窗口 | 前序对话压成摘要（快模型） | 1 次 fast |
| L3 紧急压缩 | `ContextTooLong` | 现状逻辑（保留） | 1 次浪费的往返 |

L1 复用 `tool_results.persist_and_preview` —— 数据本来就在磁盘上，换成
「结构感知预览 + 取回路径」既不花钱也**不丢信息**（模型仍可 `read_tool_result` 取回）。
用例直接验证了这一点：压完之后从预览里抠出落盘路径，`read_tool_result` 能读回
藏在靠后行里的 marker。

三条实现细节都写成了用例：从最老的开始压（尾窗是模型当下在用的）、
压到压力回落即停（不过度压缩）、已是指针形态的不重复压（否则会预览套预览）。

`compact()` 改为返回动作描述，由 loop `emit` 成 `compacted` 事件（带 `level`）——
**此前主动压缩对用户完全不可见**，只有紧急压缩有事件。

### 27.3 E-3 的用例反过来暴露了 E-4 的两个实现缺陷

写"数值保真"用例时发现 `verifier.extract_numbers` 几乎什么都提不到：

| 缺陷 | 表现 | 根因 |
|---|---|---|
| 带单位后缀的数值提取不到 | `1.244s` 只提到 `1`；`39300m` 完全提不到 | 正则后置断言 `(?![\w])` 拒绝字母，而本项目数值几乎都带单位（s/ms/m/Mi） |
| 小数指标被整体过滤 | P99 `1.244`、错误率 `1.78` 全被丢弃 | 门槛写成绝对值 `>= 11`，而最关键的度量值恰恰是小数 |

**这意味着 §23.4 里"59 个数值全部命中"是在漏提取的前提下得到的**，
说服力被高估了。修法：
- 后置断言改为 `(?![\d.])`（只防切断数字，允许单位后缀）；
- 门槛改为 `_is_informative()`：**有小数部分的一律算**，纯整数才要求 ≥ 11。

修完**重跑了 E-4 的真机误报评估**（诚实性要求：改了提取逻辑就必须重测）：
核对量从 53 涨到 90（+70%），**仍然全部命中**，A 类仍零误报。
结论比修之前更强，因为这次是在提取完整的前提下得到的。

### 27.4 缺陷三：格式化把"保真"给毁了

保真补录第一版用 `f"{v:g}"` 格式化数值，把 `1162951` 写成 **`1.16295e+06`**：
- 模型对不上工具结果里的原始数字；
- **精度真的丢了**（1.16295e6 = 1162950，差了 1）。

一个"保真补录"用会改写数值的格式化函数，等于自相矛盾。
改为 `_fmt_number()`：整数按整数写、小数保留原样，并加用例断言
"补录里不得出现科学计数法、四个数值逐字保留"。

### 27.5 三处测试构造错误（我自己的）

值得记下来，因为它们都是"断言写得比事实更严"的同类问题：

1. marker 断言用 `str(1000.0)` = `"1000.0"` 去匹配 JSON 里的 `1000` —— 形态不符；
2. 把"幂等"断言成"第二次释放 0 字符"，实际压力仍高时第二次会继续压**后面没压过的**
   条目，那是正确行为。真正的不变量是"同一条不会被压两次"；
3. 事件用例想用大工具结果顶起压力，但工具结果会被 `add_tool_result` 按
   `max_result_chars` 自动落盘，反而顶不起来 —— 改用大 system prompt。

---

## 28. 总状态

| 项 | 结果 |
|---|---|
| 单元测试 | backend 242 + mock_server 17 + collector 16 = **275/275** |
| CI | 执行体已落地并变异法验证；`.gitlab-ci.yml` 仍**未经 runner 验证** |
| harness 模块 | 16 个 |
| Skill | 8 篇（含 4 篇细则） |
| Agent | 7 个，5 个可派发，并行派发已实测确证 |
| 工具 | 22 个 |
| 数据库表 | 17 |

**Part E 全部完成**（E-1 CI / E-2 Plan 与续跑 / E-3 压缩阶梯 / E-4 事实核对 / E-5 并行实测）。

### 遗留的已知缺口（不夸大，写清边界）

- **子 Agent 超预算时空手而归**：`aborted` 路径直接 return，没有像主 Agent 那样的
  wrap_up，父只能拿到一句"[子 Agent aborted]"。已用"劝阻重试"缓解，
  但根治需要给子 loop 也加一次收口调用。
- **事实核对不理解语义**：把 A 服务的 P99 说成 B 服务的（两个数都真）抓不到。
- **B 类数值只提示不拦截**，约等于匹配（`116 万` vs `1162951`）未做。
- **`.gitlab-ci.yml` 未经真实 pipeline 验证**（权限未恢复）。

---

## 29. 第 13 步实施记录（补齐遗留缺口）

§28 列了四条遗留缺口。这一步逐条判断能不能补、值不值得补，**不为了清单好看而硬补**。

| 缺口 | 处理 | 理由 |
|---|---|---|
| ① 子 Agent 空手而归 | **已补** | 唯一带实际浪费的（真机白烧 40435），成本低 |
| ③前半 量级约等匹配 | **已补** | 中文"116 万"是高频表述，字面永远对不上，误报可控 |
| ③后半 B 类改拦截 | **不做** | E-4 已论证：换算/推算是该鼓励的行为，硬拦截必然高误报 |
| ② 语义张冠李戴 | **保留为边界** | 需结构化归因，是另一个量级；勉强做只会得到高误报 |
| ④ CI runner 验证 | **保留为边界** | 受权限限制无法验证，不是代码问题 |

### 29.1 缺口①：中断/超预算带回部分结论

原来三处 `aborted` 路径都是 `yield {"type":"aborted"}` 直接 return，
父 Agent 只拿到一句「[子 Agent aborted] 超出 token 预算」，什么都用不上 ——
E-5 实测过它的直接后果：父倾向于原样重派一次，白烧 40435 token。

修法是新增 `_abort_events()` 统一收口，三处中断路径都改走它：
- 用 `last_thinking` + `scratchpad` 拼一份**标注为"不是完整结论"**的部分结论；
- **零 LLM 调用** —— 这条路径的触发原因往往就是预算耗尽，再花一次往返自相矛盾。
  这些信息是已经产生并付过费的，缺陷在于把它们丢了，不是没去生成新的；
- 事件顺序**先 answer 后 aborted**：前端 `useChat` 与 `dispatch_agent` 都是
  "answer 优先、aborted 兜底"，先发 answer 才能让已积累信息不被"已中断"盖掉；
- **一无所获时不发空 answer**，否则会盖掉"已中断"这个唯一有用的信息。

前端无需改动：`useChat` 里 `aborted` 只在 `last.text` 为空时才填占位文案，
partial answer 先到就自然显示了 —— 这正是刻意让 answer 先于 aborted 的原因。

用例（step11 +3）：aborted 子 Agent 回传含 39000/23400 的部分结论并标注不完整、
收口零 LLM 调用且 answer 在 aborted 前、一无所获时只发 aborted。

### 29.2 缺口③前半：量级约等匹配

`_num_matched` 加一档 `_approx_matched`：模型说"116 万"、池里是 `1162951` 时算命中。
**只对万/亿量级启用、相对误差 < 1%**。容差刻意收窄 —— 放开就会让任意两个数
互相"匹配"，B 类命中率变成没有意义的数字。用例把边界钉死：
117 万（差 0.6%）命中、118 万（差 1.5%）与 200 万（差 72%）不命中。

### 29.3 顺带把 §27.3 那两个 verifier 缺陷补上正式用例

E-3 期间修了"带单位提取不到"和"小数被过滤"两个缺陷，但当时只靠 `_eval_verifier`
手动脚本验证。这次补成常驻用例（step10 +2）：带单位的 3 个数值全部提取命中、
三个小数指标全部参与核对。加上量级约等 2 条，step10 从 20 → 24。

---

## 30. 总状态

| 项 | 结果 |
|---|---|
| 单元测试 | backend 315 + mock_server 19 + collector 16 = **350/350** |
| CI | 执行体已落地并变异法验证；`.gitlab-ci.yml` 仍**未经 runner 验证**（缺口④，权限限制） |
| harness 模块 | 16 个 |
| Skill | 8 篇（含 4 篇细则） |
| Agent | 8 个，5 个可派发（新增顶层编排 OrchestratorAgent，`AGENT_ROUTING=model` 默认） |
| 工具 | 22 个 |
| 数据库表 | 17 |

**Part E 全部完成，可补的遗留缺口已补。** 明确保留为设计边界的两条：
事实核对不理解语义（缺口②，需结构化归因）、CI runner 未验证（缺口④，权限限制）。

> 本文的演进记录写到第 13 步为止。此后的进展未在本文展开，见
> [量化评测报告-最终版.md](量化评测报告-最终版.md)：模型自主编排经扩样本对比
> （6/6 vs 5/6）定为默认调度、新增朴素基线对照（`HARNESS_PROFILE=naive`）与
> L3 四维度复杂度压力评测、集群两次扩容（19 → 51 → 603 实例）后的规模回归验证，
> 以及 C 类时效核对升级为「过期自动重查复核」。
