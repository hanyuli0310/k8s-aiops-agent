"""L1 量化评测：**不依赖 mock_server**，跑完就有数字。

评测什么（全部客观判定）：
  A. 意图路由准确率      —— 对照人工标注（cases.INTENT_CASES）
  B. 数据查询准确率      —— 真值当场从 DB 算，判"回答里有没有那个数"
  C. 结论幻觉率          —— 复用 verifier 的 A 类检查
  D. 耗时 / token / 花费 —— 逐条记录
  E. 结论稳定性          —— 同一问题重复跑，看关键数字是否一致

不评测什么（判不准就别装作能判）：
  · 回答的"表达质量""是否完整" —— 没有机械判定口径
  · 人工排障耗时对照组       —— 没有真人计时数据，只能按明示假设估算

用法：
    cd backend && .venv/bin/python -m eval.run_l1
    cd backend && .venv/bin/python -m eval.run_l1 --skip-agent   # 只跑意图路由（快）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app import config, db                                      # noqa: E402
from app.harness import intent, llm, scheduler, verifier        # noqa: E402
from app.harness.runctx import RunContext                       # noqa: E402
from app.tools import registry                                  # noqa: E402
from eval import cases, metrics                                 # noqa: E402


def _hr(title: str):
    print(f"\n{'═' * 74}\n{title}\n{'═' * 74}")


# ══════════════════════════════════════════════════════════
# A. 意图路由准确率
# ══════════════════════════════════════════════════════════

def eval_intent_routing() -> dict:
    _hr("A. 意图路由准确率（对照人工标注）")
    right, wrong = 0, []
    per_intent: dict = {}
    t0 = time.perf_counter()

    routers: dict = {}
    for text, expected in cases.INTENT_CASES:
        got = intent.classify(text)
        got_intent = got.get("intent")
        # router 字段能区分"模型判的"还是"关键词兜底判的"——降级路径的准确率
        # 要单独看，否则模型挂掉时这项指标会虚高（关键词规则恰好也能答对一部分）
        routers[got.get("router", "?")] = routers.get(got.get("router", "?"), 0) + 1
        ok = got_intent == expected
        st = per_intent.setdefault(expected, {"n": 0, "ok": 0})
        st["n"] += 1
        if ok:
            right += 1
            st["ok"] += 1
        else:
            wrong.append({"text": text, "expected": expected, "got": got_intent,
                          "router": got.get("router")})

    total = len(cases.INTENT_CASES)
    elapsed = time.perf_counter() - t0
    print(f"总体：{right}/{total} = {right / total * 100:.1f}%"
          f"（{elapsed:.1f}s，{elapsed / total:.2f}s/条）\n")
    print(f"{'意图':16s} {'正确/总数':>10s}  准确率")
    for name in sorted(per_intent):
        st = per_intent[name]
        print(f"  {name:14s} {st['ok']:>4d}/{st['n']:<5d}  "
              f"{st['ok'] / st['n'] * 100:5.1f}%")
    if wrong:
        print(f"\n判错的 {len(wrong)} 条（这些是改进入口）：")
        for w in wrong:
            print(f"  ✗ 「{w['text']}」 期望 {w['expected']} → 实际 {w['got']}")
    print(f"\n判定来源分布：{routers}"
          f"（keyword 占比高说明模型侧在降级，此时准确率不代表模型能力）")
    return {"total": total, "right": right, "accuracy": right / total,
            "per_intent": per_intent, "wrong": wrong, "routers": routers}


# ══════════════════════════════════════════════════════════
# B+C+D. 数据查询准确率 / 幻觉 / 成本
# ══════════════════════════════════════════════════════════

def _run_once(session: str, question: str) -> dict:
    """跑一轮完整对话，回收回答与全部可观测量。"""
    run = RunContext(session_id=session, mode="readonly")
    t0 = time.perf_counter()
    answer, tool_calls, verify = "", 0, None
    subagents = 0
    for ev in scheduler.handle_message(session, question, run=run):
        et = ev.get("type")
        if et == "tool_call":
            tool_calls += 1
        elif et == "subagent_start":
            subagents += 1
        elif et == "answer":
            answer = ev.get("text") or answer
        elif et == "verify_warning":
            verify = ev
    return {
        "answer": answer,
        "seconds": round(time.perf_counter() - t0, 1),
        "tool_calls": tool_calls,
        "subagents": subagents,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "cost_cny": run.usage_event()["est_cost_cny"],
        "verify_warning": verify,
    }


def eval_data_queries(routing: str = None) -> dict:
    label = f"（调度={routing}）" if routing else ""
    _hr(f"B. 数据查询准确率（真值当场从数据库算）{label}")
    orig = config.AGENT_ROUTING
    if routing:
        config.AGENT_ROUTING = routing
    rows, hits = [], 0
    try:
        for c in cases.QUERY_CASES:
            truth = db.fetch_one(c["truth_sql"])["v"]
            tag = f"eval-q-{c['id']}" + (f"-{routing}" if routing else "")
            r = _run_once(tag, c["question"])
            ok = metrics.hits_number(r["answer"], float(truth))
            hits += int(ok)
            rows.append({**c, "truth": truth, "ok": ok, "routing": routing or orig, **r})
            mark = "✅" if ok else "❌"
            print(f"  {mark} {c['id']:22s} 真值={truth:<6} "
                  f"{r['seconds']:>5.1f}s {r['tool_calls']:>2d}工具 "
                  f"¥{r['cost_cny']:.4f}")
            if not ok:
                print(f"      回答摘要：{(r['answer'] or '（空）')[:110]}")
    finally:
        config.AGENT_ROUTING = orig
    n = len(cases.QUERY_CASES)
    print(f"\n准确率：{hits}/{n} = {hits / n * 100:.0f}%")
    return {"total": n, "hits": hits, "accuracy": hits / n, "rows": rows}


def eval_stability() -> dict:
    _hr("E. 结论稳定性（同一问题重复跑）")
    c = cases.STABILITY_CASE
    truth = db.fetch_one(c["truth_sql"])["v"]
    seen, runs = [], []
    for i in range(c["repeats"]):
        r = _run_once(f"eval-stab-{i}", c["question"])
        ok = metrics.hits_number(r["answer"], float(truth))
        seen.append(ok)
        runs.append(r)
        print(f"  第 {i + 1} 次：{'命中' if ok else '未命中'} 真值 {truth}  "
              f"{r['seconds']:>5.1f}s {r['tool_calls']}工具 ¥{r['cost_cny']:.4f}")
    consistent = len(set(seen)) == 1
    print(f"\n一致性：{'✅ 三次结论一致' if consistent else '❌ 三次结论不一致'}"
          f"（命中情况 {seen}）")
    return {"truth": truth, "hits": seen, "consistent": consistent, "runs": runs}


# ══════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════

def summarize(res: dict):
    _hr("汇总")
    q = res.get("queries")
    intent_r = res.get("intent")
    stab = res.get("stability")

    print(f"{'指标':28s} {'结果':>12s}   口径")
    print("-" * 74)
    if intent_r:
        print(f"{'意图路由准确率':26s} {intent_r['accuracy'] * 100:>11.1f}%   "
              f"对照 {intent_r['total']} 条人工标注")
    if q:
        print(f"{'数据查询准确率':26s} {q['accuracy'] * 100:>11.0f}%   "
              f"{q['total']} 条，真值由 SQL 当场算出")
        secs = [r["seconds"] for r in q["rows"]]
        tcs = [r["tool_calls"] for r in q["rows"]]
        costs = [r["cost_cny"] for r in q["rows"]]
        toks = [r["tokens_in"] + r["tokens_out"] for r in q["rows"]]
        n = len(secs)
        print(f"{'平均端到端耗时':26s} {sum(secs) / n:>10.1f}s   "
              f"区间 {min(secs):.1f}~{max(secs):.1f}s")
        print(f"{'平均工具调用次数':26s} {sum(tcs) / n:>11.1f}   "
              f"区间 {min(tcs)}~{max(tcs)}")
        print(f"{'平均单次花费':26s} {'¥' + format(sum(costs) / n, '.4f'):>12s}   "
              f"平均 {sum(toks) / n:,.0f} tokens")
        warned = sum(1 for r in q["rows"] if r["verify_warning"])
        print(f"{'幻觉 A 类报警':26s} {warned:>8d}/{n}   "
              f"verifier 对回答做事实核对")
    if stab:
        print(f"{'结论稳定性':26s} "
              f"{'一致' if stab['consistent'] else '不一致':>12s}   "
              f"同一问题跑 {len(stab['hits'])} 次")

    # 人工基线估算（明示假设）
    if q:
        avg_sec = sum(r["seconds"] for r in q["rows"]) / len(q["rows"])
        avg_tc = sum(r["tool_calls"] for r in q["rows"]) / len(q["rows"])
        print(f"\n【人工基线估算 —— 估算而非实测对照组】")
        print(f"  任务类型：simple_query（一句话能查到的事实）")
        print(f"  ⚠️ 这类任务【不适合用来宣称提速】：人工打开控制台看一眼也就一两分钟，")
        print(f"     Agent 在这里的价值是自然语言接口与免去记 SQL，不是速度。")
        print(f"     真正体现耗时优势的是多步排障 —— 见 L2 评测。")
        rng = metrics.speedup_range(avg_sec, int(round(avg_tc)),
                                    systems_touched=1, task_type="simple_query")
        for label, v in rng.items():
            print(f"  {label:4s}（{v['per_query_min']} 分/次查询）："
                  f"人工约 {v['manual_minutes']:>4.1f} 分钟 → "
                  f"Agent {avg_sec:.1f}s，提速 {v['speedup']}×")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-agent", action="store_true",
                    help="只跑意图路由（不发起完整对话，快速自检）")
    ap.add_argument("--compare-routing", action="store_true",
                    help="数据查询在 model / intent 两种调度下各跑一遍，对比准确率与开销"
                         "（验证“只读计数被误判成扫描”在 model 路由下是否消解）")
    ap.add_argument("--json", help="把结果写到指定 JSON 文件")
    args = ap.parse_args()

    db.init_db()
    registry.ensure_loaded()

    print(f"数据源模式 : {config.DATA_SOURCE}")
    print(f"LLM 可用   : {llm.available()}（主 {config.LLM_MODEL} / 快 {config.LLM_MODEL_FAST}）")
    print(f"预算上限   : tokens={config.RUN_MAX_TOKENS or '不限制'} "
          f"wall={config.RUN_MAX_WALL_S or '不限制'}")
    print(f"顶层调度   : {config.AGENT_ROUTING}")
    if not llm.available():
        print("\n⚠️ LLM 不可用，Agent 相关指标无法评测（意图路由会走关键词降级）")

    res = {"intent": eval_intent_routing()}
    if not args.skip_agent:
        if args.compare_routing:
            res["queries_model"] = eval_data_queries("model")
            res["queries_intent"] = eval_data_queries("intent")
            res["queries"] = res["queries_model"]      # 汇总默认看 model
            _compare_routing(res["queries_model"], res["queries_intent"])
        else:
            res["queries"] = eval_data_queries()
            res["stability"] = eval_stability()
    summarize(res)

    if args.json:
        Path(args.json).write_text(
            json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")


def _compare_routing(m: dict, i: dict):
    """数据查询在两种调度下的对比 —— 验证报告 §7 待优化项“只读计数被判成扫描”。

    旧 intent 路由下，“风险有几条”含“风险”关键词，容易被分类到 risk_scan
    （触发全量扫描而非查计数）；model 路由直接交给编排 Agent，不经意图分类。
    看两件事：准确率是否提升、工具调用次数是否下降（扫描比查计数重得多）。
    """
    _hr("数据查询：model vs intent 调度对比")
    def _agg(rows):
        n = len(rows)
        return (sum(r["tool_calls"] for r in rows) / n,
                sum(r["seconds"] for r in rows) / n,
                sum(r["cost_cny"] for r in rows) / n)
    print(f"{'调度':8s} {'准确率':>8s} {'均工具次数':>10s} {'均耗时':>8s} {'均花费':>10s}")
    for name, r in (("model", m), ("intent", i)):
        tc, sec, cost = _agg(r["rows"])
        print(f"{name:8s} {r['hits']}/{r['total']:<6} {tc:>10.1f} {sec:>7.1f}s ¥{cost:>8.4f}")
    # 逐用例工具次数：扫描会显著多于查计数
    print(f"\n逐用例工具调用次数（model / intent）：")
    mi = {r["id"]: r for r in i["rows"]}
    for r in m["rows"]:
        j = mi.get(r["id"], {})
        print(f"  {r['id']:22s} {r['tool_calls']:>2d} / {j.get('tool_calls', '?')}"
              f"  ({'✅' if r['ok'] else '❌'} / {'✅' if j.get('ok') else '❌'})")


if __name__ == "__main__":
    main()
