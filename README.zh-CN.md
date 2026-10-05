[English](README.md) | **中文**

# k8s-aiops-agent — Agentic AIOps for Kubernetes

> 面向生产 K8s 集群的对话式智能运维 Agent，覆盖「数据采集 → 拓扑梳理 → 风险扫描 → 故障定位 → 治理闭环」全链路。核心是自研的 Agent Harness（非 workflow 编排）。

**亮点**（数据来自 [量化评测报告](docs/量化评测报告-最终版.md)）
- 12 个注入故障场景下，规则侧 Precision / Recall / F1 均为 100%（0 误报、0 漏报）
- 集群从 19 扩到 603 个实例（32 倍）后精度不退化，规则扫描耗时 0.05s
- 对比朴素基线（同模型、同工具，仅去掉 Harness）：工具调用中位数 -27%，排障耗时中位数 -20%
- 数据过期时自动重查复核并告知用户；破坏性动作需用户显式确认并留审计
- 350 项常驻用例 + CI；评测过程中定位并修复 13+ 个真实缺陷

## 背景

在阿里云 Intern AI Hackathon（2026）中完成的项目。

---

# 项目：全链路智能运维 Agent

面向生产 K8s 集群（`prod-cluster-01` / cn-hangzhou）的智能运维 Agent：对话式完成**数据采集 → 拓扑梳理 → 风险扫描 → 故障定位 → 治理闭环**。

核心是一套**自研 Agent Harness（非 workflow）**，对标业界 Coding Agent 的工程实践落到运维域：
模型自主编排调度 + ReAct Loop + 三态权限门禁与审计 + 并行子 Agent + 结论事实核对（含时效维度）+
分级上下文压缩 + 任务清单与分段续跑 + 短/长期记忆。8 篇 Skill 三层渐进式披露，8 个专职 Agent（5 个可派发）、22 个工具、13 类风险规则。
**350 项常驻单测 + 一键 CI 执行体**兜底，机制均经真机（qwen 系模型）实测，
并有一份**量化评测报告**：[最终版](docs/量化评测报告-最终版.md)（真值来源 / 评测数据 / 判定标准 / 结果 / 复现方式，面向未参与项目的读者）与[过程记录](docs/量化评测报告.md)（按实验演进，含缺陷发现过程）。

两种数据模式（`backend/.env` 的 `DATA_SOURCE`）：

- **live（当前主线）**：动态闭环（spec v1.1）——mock_server 持续演算可注入故障的微型世界，
  data_collector 双通道采集，backend 定时扫描 + 治理 check 列表 + Action 反馈，
  前端运维控制台一键注入故障/确认治理
- **static（遗留演示，代码默认值）**：静态数据集 `data/data`，一次性采集，11 条预埋风险对账演示

完整设计见 [docs/架构设计方案Spec.md](docs/架构设计方案Spec.md) 与 [docs/闭环执行方案Spec.md](docs/闭环执行方案Spec.md)；
Harness 演进全过程见 [docs/harness-改进方案.md](docs/harness-改进方案.md)。

> 📊 **量化评测结果**见 [docs/量化评测报告.md](docs/量化评测报告.md)：
> 12 个故障场景下风险扫描 P/R/F1 均 100%、零串报、负样本零误报；
> 评测同时查出并修复了 3 个真实缺陷（含一个会导致排障结论系统性错误的 bug）。
>
> 👉 **想快速理解整个项目（业务动因 + 当前技术全貌）**，看
> **[docs/项目总览-业务与技术.md](docs/项目总览-业务与技术.md)** —— 一篇读完就能掌握
> 为何要做、解决什么痛点、做成了什么、Harness 十大机制如何实现。

## 目录结构

```
data/data/                # 静态 Mock 数据集（static 模式用）
docs/                     # 架构方案 + 动态闭环 spec
mock_server/              # :9001 世界状态模拟器（live 模式数据源）
  app/world_def.py        #   300 服务+2×RDS+Redis、603 实例、100 节点、74 接口、412 边，预埋缺陷+阈值对照表
                          #   （核心 8 服务手写，其余生成式扩展；扩容守超卖率/零新增缺陷/接口真值隔离三约束）
  app/engine.py           #   5s/拍状态机：基线+噪声+故障效应+动作效应
  app/faults.py           #   4 故障场景 + 反向 BFS 传播（每跳×0.6）
  app/actions.py          #   治理动作生效 + 半衰期指数恢复
  app/renderers/          #   CMS/SLS/K8s 阿里云格式渲染（快照由世界单向派生）
data_collector/           # 独立采集进程：定时 60s（拓扑类，高水位增量）+ 实时 10s（水位/容量/带宽）
backend/                  # :8000 Python FastAPI + Agent Harness
  app/harness/            #   16 模块：llm / intent / scheduler / loop / context / memory
                          #   + permissions·approvals·audit（三态权限门禁与审计）
                          #   + verifier（结论事实核对）+ runctx（预算/中断/续跑）
                          #   + skills·tool_results·retention·background（定时扫描）
  app/agents/             #   8 个专职 Agent（orchestrator/general/data/topology/risk/diagnose/capacity/dbops），5 个可被主 Agent 派发
  app/skills/             #   8 篇 Skill（含 risk_governance 下 4 篇规则细则，三层渐进式披露）
  app/tools/              #   22 工具（数据查询 / 拓扑 / 风险 / 治理 check 流 / dispatch_agent / update_plan）
  app/rules/builtin.py    #   13 类规则：live 加 5min 滑窗 + 按实例分组；static 全表语义不变
  app/providers/          #   mock_aliyun（static）/ mock_control（Action 反馈）
frontend/                 # :5173 React 18 + antd + G6 + @ant-design/plots
                          #   智能对话 Tab + 运维控制台 Tab（故障注入/实时图表/治理确认）+ Skill/Agent 资产面板
docker-compose.yml        # 本地 MySQL 8（可选）
```

## 快速开始

```bash
# 0. 后端依赖（首次）
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # 填百炼 API Key 与数据库密码（见下表）

# —— static 模式（静态演示，两进程）——
cd backend && .venv/bin/uvicorn app.main:app --port 8000
cd frontend && npm install && npm run dev          # http://localhost:5173

# —— live 模式（动态闭环，四进程，按顺序启动；乱序也能最终一致）——
cd mock_server && ../backend/.venv/bin/uvicorn app.main:app --port 9001    # 1. 世界模拟器
cd data_collector && cp .env.example .env \
  && ../backend/.venv/bin/python -m collector.main                         # 2. 采集器
cd backend && DATA_SOURCE=live .venv/bin/uvicorn app.main:app --port 8000 # 3. 后端
cd frontend && npm run dev                                                 # 4. 前端
```

### 配置项（`backend/.env`）

| 项 | 说明 |
| --- | --- |
| `DASHSCOPE_API_KEY` / `LLM_BASE_URL` | 阿里云百炼；Token 计划用户可直接从 `~/.bailian/config.json` 取 `api_key` 与 `base_url`（base_url 需拼上 `/compatible-mode/v1`） |
| `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | 团队阿里云 RDS MySQL，密码向管理员索取。**多人并行开发每人用独立库**（lin 分支默认 `ai_devops_cmdb_lin`）。密码含 `#` 等特殊字符时代码自动 URL 转义 |
| `DATA_SOURCE` | `static`（默认）/ `live`；live 启用定时扫描与控制台，规则切换为 5min 滑窗 + 按实例分组。**两种模式的集群规模不同**：static 读 `data/data/*.json` 快照（8 服务 / 19 实例 / 15 接口，早期生成、未随扩容重新导出）；live 由 mock_server 实时演算（**300 服务 / 603 实例 / 100 节点 / 74 接口**）。评测与本文的规模数字均指 live |
| `MOCK_DATA_DIR` | 默认 `../data/data`（仅 static 模式使用） |

两道降级保底，断网/无权限也能演示：

- **未配 API Key** → 自动进入离线降级模式（关键词路由 + 脚本化流程），四条业务流仍可完整跑
- **数据库连不上** → 自动降级本地 SQLite（`backend/aiops.db`），表结构与代码完全一致

## 演示动线

**static 模式（对话驱动）**：依次输入页面内置快捷指令（下面的节点/风险条数对应 static 快照的 8 服务规模，不是 live 的 300 服务）：

1. `采集集群可观测数据` —— 7 张表入库，行数与 `world_manifest.json` 对账
2. `梳理服务拓扑` —— G6 拓扑图（8 节点 9 边），病灶边标红
3. `做一次全面风险扫描` —— 11 条风险报告，每条带证据与治理建议
4. `用户反馈下单接口很慢，帮我定位` —— 根因链 API-001 ← DB-002 ← DB-001
5. `按你给的方案执行治理，然后复扫验证` —— 风险转 resolved

**live 模式（控制台驱动，压轴闭环）**：切到「🎛️ 运维控制台」Tab：

1. 稳态：实时图表平稳，风险面板只有 7 条预埋基线 finding
2. 点「慢查询风暴」注入 → ≤2 分钟内扫描报告出现 +N 新增（DB-002/DB-001/API-001，
   实例精确到 rds-mysql-order，另一台 RDS 不误报），图表曲线抬升
3. 「生成治理方案」→ LLM 输出根因分析 + check 列表 → 勾选确认执行
4. Action 自动传回 mock → 世界恢复 → 指标回落 → 复扫 resolved（纯数据驱动，非标记硬置）
5. 对话问「当前系统有什么风险」—— Agent 基于 live 数据回答，自动标注数据窗口

## 验收基准

**static 模式**（以 `data/data/world_manifest.json` 为标准答案）：

- 采集行数：metrics 3904 / ingress 11939 / trace 9246 / app 395 / slow 50 / events 40 / k8s 35
- 拓扑 9 边与 `topology_ground_truth` 一致；风险 11 条精准命中不多报不少报；
  全量治理后 0 open / 11 resolved；诊断命中根因链并给出复合索引方案

**live 模式**（以 mock_server `/control/threshold_table` 阈值对照表为基准）：

- 稳态：7 条预埋配置缺陷精准命中，数据类规则零误报（含低流量接口防抖动）
- 注入 rds_conn_spike 后 ≤2 扫描周期 DB-001/CAP-004/API-001 告警，
  resource_ref 精确到故障实例（双 RDS 不串报）
- 治理执行 → mock 收到 Action → 指标自然回落（conn 92%→54%）→ 窗口滑过后全部 resolved
- 浏览器 E2E：控制台完整闭环 8 步验收通过；static 回归：11 条基准不变

> static 模式治理为模拟执行（改库内快照）；live 模式治理经 Action 反馈真实改变 mock 世界状态。
> 对接真实环境仅需替换 collector 的数据源客户端与治理工具实现。

## 工程质量

这不是一个只能跑 demo 的原型，Agent Harness 的每一项机制都有常驻用例与真机实测托底：

- **350 项单测，一条命令跑完**：`bash scripts/run_tests.sh`（backend 315 + mock_server 19 + collector 16 + 前端 tsc/build），含数据库隔离静态自检；`.gitlab-ci.yml` 一键接入
- **权限安全**：readonly/confirm/auto 三态门禁 + 阻塞式确认 + 全量审计；auto 白名单而非黑名单，新增写工具不会被静默放行
- **并行子 Agent**：上下文隔离 + 预算独立上限但消耗向父归集；中断/超预算时带回部分结论（零 LLM 调用）
- **结论事实核对**：基于“模型实际看到过的文本”建证据池，对凭空标识符/近似资源名报警，数值只统计不误报
- **分级上下文压缩**：L1 零成本微压缩 → L2 快模型摘要 → L3 紧急压缩，数值保真兜底
- **任务清单与分段续跑**：`update_plan` 工具 + 三道闸门（预算优先 → 次数上限 → 结构化未完成信号），续跑只重置步数、绝不重置预算
- **两道降级保底**：未配 API Key → 离线关键词路由；数据库连不上 → 自动降级本地 SQLite

Harness 从 0 到当前的完整演进记录（含每一步的真机验证与重大决策）见 [docs/harness-改进方案.md](docs/harness-改进方案.md)。

