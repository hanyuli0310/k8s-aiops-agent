"""朴素基线（HARNESS_PROFILE=naive）的公平性锁定测试。

## 为什么这套测试必须存在

评测报告里「相比朴素基线，根因定位准确率提升 X 个百分点」这句话，全部建立在
**两臂之间只有 Harness 机制这一个差异**的前提上。这个前提一旦被破坏，
数字就从"有说服力"变成"造假"，而且**不会有任何报错**：

  · 若 naive 臂少了某个业务工具 → 测到的是"工具少了所以差"，不是"没有 Harness 所以差"；
  · 若 naive 臂用了更弱的模型 → 测到的是模型差距；
  · 若 _NAIVE_PROMPT 里混进一句方法论（哪怕"注意先看上游"这种）→ 提升被低估，
    更糟的是别人无法判断这个数字到底测了什么；
  · 若运行时切到 naive 后事实核对没跟着关 → 基线臂偷偷带着 Harness 能力跑。

最后一条**真的发生过**：第一版把开关写成模块级 `if harness_is_naive(): VERIFY_ANSWER = False`，
而评测脚本是在同一进程内改 `config.HARNESS_PROFILE` 跑双臂的 —— 模块级只在导入时
求值一次，切到 naive 后核对依旧开着。改成 `verify_enabled()` 运行时判定才修掉。
所以这里专门有一项测运行时切换，而不只测导入时的默认值。

## 公平性边界（与 config.HARNESS_PROFILE 注释一致）

  两臂必须一致：模型、业务工具集、步数上限、集群环境描述
  只允许 naive 缺失：方法论（工作准则 + Skill）、事实核对、并行取数、
                     子 Agent、任务清单与续跑、长期记忆

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_baseline_fairness.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "baseline_fairness_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["DB_URL"] = f"sqlite:///{_TMP_DB}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config                                            # noqa: E402

# 运行时隔离闸门：环境变量可能被 .env 或既有进程状态覆盖，光设不够，必须导入后断言。
# 这套测试会 init_db() 建表，一旦连到真实库就是往生产库里写东西。
if not config.DATABASE_URL.startswith("sqlite"):
    raise SystemExit(
        f"❌ 测试中止：数据库未隔离，当前指向 "
        f"{config.DATABASE_URL.split('@')[-1]}。测试必须连本地 SQLite。")

# 触发全部工具注册（同 main.py 的导入方式），否则 get_spec 全返回 None，
# 并行判定会被误测成"两臂都串行"。
from app.tools import (agent_tools, data_tools, governance_tools,  # noqa: E402,F401
                       registry, risk_tools, topology_tools)
from app.agents import base                                       # noqa: E402
from app.harness import loop                                      # noqa: E402
from app import db                                                # noqa: E402

# full 臂组装提示词时会读长期记忆表（memory.memory_prompt），临时库必须先建表，
# 否则 build_agent 直接抛 no such table，测出的"两臂差异"全是假的。
db.init_db()

ORCH = config.ORCHESTRATOR_AGENT


class _FakeToolCall:
    """伪造 tool_call，只需要 .function.name 供 _partition 查 spec。"""

    def __init__(self, name: str):
        self.function = type("_F", (), {"name": name})()


def _with_profile(profile: str, fn):
    """在指定 profile 下执行 fn，结束后恢复 —— 避免污染后续用例。"""
    orig = config.HARNESS_PROFILE
    config.HARNESS_PROFILE = profile
    try:
        return fn()
    finally:
        config.HARNESS_PROFILE = orig


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── 一致性：这些维度两臂必须相同，否则测的不是 Harness ──

def test_same_model_both_arms():
    """模型必须一致 —— 否则测到的是模型差距。"""
    full = _with_profile("full", lambda: base.build_agent(ORCH))["model"]
    naive = _with_profile("naive", lambda: base.build_agent(ORCH))["model"]
    _assert(full == naive, f"两臂模型不一致：full={full} naive={naive}")


def test_business_tools_all_retained():
    """业务工具一个不能少，只允许摘掉 Harness 专有工具。"""
    full = set(_with_profile("full", lambda: base.build_agent(ORCH))["tools"])
    naive = set(_with_profile("naive", lambda: base.build_agent(ORCH))["tools"])
    missing = full - naive
    _assert(missing == (full & base._HARNESS_ONLY_TOOLS),
            f"naive 臂丢了业务工具：{sorted(missing - base._HARNESS_ONLY_TOOLS)}")
    _assert(not (naive - full), f"naive 臂多出工具：{sorted(naive - full)}")


def test_naive_keeps_environment_facts():
    """环境描述必须保留：不给模型连库名都不知道，那是造假对手而非评测。

    逐个核对关键资源名 —— 集群里有什么，是环境事实，不是方法论。
    """
    naive = _with_profile("naive", lambda: base.build_agent(ORCH))["system_prompt"]
    for token in ("prod-cluster-01", "rds-mysql-order", "rds-mysql-core",
                  "kvstore-redis-01", "api-gateway", "inventory-service"):
        _assert(token in naive, f"naive 提示词缺少环境事实：{token}")


# ── 差异性：这些必须只有 full 臂有，否则提升被低估/无法解释 ──

def test_naive_has_no_methodology():
    """方法论必须剥离干净。

    检查的是那 9 条工作准则的特征串，而不是笼统看长度 —— 长度可以靠
    环境描述凑出来，但只要有一句准则漏进去，这个数字就说不清测了什么。
    """
    naive = _with_profile("naive", lambda: base.build_agent(ORCH))["system_prompt"]
    for banned in ("工作准则", "并行取数", "空结果", "规则只是起点",
                   "任务清单", "禁止编造", "load_skill"):
        _assert(banned not in naive, f"naive 提示词混进了方法论：{banned}")


def test_naive_prompt_much_shorter():
    """量级上必须有明显差距 —— 兜底防"改了实现但特征串刚好都躲开"。"""
    full = _with_profile("full", lambda: base.build_agent(ORCH))["system_prompt"]
    naive = _with_profile("naive", lambda: base.build_agent(ORCH))["system_prompt"]
    _assert(len(naive) * 3 < len(full),
            f"naive({len(naive)}) 未显著短于 full({len(full)})，方法论可能没剥干净")


def test_verify_disabled_on_runtime_switch():
    """★ 运行时切到 naive 后事实核对必须关闭。

    这一项守的是真实踩过的坑：模块级求值只在导入时算一次，而评测脚本是在
    同一进程内改 HARNESS_PROFILE 的，核对会静默地继续开着。
    """
    _assert(_with_profile("full", config.verify_enabled) is True,
            "full 臂事实核对被关掉了")
    _assert(_with_profile("naive", config.verify_enabled) is False,
            "naive 臂事实核对没关 —— 基线臂偷带 Harness 能力，对比数据失效")


def test_verify_switch_is_reversible():
    """切回 full 后核对要恢复 —— 双臂交替跑时不能单向失效。"""
    seq = []
    for p in ("full", "naive", "full", "naive"):
        seq.append(_with_profile(p, config.verify_enabled))
    _assert(seq == [True, False, True, False], f"核对开关不可逆/有粘连：{seq}")


def test_naive_runs_tools_serially():
    """并行取数只有 full 臂有：它同时影响耗时与"一轮拿齐多份证据"。"""
    par = [n for n in sorted(base.effective_tools(ORCH))
           if (lambda s: bool(s and s.concurrency_safe and s.is_read_only))(
               registry.get_spec(n))
           and n not in base._HARNESS_ONLY_TOOLS]
    _assert(len(par) >= 3, f"可并行的业务工具不足，无法验证并行差异：{par}")
    calls = [_FakeToolCall(n) for n in par[:3]]

    full_batches = _with_profile("full", lambda: loop._partition(calls))
    naive_batches = _with_profile("naive", lambda: loop._partition(calls))
    _assert(len(full_batches) == 1 and len(full_batches[0]) == 3,
            f"full 臂未并行：{[len(b) for b in full_batches]}")
    _assert([len(b) for b in naive_batches] == [1, 1, 1],
            f"naive 臂未串行：{[len(b) for b in naive_batches]}")


def test_default_profile_is_full():
    """默认必须是 full：naive 只是评测用的对照臂，绝不能变成线上默认。"""
    _assert(config.HARNESS_PROFILE == "full",
            f"默认 profile 不是 full，而是 {config.HARNESS_PROFILE}")
    _assert(config.harness_is_naive() is False, "默认就处于 naive 基线态")


def main() -> int:
    groups = [
        ("两臂必须一致的维度（否则测的不是 Harness）", [
            test_same_model_both_arms,
            test_business_tools_all_retained,
            test_naive_keeps_environment_facts,
        ]),
        ("只允许 full 臂拥有的能力", [
            test_naive_has_no_methodology,
            test_naive_prompt_much_shorter,
            test_verify_disabled_on_runtime_switch,
            test_verify_switch_is_reversible,
            test_naive_runs_tools_serially,
        ]),
        ("默认值", [
            test_default_profile_is_full,
        ]),
    ]
    passed = failed = 0
    for title, tests in groups:
        print(f"\n=== {title} ===")
        for fn in tests:
            try:
                fn()
                passed += 1
                print(f"  ✓ {fn.__name__}")
            except Exception as e:                # noqa: BLE001
                failed += 1
                print(f"  ✗ {fn.__name__}: {e}")
    print("\n" + "=" * 46)
    print(f"通过 {passed} / 失败 {failed}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
