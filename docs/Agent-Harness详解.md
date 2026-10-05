# Agent Harness 详解 —— 自研意图识别 + 调度编排 + ReAct Loop + 上下文管理 + 短/长期记忆

> ⚠️ **版本说明**：本文记录的是 Harness **第一代**的五大基础机制（意图识别 / 调度编排 /
> ReAct Loop / 上下文管理 / 短长期记忆），文中的 18 工具 / 3 Skill / 5 Agent 是**那一代的真实快照**。
> 此后又陆续落地了六大生产级机制：**三态权限门禁与审计、并行子 Agent、结论事实核对、
> 分级上下文压缩、任务清单与分段续跑**（当前：8 Skill / 22 工具 / 8 Agent，其中 5 个可派发）。
> 这些新机制的权威出处是 CLAUDE.md（未随仓库公开） §3 与 [harness-改进方案.md](harness-改进方案.md)，
> 本文不再重复。
>
> 面向团队成员的架构讲解文档。所有内容基于 `backend/app/harness/` 与 `backend/app/agents/` 的实际代码，
> 文中如实标注了实现上的简化与取舍（评委追问时坦诚说明比掩饰更有说服力）。
>
> 相关源码：
> - Harness 核心：`backend/app/harness/{intent,scheduler,loop,context,memory,llm,background}.py`
> - Agent 定义：`backend/app/agents/base.py`
> - 工具注册：`backend/app/tools/`（registry + 四组工具）
> - Skill 文档：`backend/app/skills/*.md`

---

## 目录

1. [总览：一条消息的生命周期](#1-总览一条消息的生命周期)
2. [意图识别（intent.py）](#2-意图识别intentpy)
3. [调度编排（scheduler.py）](#3-调度编排schedulerpy)
4. [Agent 组装（agents/base.py）](#4-agent-组装agentsbasepy)
5. [18 个工具与 3 份 Skill 完整清单](#5-18-个工具与-3-份-skill-完整清单)
6. [ReAct Loop（loop.py）](#6-react-looplooppy)
7. [上下文管理（context.py）与 Scratchpad](#7-上下文管理contextpy与-scratchpad)
8. [短/长期记忆（memory.py）](#8-短长期记忆memorypy)
9. [static / live 两种模式对 Harness 的影响](#9-static--live-两种模式对-harness-的影响)
10. [为什么这是 Harness + Agent Loop，而不是 Workflow](#10-为什么这是-harness--agent-loop而不是-workflow)
11. [端到端实例：一次故障诊断的完整轨迹](#11-端到端实例一次故障诊断的完整轨迹)
12. [已知简化与升级路径（诚实清单）](#12-已知简化与升级路径诚实清单)
13. [改进思路与迭代路线](#13-改进思路与迭代路线)

---

## 1. 总览：一条消息的生命周期

```
用户输入 "用户反馈下单接口很慢"
   │
   ▼  main.py /api/chat（SSE，把下面每一步实时推给前端"执行轨迹"面板）
┌─────────────────────── Harness ───────────────────────┐
│ ① intent.py    意图识别 → {"intent": "fault_diagnose"} │
│ ② scheduler.py 调度     → 选中 DiagnoseAgent           │
│ ③ base.py      组装     → 提示词+工具集+Skill+记忆      │
│ ④ loop.py      ReAct    → LLM⇆工具 循环直到结论        │
│      ↑↓ context.py  每步管理上下文（截断/压缩/暂存）    │
│ ⑤ memory.py    落盘     → 会话历史 + 长期结论           │
└────────────────────────────────────────────────────────┘
```

**核心设计立场**：Harness 不替 LLM 做业务决策，只做四件事——

1. 把消息**路由**给对的 Agent（分诊）；
2. 给 Agent 配**对的工具和方法论**（配枪 + 发操作手册）；
3. 防止**上下文爆炸**（修剪工具结果、压缩历史）；
4. 把有价值的**结论留下来**（记忆沉淀）。

中间"调哪个工具、调几次、按什么顺序、出错了怎么办"全部由 LLM 在循环里自主决定。

---

## 2. 意图识别（intent.py）

**做什么**：把自然语言分类为 9 种意图之一，并抽取实体。

九种意图：`data_ingest` 采集 / `data_query` 数据查询 / `topology` 拓扑梳理 / `risk_scan` 风险扫描 /
`rule_create` 生成告警规则 / `fault_diagnose` 故障定位 / `remediation` 执行治理 /
`full_checkup` 全面体检 / `chat` 闲聊兜底。

**双通道设计**：

| 通道 | 实现 | 触发条件 |
|---|---|---|
| 主通道 | 一次轻量 LLM 调用（快模型、temperature=0），提示词内含 9 种意图的精确定义，要求只输出 JSON；输出经枚举校验防幻觉 | LLM 可用 |
| 降级通道 | `_KEYWORD_RULES` 正则表，**有优先级顺序**（`full_checkup` 最前、`data_query` 兜底最后，因为"全面体检"同时命中"体检/扫描"等词，需先匹配更具体的） | 无 API Key / LLM 调用失败 / 输出非法 |

**为什么单独做这一层**（而不是让一个大 Agent 自己看着办）：路由决定后面给 LLM 配**哪套工具、哪份 Skill**。
18 个工具全塞给一个 Agent，工具选择准确率会下降、提示词会膨胀；先分诊再进专科，每个 Agent 只面对 3~11 个工具。

### entities 字段是什么

输出格式为 `{"intent": "...", "entities": {"service": "...", "api": "...", "resource": "..."}}`：

| 字段 | 含义 | 例子 |
|---|---|---|
| `service` | 提到的服务名 | "order-service 是不是挂了" → `order-service` |
| `api` | 提到的接口 | "下单接口很慢" → `POST /api/orders` |
| `resource` | 提到的资源/实例 | "订单库连接数怎么样" → `rds-mysql-order` |

⚠️ **如实说明**：当前 entities 是"抽而未用"的预留字段。它随 `intent` 事件推给前端展示，但调度器只消费
`intent`——因为完整用户原文本来就会传给子 Agent，LLM 在 loop 里自己能读出实体。抽取成本为零（同一次
LLM 调用捎带），保留它是为了升级路径：意图预热（识别出 service 就提前查好该服务指标）、多意图拆分等。

---

## 3. 调度编排（scheduler.py）

`handle_message()` 是 Harness 总入口，三层分发：

```python
if intent == "full_checkup":   # 多 Agent 顺序编排
    _run_checkup(...)
elif llm.available():          # 单 Agent ReAct（意图→Agent 映射表 INTENT_AGENT）
    _run_single(...)
else:                          # 离线降级：脚本化流程
    _run_scripted(...)
```

### 3.1 意图 → Agent 映射

| 意图 | Agent |
|---|---|
| data_ingest | DataAgent |
| topology | TopologyAgent |
| risk_scan / rule_create / remediation | RiskAgent |
| fault_diagnose | DiagnoseAgent |
| data_query / chat | OpsAgent（general） |
| full_checkup | DataAgent → TopologyAgent → RiskAgent 顺序编排 |

### 3.2 多 Agent 顺序执行时的 Agent 间通信

机制是**一个共享 dict（scratchpad）+ 自然语言文本注入**，没有消息队列：

```python
scratchpad = {}                                   # 整条编排链共享
for agent_key, task in steps:                     # 采集 → 拓扑 → 风险扫描
    for ev in run_agent(agent, task, scratchpad=scratchpad):
        if ev["type"] == "answer":
            scratchpad[f"{agent['name']}_结论"] = ev["text"][:800]   # 上游写入
```

下游 Agent 启动时（loop.py）：

```python
ctx.scratchpad.update(scratchpad)                    # 接过共享 dict
ctx.add_user(user_input + ctx.scratchpad_prompt())   # 渲染成文本拼进首条消息
```

渲染效果——下游 Agent 的第一条 user 消息形如：

```
执行全量风险扫描，输出风险报告...

[已确认的中间结论]
{"DataAgent_结论": "采集完成，7 张表共 xx 行...",
 "TopologyAgent_结论": "9 条边，order→mysql 错误率 1.9%..."}
```

**通信的最终形态是自然语言**：下游消费者是 LLM，自然语言就是它的原生接口；结构化协议要双方约
schema，收益不匹配成本。代价是精度——上游结论没写的数值下游拿不到，但下游可以自己调工具再查（兜底）。

其他两个编排细节：

- **事件流透传**：调度器本身是 generator，把子 Agent 的每个事件原样 yield 给 main.py 转 SSE——前端
  能实时看到每一步，因为整条链路从 loop 到 HTTP 都是流式的；
- **单一最终回答**：中间 Agent 的 `answer` 事件被改写为 `phase_result`，只有最后汇总发真正的
  `answer`，保证一次对话只有一个回答气泡。

### 3.3 离线降级（_run_scripted）

无 API Key 时，四条核心业务流（采集/拓扑/扫描/诊断）退化为**固定工具调用序列 + 模板化回答**，
事件协议与 LLM 模式完全相同，前端无感知。这是演示保险丝——也恰好是系统里唯一一段真正的
"workflow"，与 ReAct 模式对照演示可以直观展示两者差异（见第 10 节）。

### 3.4 记忆沉淀

回答产生后 `_maybe_remember()` 判断意图是否值得沉淀：`fault_diagnose` / `risk_scan` / `full_checkup`
的结论写入长期记忆（详见第 8 节）。

---

## 4. Agent 组装（agents/base.py）

### 4.1 Agent 不是类，是一份配置

```
Agent = 系统提示词（role 职责 + 集群背景 + 工作准则）
      + 工具集（工具名列表，运行时从 registry 取 JSON Schema）
      + Skill 文档（可选，markdown 全文注入提示词）
      + 长期记忆片段（组装时实时检索注入）
```

**name 与 role 的区别**：`name` 是标签（"DiagnoseAgent"，前端展示用）；`role` 是一整句职责描述，
填进 `_BASE_PROMPT` 模板槽——"你是「全链路智能运维 Agent」的**故障定位专家。负责按五步排查法定位
故障根因，输出证据链和治理方案**……"。role 决定 LLM 的自我认知与行为倾向。

### 4.2 每次对话都重新组装（build_agent）

带来两个动态性：

1. **记忆是新鲜的**：`memory_prompt(user_query)` 按当前问题检索长期记忆注入——上次治理过什么、
   上次诊断结论是什么，Agent 天然"记得"；
2. **工具集是条件化的**：live 模式下自动摘除 DataAgent 的 `ingest_data`（内部 DELETE 七张表，会清掉
   采集器攒的动态数据），并追加提示"数据每 10~60s 持续更新，结论请标注时间窗"。
   **环境约束在组装层解决，loop 代码完全不知道模式的存在。**

### 4.3 Skill 机制

`skills/*.md` 是给 LLM 看的**方法论文档**，组装时全文注入系统提示词。它约束**方法**（先查接口统计、
再抓 trace、再下钻），不约束具体动作——这是"教 Agent 怎么排查"与"替 Agent 排查"的分界线。
Skill 文件每次组装实时读盘，改完即时生效（例：让拓扑回答必须带 mermaid 图，只改了
`topology_analysis.md` 一个文件，未动任何代码）。

---

## 5. 18 个工具与 3 份 Skill 完整清单

### 5.1 工具四组（全部带 JSON Schema 注册，LLM 靠 description 决定何时调用）

**查询组（data_tools.py，7 个）**——只读基础能力：

| 工具 | 干什么 |
|---|---|
| `ingest_data` | 全量采集 mock 数据入库（**会先清表**，危险工具；live 模式返回"跳过"） |
| `query_metrics` | 查 CMS 指标聚合（namespace / 指标名 / 维度过滤，返回均值/最值/点数） |
| `query_logs` | 查四类日志：app / slow / event / ingress，支持关键词、级别、状态码过滤 |
| `api_perf_stats` | 按接口聚合 ingress 日志 → 每接口请求数/P99/5xx 错误率。**故障排查第一步专用** |
| `query_traces` | 三用法：trace_id 看整条链路逐 span 耗时；api_name 找最慢/出错根 span；slow_ms 捞慢请求 |
| `get_k8s_resource` | 查 K8s 快照（Node/Deployment/Pod/Service/PDB），列表给摘要、指名给完整 spec |
| `sql_query` | **兜底自由查询**：只读 SELECT（正则拦 DML/DDL + 表名白名单 + 强制 LIMIT 50）。工具结果被截断时 LLM 靠它下钻 |

**拓扑组（topology_tools.py，2 个）**：

| 工具 | 干什么 |
|---|---|
| `build_topology` | 从 trace 的 client span 按 (service, peer.service) 聚合出边（调用数/错误率/P99），落库并返回图数据 |
| `get_topology` | 读已生成的拓扑（不重算） |

**风险组（risk_tools.py，4 个）**：

| 工具 | 干什么 |
|---|---|
| `run_risk_scan` | 执行 11 条内置规则 + AI 生成规则，管理 finding 生命周期（open/resolved） |
| `get_risk_report` | 只读最近一次扫描报告 |
| `create_risk_rule` | **AI 生成告警规则**：LLM 写一条 SQL + 阈值，先试跑验证再入库启用 |
| `list_risk_rules` | 列出全部规则（内置 + AI） |

**治理组（remediation_tools.py，5 个）**——系统里唯一的"写"能力：

| 工具 | 干什么 |
|---|---|
| `patch_deployment` | 扩缩副本 / 补探针 / 补改 CPU request、内存 limit、CPU limit / 移除单可用区亲和 |
| `create_pdb` | 创建 PodDisruptionBudget |
| `create_db_index` | 建索引治理慢查询 |
| `upgrade_rds_instance` | RDS 升配 |
| `list_governance_actions` | 治理动作审计记录 |

（live 模式下前四个执行成功后额外把动作转发给 mock_server，驱动世界状态真实恢复。）

### 5.2 三份 Skill

| Skill | 内容 | 使用者 |
|---|---|---|
| `topology_analysis.md` | span 配对算法、DB 边从 db.system 判读、异常边点名标准、**必须附 mermaid flowchart 代码块**（前端自动渲染成图） | TopologyAgent |
| `fault_diagnosis.md` | 五步排查法：接口统计圈定病灶 → 抓问题 trace → 链路下钻 → 慢日志/指标多源收口 → 根因链+方案；强调"每步必须用工具拿真实数据，宁可少说不可编造" | DiagnoseAgent |
| `risk_governance.md` | 风险→治理动作速查表（HA-002→扩副本、DB-002→建索引…）+ 治理纪律（**必须等用户确认才动手**、治理后复扫闭环）+ AI 规则生成方法 | RiskAgent |

### 5.3 每个 Agent 的配置与逻辑

| Agent | 工具（数量） | Skill | 配置逻辑 |
|---|---|---|---|
| **DataAgent** | ingest_data、sql_query、query_metrics（3） | 无 | 只干采集与数据概况，任务简单无需方法论 |
| **TopologyAgent** | build/get_topology、query_traces、sql_query（4） | topology_analysis | 拓扑梳理 + mermaid 出图 |
| **RiskAgent** | 风险组 4 + 治理组 5 + get_k8s_resource + sql_query（11） | risk_governance | **唯一持有治理工具**的 Agent |
| **DiagnoseAgent** | 查询组 6（无 ingest_data）+ get_topology（7） | fault_diagnosis | **故意不给治理工具**：诊断只出方案，执行走 RiskAgent 或治理面板，权责分离 |
| **OpsAgent** | 查询组 6 + get_topology + get_risk_report + list_governance_actions（9） | 无 | 兜底：数据问答、闲聊、"刚才发生了什么"类回顾 |

设计原则一条：**最小权限 + 最小工具面**。工具越少 LLM 选择越准、提示词越短；危险能力（清库、变更）
只给对应职责的 Agent。

---

## 6. ReAct Loop（loop.py）

"不是 workflow"的核心证据，主循环不到 60 行：

```python
for step in range(MAX_AGENT_STEPS):          # 预算护栏：最多 12 步
    ctx.compact()                            # 每步先检查上下文是否需压缩
    msg = llm.chat(ctx.messages, tools=tools)  # LLM 决策：说话 or 调工具
    ctx.add_assistant(msg)
    if msg.content:
        yield {"type": "thinking", ...}      # 思考过程实时推前端
    if not msg.tool_calls:                   # 无工具调用 = 得出结论，退出
        final_answer = msg.content
        break
    for tc in msg.tool_calls:                # 执行 LLM 选的工具
        yield {"type": "tool_call", ...}
        result = registry.execute(name, args)  # 永不抛异常，错误也作为结果返回
        yield {"type": "tool_result", ...}
        ctx.add_tool_result(tc.id, name, result)
```

**Reason（思考）→ Act（工具执行）→ Observe（结果回填）→ 再 Reason**，循环到 LLM 自认可以回答为止。

三个护栏：

1. **步数预算**：12 步封顶，超限强制收敛为"基于现有信息的结论"，防死循环烧 token；
2. **工具异常兜底**：`registry.execute` 把任何异常包装成 `{"error": ...}` 返回——LLM 看到错误自己换参数
   重试或换工具，**错误恢复也是 LLM 的决策**，不是 harness 的 try-retry 策略；
3. **事件流协议**：`thinking / tool_call / tool_result / answer / error` 五种事件是 loop 对外的唯一接口，
   调度器、SSE、前端面板消费同一协议。

可观察的非确定性：同一句"下单接口很慢"，LLM 有时 4 步有时 6 步，查慢日志与查指标的顺序也会变——
**路径不确定但结论稳定**，这是 ReAct 与固定 DAG 的直观区别。

---

## 7. 上下文管理（context.py）与 Scratchpad

### 7.1 上下文管理 vs 短期记忆（易混，先分清）

| | 上下文管理（ContextManager） | 短期记忆（chat_messages 表） |
|---|---|---|
| 是什么 | 进程内 `messages` 数组 | 数据库表 |
| 活多久 | **一次 loop 运行**，结束即销毁 | **跨消息、跨重启**持久化 |
| 装什么 | system 提示词、user 消息、tool_calls、**每个工具的返回结果** | 只存 user/assistant 的**最终文本** |
| 解决什么 | 工具结果太大撑爆窗口 | 多轮对话连贯性 |

衔接点在 `scheduler._run_single()`：处理新消息时先从 `chat_messages` 捞最近 8 条历史，塞进新建的
ContextManager 作开场上下文——**短期记忆是持久层，上下文是它在本次运行中的工作副本 + 本次新产生
的工具轨迹**。

### 7.2 三个管理机制（按触发频率排）

1. **工具结果截断**（每次都可能发生）：超 2KB 截断，尾部追加关键提示——
   *"完整数据已入库，可用 sql_query 工具精确查询"*。不只省 token：把"数据太大"转化为"给 LLM 一个
   下钻手段"，LLM 看到提示会自己写 SQL 缩小范围；
2. **历史压缩**（长对话触发）：总量超 24K 字符时，保留 system + 最近 6 条，中段用快模型压成 200 字
   摘要（要求保留数值/资源名/traceID）；LLM 不可用时降级硬截断；
3. **Scratchpad**（跨步骤/跨 Agent）：见下。

### 7.3 Scratchpad 详解

`ContextManager` 上的一个 dict + 三个方法：

```python
self.scratchpad: dict = {}
def note(key, value): ...            # 写一条中间结论
def scratchpad_prompt() -> str: ...  # 渲染成 "[已确认的中间结论]\n{json}" 文本
```

**设计意图**：消息会被压缩/截断，但某些中间结论"压缩不能丢"（确认过的 traceID、慢 SQL 原文）——
scratchpad 是一块"防压缩的置顶便签"，注入时原样带上。

⚠️ **实际用法（如实说明）**：当前唯一活跃写入方是**调度器**——多 Agent 编排时写 `{Agent名}_结论`
作为 Agent 间通信载体（见 3.2）。单 Agent loop 内部暂无 `note()` 调用：5 分钟窗的数据量下，中间结论
靠 24K 窗口就能活到最后。准确说法：**scratchpad 当前承担 Agent 间通信，防压缩置顶是预留能力**
（激活方式：给 LLM 注册 save_note 工具，或压缩前用快模型抽取关键结论写入）。

---

## 8. 短/长期记忆（memory.py）

### 8.1 存在哪、存什么

两张表，都在 MySQL/SQLite（重启不丢）：

**短期：`chat_messages`** `(session_id, role, content, tool_calls_json, created_at)`
写入时机：handle_message 开头存 user 消息、产出回答后存 assistant 消息；按 session_id 隔离
（前端每次打开页面生成 `web-<时间戳>` 会话）。

**长期：`agent_memory`** `(scope, mem_key, content, created_at)`，三种 scope、三个写入方：

| scope | 谁写 | 写什么 | 例子 |
|---|---|---|---|
| `conclusion` | 调度器 `_maybe_remember()`（仅 fault_diagnose / risk_scan / full_checkup 三种意图） | 回答文本前 600 字，key 固定，**同 key 覆盖更新** | "最近一次故障定位结论: 根因链 API-001←DB-002←DB-001…" |
| `governance` | 治理工具 `_record_governance()` | 每次治理动作一条 | `db_index:orders → 已执行 CREATE INDEX...，治理慢日志 50 条` |
| `preference` | 预留（接口支持，暂无调用方） | 用户偏好 | — |

**governance 记忆的双重身份**：既给 LLM 看（"上次已治理过"），又被**确定性规则代码消费**——
static 模式下规则引擎用 `_has_governance_prefix("db_index:")` 判定 DB-002 转 resolved。
记忆不只是提示词素材，也是系统状态的一部分。

### 8.2 检索机制

每次 `build_agent()` 组装时：`memory_prompt(user_query)` → `recall(query)`：

```python
terms = [t for t in query.replace("，"," ").replace(","," ").split() if len(t) >= 2][:5]
# 每个词对 content / mem_key 做 OR LIKE，按时间倒序取 8 条
# 一条都没匹配 → 降级返回最近 8 条
```

渲染注入 system prompt：

```
[长期记忆（历史结论与治理记录，可直接引用，但需注意时效）]
- [conclusion] 最近一次故障定位结论: 根因链……
- [governance] db_index:orders: 已执行 CREATE INDEX……
```

⚠️ **如实说明检索的粗糙之处**：分词按空格/逗号切，中文问题通常无空格——"当前系统有什么风险"整句
成为一个 term，LIKE 大概率不中，**实际多数时候走降级路径（返回最近 8 条）**。本项目量级下效果可接受
（长期记忆总量十几条，最近 8 条 ≈ 全部），但严格说这是"时间近因检索"而非"语义检索"。
升级路径：换 embedding + 向量相似度，`recall()` 接口不变。

最后一道防线在提示词：注入时标注"**需注意时效**"，工作准则要求一切结论以工具实查为准——
记忆负责"提醒 Agent 去查什么"，不替代查询。live 模式下尤其重要（数据每 10s 在变）。

---

## 9. static / live 两种模式对 Harness 的影响

模式由 `DATA_SOURCE` 环境变量控制（config.py），对应两套数据世界：

| | **static（默认）** | **live** |
|---|---|---|
| 数据来源 | 静态数据集 `data/data`，`ingest_data` 一次性灌库 | mock_server 持续演算世界，data_collector 每 10~60s 增量写库 |
| 数据是否变化 | 不变（快照） | 持续变化（可注入故障、可被治理动作影响） |
| Agent 工具集 | 完整 | **组装层摘除 DataAgent 的 ingest_data** |
| 系统提示词 | 基础版 | 追加"数据持续更新，结论请标注时间窗；无需也无法手动重新采集" |
| 规则语义 | 全表聚合 | 5 分钟滑动窗 + 按实例分组 |
| 风险 resolved 判定 | **信任 governance 记忆标记**（数据不会变，不靠标记风险永远 open） | **无视标记，纯靠数据回落**（标记会造成"动作一执行立刻 resolved 但指标未恢复"的假闭环，且旧标记会让同一故障二次注入被误判为已治理） |
| 定时扫描 | 不启动 | 每 60s 后台扫描 + diff 写 scan_reports |

为什么 live 要摘 `ingest_data`：其内部是 `DELETE FROM` 七张表再重灌。static 下是"一键重置演示环境"
的正常操作；live 下会清掉采集器持续攒的动态数据——用户随口一句"重新采集"，LLM 就可能调它。
防线有两道：组装层不给这个选项（主）+ 工具自身在 live 下返回"跳过"（兜底）。

**记忆机制在两种模式下**：存储与检索完全相同，唯一分叉是 governance 记忆的消费方式（上表最后一行）。
一句话：记忆对 LLM 的作用两种模式一致（历史线索）；对确定性规则，static 是判据、live 只是审计日志。

---

## 10. 为什么这是 Harness + Agent Loop，而不是 Workflow

| 维度 | Workflow | 本项目 |
|---|---|---|
| 执行路径 | 编排时写死（节点/DAG） | LLM 每步现场决策，同一问题路径可不同 |
| 工具选择 | 节点绑定固定动作 | LLM 从工具集自选、参数自拟（含自由 SQL） |
| 错误处理 | 预设分支/重试策略 | 错误作为观察结果回给 LLM，由它决定换招 |
| 新增能力 | 改流程图 | 注册一个工具 / 加一份 skill md，loop 零改动 |
| harness 职责 | 驱动流程 | 只提供护栏：路由、预算、上下文、记忆 |

最有说服力的一句话：**这套代码里不存在任何一处"if 上一步是 A 则下一步做 B"的业务流转逻辑**。
`scheduler.py` 唯一的顺序编排（full_checkup）也只是给三个自治 Agent 排了出场顺序，每个 Agent
内部怎么干仍由自己决定。

系统里确实**也有**一条真正的 workflow——离线降级的 `_run_scripted`。两者并存恰好可对照演示：
断掉 API Key 跑一次（固定 4 步、固定顺序、模板回答），接上再跑一次（步数/顺序随机、生成式回答），
看执行轨迹差异即可直观理解两种范式。

---

## 11. 端到端实例：一次故障诊断的完整轨迹

输入：**"用户反馈下单接口很慢，帮我定位"**（live 模式实测）

| 步 | 环节 | 发生了什么 |
|---|---|---|
| 1 | intent | LLM 路由 → `fault_diagnose` |
| 2 | scheduler + 组装 | 选 DiagnoseAgent；注入五步排查法 skill + 7 个查询工具 + 长期记忆（若有上次诊断结论会带上） |
| 3 | loop 第 1 步 | LLM："先圈定病灶接口" → 调 `api_perf_stats` → POST /api/orders P99 1.6s |
| 4 | loop 第 2 步 | 调 `query_traces(api_name=..., only_error=true)` → 拿到问题 trace_id |
| 5 | loop 第 3 步 | 调 `query_traces(trace_id=...)` 下钻 → `UPDATE orders` span 占整链 80%+ |
| 6 | loop 第 4-5 步 | 调 `query_logs(slow)` + `query_metrics(acs_rds_dashboard)` 收口——慢日志 rows_examined 百万级、连接使用率 83% |
| 7 | loop 退出 | LLM 不再发起工具调用 → 输出根因链（接口慢 ← 无索引全表扫描 ← 连接高水位）+ 治理建议（建复合索引，标注"待确认后执行"） |
| 8 | memory | 调度器把结论存入长期记忆——之后问"刚才发生了什么"，OpsAgent 组装时检索到直接引用 |

全过程 harness 没有指定过任何一步"该查什么"，它做的只是：分诊到对的专科（步 1-2）、每步把工具结果
修剪好再喂回去（context）、最后把病历归档（步 8）。

---

## 12. 已知简化与升级路径（诚实清单）

评委深挖时主动承认，比被挖出来强：

| # | 简化点 | 现状 | 升级路径 |
|---|---|---|---|
| 1 | 意图识别是**单意图**分类 | "先扫描再治理"这类复合请求靠 Agent 在 loop 内自行连续完成，而非调度层拆分 | 多意图拆分进 scheduler，复用现有编排机制 |
| 2 | entities 抽而未用 | 仅前端展示，调度不消费 | 意图预热 / 实体路由 |
| 3 | 记忆检索是关键词 LIKE | 中文无空格分词常整句失配，实际多走"最近 8 条"降级 | embedding + 向量相似度，`recall()` 接口不变 |
| 4 | scratchpad 的防压缩置顶未激活 | 当前只承担 Agent 间通信 | 注册 save_note 工具或压缩前自动抽取 |
| 5 | Agent 间通信为自然语言 | 上游结论截断 800 字注入下游首条消息 | 结构化 handoff schema（如需精确传参） |
| 6 | 长期记忆同 key 覆盖 | conclusion 类只保留"最近一次" | 追加式 + 时间衰减权重 |

以上取舍的共同理由：一天赛程内，每一项的"简化版"都已闭环可演示，"完整版"都有不破坏现有接口的
升级路径——这本身就是 harness 分层是否合理的检验。

---

## 13. 改进思路与迭代路线

第 12 节是"已知欠账"，本节是"往前怎么走"——按投入从小到大分三个梯度，每项都锚定到现有代码的
具体模块，保证"可开工"而非愿景清单。

### 13.0 迭代两条铁律

1. **接口不破坏**：五个稳定接口是迭代的保护网——事件流协议（thinking/tool_call/…）、工具注册
   （registry.tool 装饰器）、Agent 配置（AGENT_SPECS 三元组）、记忆接口（recall/remember）、
   Skill 注入（markdown 文件）。所有改进都应在接口背后替换实现，前端与调用方零感知；
2. **评测先行**：没有评测基准的提示词/策略改动是盲调。本项目已有现成基准：static 模式的 11 条
   ground truth、live 模式的阈值对照表、四故障场景的预期触发规则——先把它们固化成自动化评测
   （见 13.4），再做其他迭代。

### 13.1 近期（赛后 1~3 天，单点小改，各 ≤ 半天）

| # | 改进 | 现状痛点 | 方案与落点 |
|---|---|---|---|
| N1 | **记忆语义检索** | LIKE 分词对中文失效，实际多走"最近 8 条"降级 | DashScope text-embedding 入库时向量化，`recall()` 内部改余弦相似度 Top-K（十几条量级直接 Python 算，不引入向量库）；接口签名不变 |
| N2 | **预算分级** | 所有意图共用 MAX_STEPS=12 与同一主模型，闲聊也烧 qwen-max | AGENT_SPECS 增加 `max_steps` 与 `model` 字段（诊断 12 步 max，闲聊 3 步 turbo）；同时把步数预算升级为 token 预算（累计 usage 超限即收敛） |
| N3 | **工具结果分型摘要** | 超 2KB 一律尾部硬截断，可能切掉关键字段 | 按工具定制摘要器：trace 结果保留耗时 Top-5 span、拓扑结果保留异常边全量+正常边计数；在 `truncate_tool_result` 前插一层 per-tool summarizer |
| N4 | **高危工具 loop 内确认** | 治理确认只在提示词层面约束（"必须等用户确认"），靠 LLM 自觉 | 工具注册时标注 `risk_level`，loop 拦截高危调用 → 发 `confirm_request` 事件 → 前端弹确认 → 用户同意后继续。把"人在环"从提示词约定变成机制保证 |
| N5 | **entities 消费：上下文预热** | 意图层抽了实体但没用 | 识别出 `service/api` 时，调度层预先调一次 `api_perf_stats(url=...)` 把结果塞进首条消息——诊断类问题平均省 1~2 步 loop |

### 13.2 中期（1~2 周，协作模式升级）

**M1 从固定分诊到动态委派（planner/executor）**
现状：意图→Agent 是静态映射表，full_checkup 是写死的三步序列。
改进：新增一个 `dispatch_agent(agent, task)` 工具注册给一个轻量 PlannerAgent，由 LLM 自主决定
"这个问题先让 TopologyAgent 看拓扑，再转 DiagnoseAgent"——编排本身也变成 ReAct 决策。
实现上 `dispatch_agent` 内部就是递归调 `run_agent()`，事件流嵌套透传；现有静态映射保留作为
降级路径（planner 失败时回退）。

**M2 编排并行化**
现状：checkup 的拓扑与风险扫描串行，但两者无数据依赖；loop 内同一步的多个 tool_calls 也是
顺序 for 执行。
改进：两层并行——编排层用 asyncio.gather 跑无依赖的 Agent（事件流加 agent 标识后合并）；
loop 层把同步工具扔进线程池并发。前端 ToolTimeline 按 agent 分泳道展示。

**M3 结论前自检（Reflexion 轻量版）**
现状：LLM 不再调工具即视为结论，证据链完整性只靠 skill 里的约束。
改进：loop 退出前插入一次快模型自检——"结论中每个数值/资源名是否能在工具轨迹中找到出处？
根因链是否每环有证据？"不通过则带着自检意见回炉一轮（步数预算内）。针对幻觉数值这个
运维场景最致命的风险点。

**M4 结构化 handoff**
现状：Agent 间只传 800 字自然语言结论，下游拿不到精确参数（如 trace_id 列表）需重查。
改进：scratchpad 里除文本结论外增加结构化附件槽（trace_ids / finding_ids / 异常边列表），
上游 Agent 通过新增的 `save_note` 工具主动写入——同时把第 12 节简化 #4（scratchpad 防压缩
置顶未激活）一并解决。

**M5 多意图拆分**
现状："先扫描再把 P1 都治理掉"这类复合指令靠单 Agent 在 loop 内自行连续完成，跨 Agent
能力时会卡住。
改进：intent 输出改为意图序列 `[{intent, depends_on}]`，调度器复用 checkup 的编排机制按
依赖执行——M1 落地后此项可直接由 planner 承接，两者二选一。

### 13.3 远期（持续演进方向）

| 方向 | 思路 |
|---|---|
| **记忆分层与归纳** | 现在只有 episodic（单次结论，且同 key 覆盖）。演进：结论改追加式存储 + 后台周期任务用 LLM 归纳模式级记忆（"rds-mysql-order 历史上 3 次慢查询风暴，均为 orders 表缺索引"）——诊断时直接命中历史模式可跳过排查前三步；叠加时间衰减权重防旧结论误导 |
| **Skill 自进化** | 诊断成功的轨迹（用户确认/治理后复扫通过）由 LLM 反向提炼成 skill 补丁（"遇到 X 症状先查 Y"），人工 review 后合入 md——skill 文件即提示词的版本化载体，git diff 即变更审计 |
| **工具自动生成** | 接真实云环境时，由 LLM 依据 OpenAPI/SDK 文档生成工具定义（schema + 实现骨架），人工审核后注册——registry 装饰器机制天然支持动态注册 |
| **主动式 Agent** | 目前全部是"用户问→Agent 答"。live 模式已有定时扫描（background.py），下一步：扫描发现 new finding 时自动触发 DiagnoseAgent 预诊断，把"告警+根因初探+建议方案"一起推给用户——从 chatbot 到 on-call 副驾驶 |
| **接真实环境** | MockProvider → 真实 CMS/SLS SDK（文件头已声明替换缝隙）；治理工具 → kubectl patch / RDS OpenAPI + 审批流。Harness 层零改动，这是分层设计的最终兼现 |

### 13.4 评测体系（所有迭代的前提，建议最先做）

现成素材都在仓库里，缺的只是一个跑分脚本：

| 评测项 | 基准来源 | 指标 |
|---|---|---|
| 意图识别准确率 | 人工标 50 句测试集（含口语化/复合句） | top-1 准确率，目标 ≥95% |
| 诊断命中率 | 四故障场景×注入后提问，断言结论含预期根因关键词（如 slow_query_storm → 必含"索引/全表扫描"） | 命中率 + 平均 loop 步数 + token 成本 |
| 风险扫描召回/误报 | static 11 条 ground truth + live 阈值对照表 | 不多报不少报（已有手工验收，固化成 CI） |
| 回归防护 | chat_messages 已存全量 tool_calls_json | 轨迹回放对比：提示词/skill 改动前后，同问题的工具选择与结论差异 |

落地形态：`scripts/eval_agent.py`，输入问题集 JSON，走真实 handle_message，输出指标报告——
之后任何提示词/skill/模型改动，先跑分再合入。

### 13.5 优先级建议（如果只能挑三件）

1. **13.4 评测脚本**：没有它，其他所有改进都无法证明"改好了"；
2. **N4 高危工具机制化确认**：把安全从"LLM 自觉"变成"架构保证"，是面试/答辩中最能体现
   工程成熟度的一项；
3. **M3 结论自检**：运维场景容错率最低的是"编造数值"，一次廉价的快模型自检对可信度提升
   性价比最高。
