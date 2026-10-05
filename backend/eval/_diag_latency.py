"""排障耗时构成诊断：每轮 LLM 往返 vs 工具执行 vs 并行利用率。

优化前必须先知道时间花在哪 —— 896s/54 次工具调用看着像"工具慢"，
但工具是本地 DB 查询，真正的开销可能全在 LLM 往返上。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.harness import llm, scheduler
from app.harness.runctx import RunContext
from app.tools import registry

db.init_db(); registry.ensure_loaded()

# 给 llm.chat_with_retry 加计时探针（不改生产代码）
orig_chat = llm.chat_with_retry
stats = {"llm_calls": 0, "llm_seconds": 0.0, "per_call": [], "detail": []}

def timed(messages, **kw):
    import json as _j
    in_chars = sum(len(str(m.get("content") or "")) +
                   sum(len(_j.dumps(tc.get("function") or {}, ensure_ascii=False))
                       for tc in (m.get("tool_calls") or []))
                   for m in messages)
    t0 = time.perf_counter()
    msg = None
    try:
        msg = orig_chat(messages, **kw)
        return msg
    finally:
        d = time.perf_counter() - t0
        out_chars = len(str(getattr(msg, "content", "") or "")) if msg else 0
        n_tc = len(getattr(msg, "tool_calls", None) or []) if msg else 0
        stats["llm_calls"] += 1
        stats["llm_seconds"] += d
        stats["per_call"].append(round(d, 2))
        # 关键：把「输入长度 / 输出长度 / 耗时」摆在一起，才能判断谁是主因
        stats["detail"].append({"sec": round(d, 1), "in_kchars": round(in_chars / 1000, 1),
                                "out_chars": out_chars, "tools": n_tc,
                                "model": kw.get("model")})

llm.chat_with_retry = timed

q = "用户反馈下单接口很慢，帮我定位根因，说明是哪个组件的问题"
run = RunContext(session_id="diag-lat", mode="readonly")
t0 = time.perf_counter()
rounds = []           # 每轮的工具数
cur_round = 0
tool_total = 0
for ev in scheduler.handle_message("diag-lat", q, run=run):
    t = ev.get("type")
    if t == "thinking":
        if cur_round:
            rounds.append(cur_round)
        cur_round = 0
    elif t == "tool_call":
        cur_round += 1
        tool_total += 1
if cur_round:
    rounds.append(cur_round)
total = time.perf_counter() - t0

print(f"\n{'='*66}")
print(f"总耗时           {total:.1f}s")
print(f"LLM 往返         {stats['llm_calls']} 次，累计 {stats['llm_seconds']:.1f}s "
      f"（占总时长 {stats['llm_seconds']/total*100:.0f}%）")
print(f"  平均每次       {stats['llm_seconds']/max(1,stats['llm_calls']):.1f}s")
print(f"  最慢/最快      {max(stats['per_call']):.1f}s / {min(stats['per_call']):.1f}s")
print(f"工具调用         {tool_total} 次，分布在 {len(rounds)} 轮")
print(f"  每轮工具数     {rounds}")
par = sum(1 for r in rounds if r > 1)
print(f"  并行轮次       {par}/{len(rounds)} = {par/max(1,len(rounds))*100:.0f}%"
      f"（>1 个工具的轮次才有并行）")
print(f"  平均每轮       {tool_total/max(1,len(rounds)):.2f} 个工具")
print(f"非 LLM 时间      {total-stats['llm_seconds']:.1f}s"
      f"（工具执行 + 编排开销）")
print(f"tokens           in={run.tokens_in:,} out={run.tokens_out:,} ¥{run.usage_event()['est_cost_cny']}")
print(f"\n每次 LLM 调用明细（判断耗时主因）:")
print(f"  {'#':>2s} {'耗时':>7s} {'输入':>9s} {'输出字符':>9s} {'工具':>4s} 模型")
for i, d in enumerate(stats["detail"], 1):
    print(f"  {i:>2d} {d['sec']:>6.1f}s {d['in_kchars']:>7.1f}k {d['out_chars']:>9d} "
          f"{d['tools']:>4d} {d['model'] or '-'}")
# 相关性：耗时与输入长度 / 输出长度谁更相关
import statistics as st
if len(stats["detail"]) > 2:
    secs = [d["sec"] for d in stats["detail"]]
    ins = [d["in_kchars"] for d in stats["detail"]]
    outs = [d["out_chars"] for d in stats["detail"]]
    def corr(xs, ys):
        mx, my = st.mean(xs), st.mean(ys)
        num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
        den = (sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys)) ** 0.5
        return num/den if den else 0
    print(f"\n  耗时 ~ 输入长度  相关系数 {corr(ins, secs):+.2f}")
    print(f"  耗时 ~ 输出长度  相关系数 {corr(outs, secs):+.2f}")
    print(f"  → 相关系数更高的那个才是优化重点")
print(f"{'='*66}")
