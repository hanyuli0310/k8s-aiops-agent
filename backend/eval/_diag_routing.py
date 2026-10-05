"""真机验证顶层调度：模型自主编排 vs 预制意图硬映射。

为什么必须真机跑：单测只能证明"代码走到了编排 Agent"，证明不了**模型真的会
自己决定派几个子 Agent、以及并行还是串行**。提示词里写了"独立子任务同一轮里并行派"，
但模型听不听是另一回事 —— 不实测就断言"调度权已交给模型"，等于把提示词当成了效果。

观测三件事（都从事件流里机械统计，不靠读回答判断）：
  1. 派了几个子 Agent、类型分别是什么；
  2. 是否**并行**（同一轮里发出多个 dispatch_agent → 事件流里连续出现且时间重叠）；
  3. 端到端耗时 / token / 成本，与 intent 模式对照。

用法（会真实调用 LLM 花钱）：
    cd backend && .venv/bin/python -m eval._diag_routing
    ... --only 1            # 只跑第 1 个用例
    ... --routing intent    # 只跑旧模式
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app import config, db                                        # noqa: E402
from app.harness import scheduler                                 # noqa: E402
from app.harness.runctx import RunContext                         # noqa: E402
from app.tools import registry                                    # noqa: E402

# 刻意挑「跨越多个预制意图」的请求 —— 这正是意图硬映射的失效区。
CASES = [
    {
        "id": "cross_intent",
        "q": "先梳理一下服务拓扑，同时做一次风险扫描，两件事的结果一起给我",
        "why": "跨 topology + risk_scan 两个意图。旧路由只能选一个 Agent；"
               "两件事互不依赖，理想做法是同一轮并行派两个子 Agent",
        "expect_parallel": True,
    },
    {
        "id": "simple_query",
        "q": "两个 MySQL 实例的连接使用率分别是多少？",
        "why": "简单查询。理想做法是自己查（可并行两次 query_metrics），"
               "**不该**派子 Agent —— 派了就是多一层开销",
        "expect_parallel": False,
    },
    {
        "id": "no_intent_match",
        "q": "把当前所有 P1 风险按涉及的实例分组列出来，并说明哪些实例被多条风险同时命中",
        "why": "归不到任何预制意图（既不是纯查询也不是扫描/治理）。"
               "旧路由会落到兜底 general",
        "expect_parallel": False,
    },
]


def _run_one(q: str, routing: str) -> dict:
    orig = config.AGENT_ROUTING
    config.AGENT_ROUTING = routing
    run = RunContext(session_id=f"diag-routing-{routing}", mode="readonly")
    t0 = time.perf_counter()
    dispatches, tool_calls, answer, agent = [], [], "", None
    # 记录每个 dispatch_agent 事件的相对时刻，用于判断是否并行
    try:
        for ev in scheduler.handle_message(f"diag-routing-{routing}", q, run=run):
            et = ev.get("type")
            if et == "agent_start" and agent is None:
                agent = ev.get("agent")
            elif et == "tool_call":
                at = round(time.perf_counter() - t0, 2)
                tool_calls.append((ev.get("tool"), at, ev.get("depth", 0)))
                if ev.get("tool") == "dispatch_agent":
                    dispatches.append(((ev.get("args") or {}).get("subagent_type"), at))
            elif et == "answer":
                answer = ev.get("text") or answer
    finally:
        config.AGENT_ROUTING = orig

    # 并行判定：同一轮发出的调用会在极短时间内相继出现（都在同一次模型回复里）。
    # 用 0.5s 作为"同一轮"的界 —— 串行的话中间必然夹着一次完整的模型往返（实测数秒）。
    top_calls = [(n, at) for n, at, d in tool_calls if not d]
    parallel_groups = []
    for name, at in top_calls:
        if parallel_groups and at - parallel_groups[-1][-1][1] < 0.5:
            parallel_groups[-1].append((name, at))
        else:
            parallel_groups.append([(name, at)])
    max_group = max((len(g) for g in parallel_groups), default=0)

    return {
        "routing": routing, "agent": agent,
        "seconds": round(time.perf_counter() - t0, 1),
        "tool_calls": len(tool_calls),
        "top_level_calls": len(top_calls),
        "dispatches": dispatches,
        "max_parallel": max_group,
        "groups": [[n for n, _ in g] for g in parallel_groups],
        "tokens": run.tokens_in + run.tokens_out,
        "cost": run.usage_event()["est_cost_cny"],
        "answer_len": len(answer),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, help="只跑第 N 个用例（1 起）")
    ap.add_argument("--routing", choices=["model", "intent", "both"], default="both")
    args = ap.parse_args()

    db.init_db()
    registry.ensure_loaded()
    cases = [CASES[args.only - 1]] if args.only else CASES
    modes = ["model", "intent"] if args.routing == "both" else [args.routing]

    rows = []
    for c in cases:
        print(f"\n{'═' * 76}\n【{c['id']}】{c['q']}\n  考察点：{c['why']}")
        for routing in modes:
            r = _run_one(c["q"], routing)
            r["case"] = c["id"]
            r["expect_parallel"] = c["expect_parallel"]
            rows.append(r)
            disp = (", ".join(f"{t}@{at}s" for t, at in r["dispatches"])
                    or "（未派子 Agent）")
            print(f"  ─ {routing:6s} {r['agent'] or '?':20s} {r['seconds']:6.1f}s  "
                  f"{r['tool_calls']:2d} 次工具（顶层 {r['top_level_calls']}）  "
                  f"最大并行 {r['max_parallel']}  ¥{r['cost']:.4f}")
            print(f"      子 Agent: {disp}")
            print(f"      调用分组: {r['groups']}")

    print(f"\n{'═' * 76}\n汇总\n{'═' * 76}")
    for routing in modes:
        sub = [r for r in rows if r["routing"] == routing]
        if not sub:
            continue
        n = len(sub)
        print(f"{routing:6s} 平均 {sum(x['seconds'] for x in sub) / n:.1f}s、"
              f"{sum(x['tokens'] for x in sub) / n:.0f} tokens、"
              f"¥{sum(x['cost'] for x in sub) / n:.4f}、"
              f"派子 Agent {sum(len(x['dispatches']) for x in sub)} 次")
    # 期望并行的用例是否真的并行了 —— 这条才是项 3 的核心结论
    for r in rows:
        if r["routing"] == "model" and r["expect_parallel"]:
            ok = r["max_parallel"] >= 2
            print(f"\n{'✅' if ok else '❌'} [{r['case']}] 期望模型自主并行，"
                  f"实测最大并行度 {r['max_parallel']}"
                  + ("" if ok else " —— 提示词没起作用，需要调整或改机制"))


if __name__ == "__main__":
    main()
