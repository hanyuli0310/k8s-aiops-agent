"""E-4 误报率评估（手动工具，**不是**测试套件的一部分）。

verifier 的价值完全取决于误报率：一个会对正常回答报警的核对器是负资产。
本脚本跑真实对话，把 answer 与证据池都留下，逐条打印未命中项供人工判断。

⚠️ 与 tests/test_*.py 不同，本脚本**刻意连真实数据库、调真实模型** ——
它评估的就是"真机语料上的误报率"，用 SQLite 空库跑不出有意义的结论。
因此它以 `_` 开头、不叫 test_*，不会被 scripts/run_tests.sh 收集，
也不受"测试必须连 sqlite"那条纪律约束（它以 readonly 模式运行，只读不写）。

结论应当固化回 tests/test_harness_step10.py 的 `test_real_answer_*` 系列。

用法：
    cd backend && .venv/bin/python tests/_eval_verifier.py [提问...]
    # 长任务建议放宽预算：RUN_MAX_TOKENS=300000 RUN_MAX_WALL_S=420 MAX_AGENT_STEPS=8
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.agents.base import build_agent                          # noqa: E402
from app.harness import verifier                                 # noqa: E402
from app.harness.context import ContextManager                   # noqa: E402
from app.harness.loop import run_agent                           # noqa: E402
from app.harness.runctx import RunContext                        # noqa: E402
from app.tools import registry                                   # noqa: E402

QUESTIONS = [
    ("topology", "梳理服务调用拓扑，输出 mermaid 图和异常边清单"),
    ("capacity", "核算 default 命名空间的 CPU 超卖率，给出需要下调的毫核数与各服务当前 limit"),
]


def run_one(agent_key: str, question: str):
    print("=" * 70)
    print(f"[{agent_key}] {question}")
    print("=" * 70)

    # 复用 loop 但自己持有 ctx —— 需要拿到证据池做事后分析。
    # 直接调 run_agent 拿不到 ctx，所以这里关掉 loop 内置核对（避免自纠正干扰观测），
    # 事后自己核对一遍。
    from app import config
    old = config.VERIFY_ANSWER
    config.VERIFY_ANSWER = False

    captured = {}
    orig_init = ContextManager.__init__

    def spy(self, system_prompt, session_id="_"):
        orig_init(self, system_prompt, session_id)
        captured["ctx"] = self

    ContextManager.__init__ = spy
    try:
        run = RunContext(session_id=f"eval-{agent_key}", mode="readonly")
        answer, tools = "", 0
        for ev in run_agent(build_agent(agent_key, question), question, run=run):
            if ev["type"] == "answer":
                answer = ev["text"]
            elif ev["type"] == "tool_call":
                tools += 1
            elif ev["type"] in ("aborted", "error"):
                print("  [%s] %s" % (ev["type"], ev.get("reason") or ev.get("text")))
    finally:
        ContextManager.__init__ = orig_init
        config.VERIFY_ANSWER = old

    ctx = captured.get("ctx")
    if ctx is None or not answer:
        print("!! 没拿到 ctx 或 answer，跳过")
        return None

    vr = verifier.check(answer, ctx.evidence)
    print(f"\n工具调用 {tools} 次 · 回答 {len(answer)} 字符 · "
          f"证据池 {ctx.evidence.stats()}")
    print(f"核对了：{vr['checked']}")
    print(f"suspicious = {vr['suspicious']}  → {verifier.describe(vr)}")

    if vr["fake_ids"]:
        print("\n【A1 没见过的 hex 标识符】")
        for i in vr["fake_ids"]:
            print("  ", i)
    if vr["near_miss_resources"]:
        print("\n【A2 近似资源名】")
        for a, b, d in vr["near_miss_resources"]:
            print(f"   说了 {a!r}，池里最近的是 {b!r}（距离 {d}）")
    if vr["unverified_numbers"]:
        print(f"\n【B 未命中数值】共 {len(vr['unverified_numbers'])} 个"
              f"（核对了 {vr['checked']['numbers']} 个）")
        print("  ", ", ".join(vr["unverified_numbers"]))
        print("   ↑ 请人工判断：是模型算出来的/换算过的（正常），还是编的（真问题）")

    print("\n【回答片段】")
    print(answer[:600])
    print()
    return vr


def main():
    registry.ensure_loaded()
    qs = QUESTIONS
    if len(sys.argv) > 1:
        qs = [("general", " ".join(sys.argv[1:]))]
    results = []
    for key, q in qs:
        try:
            r = run_one(key, q)
            if r:
                results.append((key, r))
        except Exception as e:                                  # noqa: BLE001
            import traceback
            print(f"!! {key} 跑失败: {e}")
            traceback.print_exc()

    print("=" * 70)
    print("汇总")
    print("=" * 70)
    for key, r in results:
        print(f"{key:10} suspicious={r['suspicious']!s:5} "
              f"A1={len(r['fake_ids'])} A2={len(r['near_miss_resources'])} "
              f"B={len(r['unverified_numbers'])}/{r['checked']['numbers']}")
    bad = [k for k, r in results if r["suspicious"]]
    if not results:
        print("\n!! 一个场景都没跑出 answer（超预算/出错），本次评估无结论")
    elif bad:
        print(f"\n⚠️ 以下场景报警了，必须逐条确认是真问题还是误报：{bad}")
    else:
        print(f"\n✅ {len(results)} 个正常回答均未触发报警（A 类零误报）")


if __name__ == "__main__":
    sys.exit(main() or 0)
