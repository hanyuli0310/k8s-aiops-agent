**English** | [中文](README.zh-CN.md)

# k8s-aiops-agent — Agentic AIOps for Kubernetes

> A conversational operations agent for production Kubernetes clusters. It covers the full loop: **data collection → topology analysis → risk scanning → root-cause diagnosis → remediation with audit**. The core is a self-written **Agent Harness** (a model-driven runtime, not workflow orchestration).

**Highlights** (numbers from the [evaluation report](docs/量化评测报告-最终版.md), written in Chinese)

- Rule-side Precision / Recall / F1 of **100%** across 12 injected fault scenarios (0 false positives, 0 false negatives)
- Still 100% after scaling the simulated cluster from 19 to 603 instances (32×); rule scan time 0.05 s
- Against a naive baseline (same model and tools, Harness removed): **-27% median tool calls** and **-20% median time** (results vary by scenario)
- Stale data is re-checked automatically and the user is told; destructive actions require explicit confirmation and are audited
- 350 regression tests plus a one-command CI runner; 13+ real defects found and fixed during evaluation

## Background

Built at the **Alibaba Cloud Intern AI Hackathon (2026)**.

---

# Project: Full-Loop AIOps Agent

An operations agent for a production Kubernetes cluster (`prod-cluster-01`, cn-hangzhou). Through conversation it runs **data collection → topology analysis → risk scanning → fault localization → remediation loop**.

The core is a **self-written Agent Harness**, applying coding-agent engineering practices to operations:
model-driven orchestration, a ReAct loop, a three-state permission gate with audit, parallel sub-agents, fact checking of conclusions (including freshness), tiered context compression, task plans with resumable runs, and short- and long-term memory. There are 8 skills (three-layer progressive disclosure), 8 specialist agents (5 of which can be dispatched by the main agent), 22 tools, and 13 risk-rule categories.
It is backed by **350 regression tests and a one-command CI runner**. The mechanisms were tested on a real model (Qwen family). A **quantitative evaluation** is documented in [the final report](docs/量化评测报告-最终版.md) (ground truth, data, criteria, results, reproduction) and in a [process log](docs/量化评测报告.md) (how the experiments evolved, including the defects found).

Two data modes (`DATA_SOURCE` in `backend/.env`):

- **live (current main path)**: a closed loop. `mock_server` simulates a small world where faults can be injected, `data_collector` collects data through two channels, and the backend scans periodically, produces a remediation checklist, and gets feedback from actions. A web console injects faults and confirms remediation.
- **static (legacy demo, code default)**: a static dataset in `data/data`, collected once, with 11 pre-seeded risks.

Full design: [architecture spec](docs/架构设计方案Spec.md) and [closed-loop spec](docs/闭环执行方案Spec.md). The full history of the Harness is in [docs/harness-改进方案.md](docs/harness-改进方案.md).

> The `docs/` folder is written in Chinese. The most useful entry points are [docs/项目总览-业务与技术.md](docs/项目总览-业务与技术.md) (project overview: motivation, technical design, the ten Harness mechanisms) and [the final evaluation report](docs/量化评测报告-最终版.md).

## Project structure

```
data/data/                # static mock dataset (used by static mode)
docs/                     # architecture docs and closed-loop spec (Chinese)
mock_server/              # :9001 world-state simulator (data source for live mode)
  app/world_def.py        #   300 services + 2 RDS + Redis, 603 instances, 100 nodes, 74 APIs, 412 edges, seeded defects and threshold table
  app/engine.py           #   5 s tick state machine: baseline + noise + fault effects + action effects
  app/faults.py           #   4 fault scenarios + reverse BFS propagation (×0.6 per hop)
  app/actions.py          #   remediation actions + half-life exponential recovery
  app/renderers/          #   CMS / SLS / K8s output in Alibaba Cloud formats
data_collector/           # separate collector process: topology every 60 s (incremental) + real-time metrics every 10 s
backend/                  # :8000 Python FastAPI + Agent Harness
  app/harness/            #   16 modules: llm / intent / scheduler / loop / context / memory
                          #   + permissions, approvals, audit (three-state permission gate)
                          #   + verifier (fact checking) + runctx (budget, interrupt, resume)
                          #   + skills, tool_results, retention, background (scheduled scans)
  app/agents/             #   8 specialist agents
  app/skills/             #   8 skills (including 4 detailed risk-governance rule files)
  app/tools/              #   22 tools (data queries / topology / risk / governance checks / dispatch_agent / update_plan)
  app/rules/builtin.py    #   13 risk-rule categories
  app/providers/          #   mock_aliyun (static) / mock_control (action feedback)
frontend/                 # :5173 React 18 + antd + G6 + @ant-design/plots
                          #   chat tab + operations console tab (fault injection, live charts, remediation confirmation)
docker-compose.yml        # local MySQL 8 (optional)
```

## Quick start

```bash
# 0. Backend dependencies (first time)
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in your LLM API key and database settings (see below)

# — static mode (two processes) —
cd backend && .venv/bin/uvicorn app.main:app --port 8000
cd frontend && npm install && npm run dev          # http://localhost:5173

# — live mode (four processes, start in this order) —
cd mock_server && ../backend/.venv/bin/uvicorn app.main:app --port 9001    # 1. world simulator
cd data_collector && cp .env.example .env \
  && ../backend/.venv/bin/python -m collector.main                         # 2. collector
cd backend && DATA_SOURCE=live .venv/bin/uvicorn app.main:app --port 8000 # 3. backend
cd frontend && npm run dev                                                 # 4. frontend
```

### Configuration (`backend/.env`)

| Key | Description |
| --- | --- |
| `DASHSCOPE_API_KEY` / `LLM_BASE_URL` | Alibaba Cloud Bailian (DashScope, OpenAI-compatible endpoint). The base URL must end with `/compatible-mode/v1` |
| `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | MySQL connection. Use a separate `DB_NAME` per environment so collectors do not overwrite each other. Special characters in passwords are URL-escaped automatically |
| `DATA_SOURCE` | `static` (default) or `live`. Live enables scheduled scans and the console, and switches rules to a 5-minute sliding window grouped by instance. The two modes have different scales: static reads a snapshot (8 services, 19 instances, 15 APIs); live is simulated by `mock_server` (300 services, 603 instances, 100 nodes, 74 APIs). The evaluation numbers refer to live |
| `MOCK_DATA_DIR` | Default `../data/data` (static mode only) |

Two fallbacks keep the demo running without network access or credentials:

- **No API key** → offline mode (keyword routing + scripted flows); all four business flows still run
- **Database unreachable** → automatic fallback to a local SQLite database (`backend/aiops.db`), with the same schema

## Demo flow

**Static mode (chat-driven)**. Enter the built-in shortcuts in order. The node and risk counts refer to the 8-service static snapshot, not the 300-service live world.

1. `采集集群可观测数据` — collect observability data into 7 tables, reconciled against `world_manifest.json`
2. `梳理服务拓扑` — G6 topology graph (8 nodes, 9 edges), faulty edges in red
3. `做一次全面风险扫描` — full risk scan: 11 findings, each with evidence and remediation advice
4. `用户反馈下单接口很慢，帮我定位` — root-cause chain API-001 ← DB-002 ← DB-001
5. `按你给的方案执行治理，然后复扫验证` — remediation, then rescan, risks become resolved

(The shortcuts are in Chinese, matching the UI.)

**Live mode (console-driven, the closing loop)**. Open the operations console tab:

1. Steady state: live charts are flat, and the risk panel shows only the 7 pre-seeded baseline findings
2. Click **slow-query storm** to inject a fault. Within 2 minutes the scan report shows new findings (DB-002, DB-001, API-001). The instance is precisely `rds-mysql-order`; the other RDS is not falsely flagged. The chart curves rise
3. **Generate remediation plan** → the LLM outputs a root-cause analysis and a check list → confirm and execute
4. The action is sent back to the mock world → the world recovers → metrics fall → the rescan marks the risks resolved (driven by data, not by flags)
5. Ask in chat "what risks does the system have now?" The agent answers from live data and labels the data window

## Acceptance criteria

**Static mode** (ground truth: `data/data/world_manifest.json`)

- Collected rows: metrics 3904, ingress 11939, trace 9246, app 395, slow 50, events 40, k8s 35
- Topology matches the 9 ground-truth edges; exactly 11 risks are found, with no false positives or misses; after full remediation there are 0 open and 11 resolved; diagnosis finds the root-cause chain and proposes a composite index

**Live mode** (threshold table at `/control/threshold_table` in `mock_server` is the reference)

- Steady state: the 7 pre-seeded configuration defects are found exactly; data-based rules have zero false positives (including low-traffic APIs)
- After injecting `rds_conn_spike`, DB-001 / CAP-004 / API-001 alert within 2 scan cycles, and `resource_ref` points to the faulty instance (no cross-reporting between the two RDS)
- Remediation → mock receives the action → metrics recover naturally (connections 92% → 54%) → all risks resolved once the window slides past
- Browser end-to-end: the full console loop passes all 8 acceptance steps. Static regression: the 11 baseline results are unchanged

> In static mode, remediation is simulated by modifying the snapshot in the database. In live mode, remediation actions change the state of the mock world through action feedback. Connecting to a real environment only requires replacing the collector's data-source clients and the remediation tool implementations.

## Engineering quality

This is not a demo-only prototype. Each Harness mechanism is backed by regression tests and real-model runs:

- **350 unit tests in one command**: `bash scripts/run_tests.sh` (backend 315, mock_server 19, collector 16, frontend tsc/build), including a database-isolation self-check. `.gitlab-ci.yml` wires it into CI
- **Permission safety**: readonly / confirm / auto gate, blocking confirmation, full audit. Auto mode uses an allowlist, not a denylist, so new write tools are not silently allowed
- **Parallel sub-agents**: isolated context, separate budget limits, with consumption rolled up to the parent. On interruption or budget exhaustion, partial conclusions are returned (no extra LLM calls)
- **Conclusion fact checking**: an evidence pool is built from the text the model actually saw. Invented identifiers and near-matching resource names are flagged; numbers are checked without false alarms
- **Tiered context compression**: L1 zero-cost micro-compression → L2 fast-model summary → L3 emergency compression, with numeric fidelity safeguards
- **Task plans and resumable runs**: `update_plan` tool with three gates (budget first → step limit → structured unfinished signal). Resuming resets the step count but never the budget
- **Two fallbacks**: no API key → offline keyword routing; database unreachable → local SQLite

For the complete Harness evolution log, including the real-model verification and key decisions at each step, see [docs/harness-改进方案.md](docs/harness-改进方案.md) (Chinese).
