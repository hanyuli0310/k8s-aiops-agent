# 全链路智能运维 Agent（一天黑客松）实施方案

## 总体架构

```
React 对话 GUI (SSE 流式)
        │ /api/chat
FastAPI 后端
        │
┌──── Agent Harness（自研，评委考察核心）────┐
│ intent.py    意图识别（LLM 路由：数据查询/  │
│              风险扫描/故障定位/治理/闲聊）    │
│ scheduler.py 调度器（将意图分发给子Agent，   │
│              支持多步任务编排）              │
│ loop.py      Agent Loop（ReAct：LLM function│
│              -calling ↔ tool 执行，直到产出  │
│              最终答案，带最大步数/预算控制）  │
│ context.py   上下文管理（消息裁剪、大工具结果 │
│              截断+摘要、跨步骤引用）          │
│ memory.py    短期会话记忆 + 长期记忆(MySQL：  │
│              历史结论/已确认风险/用户偏好)    │
│ llm.py       DashScope OpenAI 兼容客户端     │
└─────────────────────────────────────────┘
        │ tool calls
Tool Registry（JSON Schema 注册）+ Skills（md 文档注入）
        │
MockProvider（模拟 CMS DescribeMetricList / SLS GetLogs）──采集──▶ MySQL
```

技术选型（已定）：Python 3.11 + FastAPI；**自研 Agent loop**（不用 AgentScope——一天内自研 500 行以内的 loop 可控且能向评委直接展示 harness 设计，框架封装反而说不清）；MySQL 8（docker-compose 一键起，无 docker 则降级 SQLite 同一套 SQLAlchemy 代码）；LLM 用 qwen-max（意图/诊断）+ qwen-turbo（摘要/压缩）；前端 React + Vite + antd + antv/G6（拓扑图）。

## 目录结构

```
k8s-aiops-agent/
├── 世界观Mock数据集/            # 已有，只读
├── docker-compose.yml           # mysql:8
├── backend/
│   ├── requirements.txt  .env.example
│   └── app/
│       ├── main.py              # FastAPI：/api/chat(SSE) /api/ingest /api/topology /api/risks
│       ├── config.py  db.py     # 配置、SQLAlchemy engine + 建表
│       ├── harness/             # loop.py context.py intent.py scheduler.py memory.py llm.py
│       ├── agents/              # data_agent / topology_agent / risk_agent / diagnose_agent（各=系统提示词+工具集+可加载skill）
│       ├── tools/               # registry.py + cms/sls/k8s/topology/risk/remediation 工具
│       ├── skills/              # topology_analysis.md  fault_diagnosis.md  risk_governance.md
│       ├── providers/mock_aliyun.py  # 读 mock 文件，模拟 CMS/SLS API（分页、Datapoints字符串等格式坑保真）
│       ├── ingest/pipeline.py   # Provider → 解析 → 批量写 MySQL
│       └── rules/builtin.py     # 固定规则引擎（覆盖 11 条 ground truth）
└── frontend/                    # Vite+React：聊天流 + 拓扑图卡片 + 风险报告卡片 + 工具调用过程折叠展示
```

## MySQL 表设计

- `metrics(id, namespace, metric_name, dims_json, ts, avg, max, min)` — 3,904 点
- `ingress_logs(ts, method, url, status, request_time, upstream_addr, req_id, raw_json)` — 11,939 行，req_id 建索引
- `trace_spans(trace_id, span_id, parent_span_id, service, name, kind, start_us, duration_us, status_code, status_message, attr_json, resource_json)` — 9,246 行；入库时把 attribute/resource 的 JSON 字符串**解开存 JSON 列**
- `app_logs / slow_logs / k8s_events` — 按原字段展开
- `k8s_resources(kind, namespace, name, spec_json)` — 35 个资源
- `topology_edges(source, target, call_count, error_rate, avg_ms, p99_ms)` — 拓扑 skill 产出
- `risk_rules(rule_id, source[builtin|ai], severity, title, check_sql/check_type, threshold, enabled)`
- `risk_findings(rule_id, severity, resource_ref, evidence_json, status[open|resolved], suggestion)`
- `agent_memory(scope, key, content, created_at)` — 长期记忆
- `chat_messages(session_id, role, content, tool_calls_json)` — 会话历史

## Harness 核心设计（评委考察点）

- **Agent Loop**（loop.py）：`while step < MAX_STEPS`：组装 messages → LLM(tools=schema) → 若 tool_calls 则并行执行、结果回填、继续；若纯文本则结束。每步经 SSE 推送 `thinking/tool_call/tool_result/answer` 事件给前端展示"Agent 在干什么"。
- **意图识别**（intent.py）：轻量 LLM 调用输出结构化 `{intent, entities}`，五类意图：data_query / topology / risk_scan / fault_diagnose / remediation；未命中走通用对话。
- **调度**（scheduler.py）：意图 → 选择子 Agent（工具集+系统提示词+skill 文档）；复杂任务（如"全面体检"）拆为 采集→拓扑→扫描→汇总 的多 Agent 顺序执行，各步产物经 context 传递。
- **上下文管理**（context.py）：工具结果超 2KB 自动截断并附"已存 MySQL，可用 SQL 查询"提示；历史超窗口用 qwen-turbo 压缩为摘要；诊断中间结论（如已定位的 traceID）写入 scratchpad 供后续步骤引用。
- **Memory**（memory.py）：会话内 scratchpad + 跨会话长期记忆（如"已确认 API-001 根因是无索引慢查询"），Agent 回答前检索注入，治理后写入"已治理"记录——演示第二次扫描时 Agent 能记得。

## 工具与 Skill

工具（全部 JSON Schema 注册，供 LLM function calling）：
- `query_metrics(namespace, metric, dims, agg)` / `query_logs(logstore, filters, limit)` / `query_traces(trace_id | service | slow_threshold)` / `get_k8s_resource(kind, name)` / `sql_query(sql)`（只读白名单，兜底自由查询）
- `build_topology()`：从 trace_spans 聚合 client-span→server-span 边，写 topology_edges，返回图数据（对齐 manifest 9 条边）
- `run_risk_scan(scope)`：执行 risk_rules 全部启用规则，写 risk_findings
- `generate_risk_rule(description)`：LLM 依据表 schema 生成新规则（SQL + 阈值）入库——"AI 智能生成告警规则"
- 治理工具（**模拟执行**，直接改 MySQL 里的 k8s_resources 快照，形成可验证闭环）：`patch_deployment(name, patch)`（扩副本/补探针/补 request/limit）、`create_pdb(app)`、`create_db_index(table, columns)`（标记慢 SQL 已治理，slow_logs 后续查询命中提示已建索引）

Skills（markdown，被对应 Agent 加载进系统提示词）：
- `topology_analysis.md`：span kind 配对算法、DB 边从 attr 的 db.system 推断
- `fault_diagnosis.md`：五步排查法（Ingress P99 分组 → req_id↔traceID → span 耗时下钻 → 慢日志/指标收口 → 根因+方案），即数据说明第 5 节路径
- `risk_governance.md`：11 类风险的治理动作映射（HA-002→扩副本、DB-002→建索引…）

固定规则（rules/builtin.py，纯 SQL/Python 判定，不依赖 LLM，保证 11 条 ground truth 全命中）：HA-001~004、CAP-001~004、DB-001~002、API-001。

## 业务流（演示动线）

1. **数据采集**：对话"采集集群数据" → data_agent 调 ingest → MockProvider 按阿里云 API 格式吐数 → 入 MySQL → 返回采集报告（各表行数，与 manifest 对账）
2. **拓扑梳理**：对话"梳理服务拓扑" → topology_agent 加载 skill → build_topology → 前端 G6 渲染 9 边拓扑图，病灶边（order-service→MySQL 错误率 1.93%）标红
3. **风险扫描**：对话"做一次风险扫描" → 固定规则 11 条全命中 + 演示 AI 生成新规则（如"Pod 重启次数>3 告警"）→ 风险报告卡片（severity 分组）→ 每条附 LLM 治理方案
4. **故障定位+治理**：用户输入"用户反馈下单接口很慢" → diagnose_agent 按 skill 五步排查（每步工具调用在 GUI 实时可见）→ 输出根因链（API-001←DB-002←DB-001）→ 用户确认后调 `create_db_index` + `patch_deployment` 治理 → 重扫验证 finding 转 resolved，写入长期记忆

## 三人分工与时间线

- **A（Agent 核心）**：harness 全部 + agents + skills + 规则引擎。上午：llm.py+loop.py+registry 跑通单 Agent echo 工具；下午：意图/调度/memory + 诊断链路调优
- **B（数据层）**：docker-compose + db.py 建表 + MockProvider + ingest + 全部查询/治理工具 + builtin 规则。上午必须交付"数据全部入库 + 工具可独立调用"，供 A 集成
- **C（前端）**：React 聊天页 + SSE 事件流渲染（工具调用折叠面板）+ 拓扑图/风险报告卡片。上午用 mock SSE 数据先行开发，下午联调
- **里程碑**：T+3h 数据入库+loop 跑通；T+6h 四条业务流后端可用；T+8h GUI 联调完成；最后 1-2h 演示脚本彩排 + memory/AI 生成规则等加分项

## 测试与验收

- 对账脚本：入库行数 vs manifest.meta.files；扫描结果 vs risk_ground_truth 11 条（不多报不少报）；拓扑 vs topology_ground_truth 9 边
- 诊断验收：输入"下单慢"，Agent 结论须命中 API-001→DB-002→DB-001 因果链并给出 `orders(status, created_at)` 索引建议
- 治理闭环验收：治理后重扫，对应 finding 变 resolved

## 假设

- 比赛现场可访问 DashScope API（key 由你们提供，.env 配置）
- MySQL 用本机 docker 起；无 docker 环境自动降级 SQLite（代码同一套）
- 治理动作均为模拟执行（改 DB 快照），不连真实集群——演示时明确说明"对接真实环境仅需替换 Provider 与治理工具实现"