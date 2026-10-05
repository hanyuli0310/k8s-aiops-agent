"""L2 量化评测：故障场景闭环，需要 mock_server + collector 在跑。

评测什么（真值全部来自 mock_server 的场景定义，不是我写在评测里的期望）：
  A. 风险扫描准确性   —— 新增 findings 对照 expected_rules，算 P/R/F1
  B. 故障实例定位精度 —— resource_ref 是否指向 expected_instance
  C. 串报（误报）     —— 是否连带告警了 must_not_flag_instance（同型的另一台）
  D. 负样本误报       —— expect_no_new_findings 的场景是否真的没报新风险
  E. Agent 根因定位   —— 实例/机理/表三个维度分开判，不合成一个笼统的对错
  F. 耗时 / 成本      —— Agent 排障的端到端开销

为什么每个场景都重新取基线：上一个场景恢复后指标是指数回落的，可能有残留。
用固定基线会把残留算成本场景的新增（虚高 fp）。每次重扫虽然慢一点，但对得住。

用法（务必带上加速参数，否则每场景要等 3 分钟以上）：
    cd backend && RULE_WINDOW_MINUTES=2 .venv/bin/python -m eval.run_l2 --wait 140
    ... --only slow_query_storm     # 只跑一个场景，先验证链路
    ... --no-agent                  # 只评测规则侧，不烧 LLM

⚠️ 窗口与等待时长不能随意调，两边都有硬约束：
- **窗口不能 ≤ 60 秒**：指标采集是 60 秒一批，窗口与它同频时窗口内样本数会在
  0/1 之间振荡，落在 0 相位的扫描里指标类规则全部静默返回空。
  第一次评测 Redis 雪崩就这么丢了全部命中（手查库里 CPU 80.8% 早已越阈）。
- **等待时长需 ≥ 窗口时长**：规则算的是窗口均值，窗口里残留的故障前低值会把均值
  稀释到阈值下方（雪崩 mem 42→86%，3 分钟窗口里只有 1 分钟故障数据时均值仅 57%）。
  所以调大窗口必须同步调大 --wait，两者是绑定的。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import itertools
import time
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app import config, db                                      # noqa: E402
from app.harness import scheduler                               # noqa: E402
from app.harness.llm import QuotaExhausted
from app.harness.runctx import RunContext                       # noqa: E402
from app.tools import registry                                  # noqa: E402
from eval import cases, metrics                                 # noqa: E402

MOCK = "http://localhost:9001"

# 排障提问：没有为每个场景手写问法的，用这句通用的。
# 刻意**不告诉 Agent 是哪个组件出问题**——那等于把答案喂给它。
GENERIC_ASK = ("线上有接口变慢或报错，帮我定位根因："
               "说明是哪个组件/实例的问题、机理是什么，并给出治理建议。")


# 配置类规则在**稳态就已存在**（预埋缺陷），注入故障不会让它们"新增"。
# 例：instance_down 的根因确实是 HA-002 单副本，但 HA-002 在稳态基线里就报着，
# 按"新增"判就会记成漏报 —— 那是评测口径错，不是系统漏报。
# 所以这类规则改判"当前是否仍在报"。
_CONFIG_RULES = {"HA-001", "HA-002", "HA-003", "HA-004",
                 "CAP-001", "CAP-002", "CAP-003"}


def _api_backend_map() -> dict:
    """从 mock_server 取「接口路径 → 后端服务」映射。

    为什么需要：API-001 的 resource_ref 是接口路径（"POST /api/orders"），
    而故障场景标注的根因是服务名（"order-service"）。没有这层映射，
    "报出的接口是否属于故障服务"就无法判定 ——
    第一版评测就是缺了它，把 7 个正确结果误判成未命中（实例定位虚报成 36%）。
    """
    try:
        d = httpx.get(f"{MOCK}/control/world_status", timeout=10).json()
        return {a["api"]: a.get("backend") for a in d.get("apis", [])
                if a.get("backend")}
    except Exception as e:                                       # noqa: BLE001
        print(f"    ⚠️ 取接口→服务映射失败（{e}），实例定位判定将只按名字匹配")
        return {}


def _instance_hit(refs: list, expected: str, api2svc: dict) -> bool:
    """判定风险是否落在期望的实体上。两种 resource_ref 形态都要认。"""
    for ref in refs:
        if expected in ref:                     # rds-mysql-core / 服务名直接出现
            return True
        if api2svc.get(ref) == expected:        # 接口路径归属该服务
            return True
    return False


class EnvLost(RuntimeError):
    """被测环境（mock_server）中途失联。"""


def _require_mock(stage: str):
    """每个关键阶段都要确认 mock_server 还在 —— 失联就**立即停**，不准继续。

    为何必要：实测遇到过 mock_server 中途被杀（日志无任何错误、正常服务到最后一条），
    而评测**毫无察觉地继续跑了下去**：Agent 只读数据库，旧数据还在，
    于是照样能算出 P/R/F1 与耗时 —— 但世界已经不再演进、注入的故障也永远不会恢复，
    这些数字看起来正常却完全无意义。

    这比“评测报错”危险得多：报错会被看见，假数据会被当结论写进报告。
    """
    try:
        r = httpx.get(f"{MOCK}/health", timeout=5)
        r.raise_for_status()
    except Exception as e:                                       # noqa: BLE001
        raise EnvLost(
            f"mock_server 在【{stage}】阶段失联（{e}）。本轮数据不可信，已中止。\n"
            f"   请重启后重跑（用 setsid/nohup 避免跟随终端会话退出）：\n"
            f"   cd mock_server && TICK_INTERVAL_S=2 setsid nohup "
            f"../backend/.venv/bin/python -m uvicorn app.main:app --port 9001 "
            f"> /tmp/mock9001.log 2>&1 &") from e


def _open_findings() -> list:
    return db.fetch_all(
        "SELECT rule_id, severity, resource_ref FROM risk_findings WHERE status='open'")


def _scan():
    run = RunContext(session_id="eval-l2-scan", mode="auto")
    registry.execute("run_risk_scan", {}, run=run)


# 稳态应有的 open findings 条数 = 预埋缺陷数（7 条：HA-001~004 + CAP-001~003）。
# 从 mock_server 的 PLANTED_DEFECTS 拿会更权威，但那要多一次跨进程依赖；
# 这里用"扫描结果自己收敛"的方式判断，不硬编码语义。
STEADY_MAX = 7


def _wait_until_steady(timeout: int = None, poll: int = 15) -> tuple:
    """恢复故障后**轮询等到世界回到稳态**，再开始下一个场景。

    为什么必须等：治理/恢复后指标是**指数衰减**的（半衰期 2 拍），
    不是立刻归零。第一版只 sleep 8s 就进下一个场景，结果上一场的残留
    被算进了下一场的基线 —— 基线从 7 条涨到 11 条，本场景的"新增"
    就少算了，recall 被系统性压低。**那是评测污染，不是系统漏报。**

    ★ 超时默认值必须 ≥ 规则窗口时长，这一点上面那段教训只改了一半：
      timeout 曾固定为 150s，而 live 模式下 finding 要靠"窗口内的高值全部
      滑出"才会自然 resolved。窗口 5 分钟（300s）时 150s 根本等不到回稳 ——
      实测 12 个场景里 11 个 steady_before=False、基线涨到 9~16 条，
      并因此造出一个假漏报（slow_query_storm 的 API-001）。
      所以按窗口动态算，留 20% 余量。

    返回 (是否收敛, 最终条数, 等待秒数)。超时也继续（并如实记录），
    否则一个不收敛的场景会把整轮评测卡死。
    """
    if timeout is None:
        timeout = max(150, int(config.RULE_WINDOW_MINUTES * 60 * 1.2))
    t0 = time.time()
    n = None
    while time.time() - t0 < timeout:
        _scan()
        n = len(_open_findings())
        if n <= STEADY_MAX:
            return True, n, round(time.time() - t0, 1)
        time.sleep(poll)
    return False, n, round(time.time() - t0, 1)


def _baseline() -> set:
    """扫一次并返回当前 open 的 (rule_id, resource_ref) 集合。"""
    _scan()
    return {(r["rule_id"], r["resource_ref"]) for r in _open_findings()}


def _clear_active_faults() -> int:
    """开跑前把 mock_server 里**残留的活跃故障全部恢复**。

    为什么必需：评测被 Ctrl-C 或 kill 中断时，已注入的故障不会自动恢复，
    留在世界里继续生效。下一轮评测开跑时世界已是多故障叠加态 ——
    实测出现过基线 17 条 open（稳态 7 条）、第一个场景就 R=0% 的情况，
    而那完全是脏环境造成的假数据。

    评测最怕的不是分数低，而是**分数不可信**。所以宁可每次多花几十秒清场。
    """
    try:
        d = httpx.get(f"{MOCK}/control/world_status", timeout=10).json()
        active = d.get("active_faults", [])
    except Exception as e:                                       # noqa: BLE001
        print(f"⚠️ 无法读取世界状态清理残留故障：{e}")
        return 0
    for f in active:
        fid = f.get("fault_id")
        if fid:
            _recover(fid)
            print(f"  已恢复残留故障 {fid}（{f.get('scenario_id')}）")
    return len(active)


def _inject(sid: str) -> dict:
    r = httpx.post(f"{MOCK}/control/inject_fault", json={"scenario_id": sid}, timeout=15)
    r.raise_for_status()
    return r.json()


def _recover(fault_id: str):
    try:
        httpx.post(f"{MOCK}/control/recover_fault",
                   json={"fault_id": fault_id}, timeout=15)
    except Exception as e:                                       # noqa: BLE001
        print(f"    ⚠️ 恢复故障失败（{e}），后续场景可能受残留影响")


def _ask_agent(sid: str, question: str, routing: str = None,
               profile: str = None) -> dict:
    """让 Agent 排一次障。routing / profile 不为空时临时切换。

    profile="naive" 即朴素基线臂：同模型、同业务工具，但剥离方法论、事实核对、
    并行取数、子 Agent —— 用于回答"相比不用本项目的替代方案，提升了多少"。

    session_id 必须带上 routing 与 profile：不同臂**绝不能共用会话历史**，
    否则后跑的那个能看到前一个的结论，等于直接把答案喂给它。
    这里漏一个维度，整批对比数据就全废，而且表面上完全看不出异常。
    """
    orig_routing, orig_profile = config.AGENT_ROUTING, config.HARNESS_PROFILE
    if routing:
        config.AGENT_ROUTING = routing
    if profile:
        config.HARNESS_PROFILE = profile
    tag = ("eval-l2-" + sid + (f"-{routing}" if routing else "")
           + (f"-{profile}" if profile else ""))
    run = RunContext(session_id=tag, mode="readonly")
    t0 = time.perf_counter()
    answer, tools, warn = "", 0, None
    dispatches, main_agent = [], None
    # 记录这几类事件，为的是把"没做完"与"做完但答错"分开统计。
    # naive 臂没有 update_plan，步数用尽就只能中断 —— 若不区分，
    # 它的失败会被一律记成"推理不行"，那是在夸大 Harness 的推理贡献。
    aborted, continuations = False, 0
    try:
        for ev in scheduler.handle_message(tag, question, run=run):
            et = ev.get("type")
            if et == "agent_start" and main_agent is None:
                main_agent = ev.get("agent")
            elif et == "tool_call":
                tools += 1
                # ★ 逐步打点：一次排障可能跑十几分钟，若全程不输出，
                #   从外部完全分不出“在正常多轮推理”还是“挂在某次调用上了”。
                #   实测踩过：看到 11 分钟零输出、CPU 0%，误判为卡死而中止了评测，
                #   事后用 sample 看栈才确认它一直在正常等响应。
                d = ev.get("depth") or 0
                print(f"       [{time.perf_counter() - t0:5.0f}s] "
                      f"{'  ' * d}#{tools} {ev.get('tool')}", flush=True)
                if ev.get("tool") == "dispatch_agent":
                    dispatches.append((ev.get("args") or {}).get("subagent_type"))
            elif et == "answer":
                answer = ev.get("text") or answer
            elif et == "verify_warning":
                warn = ev
            elif et in ("compacted", "model_fallback", "continuation", "aborted"):
                # 这几类事件直接影响耗时与结果可信度，必须在日志里看得见
                if et == "aborted":
                    aborted = True
                elif et == "continuation":
                    continuations += 1
                print(f"       [{time.perf_counter() - t0:5.0f}s] ⚠️ {et}"
                      f" {ev.get('reason') or ev.get('text') or ''}"[:100], flush=True)
    finally:
        config.AGENT_ROUTING, config.HARNESS_PROFILE = orig_routing, orig_profile
    # 回答全文一并返回：串报/机理这类判定是启发式的，报告必须能被复核
    return {"answer": answer, "seconds": round(time.perf_counter() - t0, 1),
            "tool_calls": tools, "tokens": run.tokens_in + run.tokens_out,
            "cost_cny": run.usage_event()["est_cost_cny"], "verify_warning": warn,
            "routing": routing or orig_routing, "main_agent": main_agent,
            "profile": profile or orig_profile, "dispatches": dispatches,
            # incomplete=任务没跑完（中断或压根没给出回答），与"答错"是两回事
            "incomplete": bool(aborted or not (answer or "").strip()),
            "continuations": continuations,
            # naive 臂的幻觉探针：同一把尺子量两臂的回答（基线不自纠但要留证据）
            "verify_probe": run.verify_probe}


def run_scenario(sid: str, sc: dict, wait_s: int, ask: bool,
                 api2svc: dict = None, routings: list = None, repeat: int = 1,
                 profiles: list = None) -> dict:
    """跑一个场景。

    routings / profiles 给多个值时，在**同一次注入下**依次问每种组合。
    为何不分两轮跑：每轮重新注入一次，两种模式面对的世界就不是同一个
    （指标是指数演变的，注入时长、采集相位都不一样），对比就不公平了；
    同一注入下连着问才是控制变量。附带好处：注入/回稳开销只付一次。
    """
    api2svc = api2svc or {}
    print(f"\n{'─' * 74}\n▶ {sid}（{sc.get('difficulty', '?')}）— {sc['title']}")
    _require_mock(f"{sid} 开始前")
    steady, n_steady, waited = _wait_until_steady()
    if not steady:
        print(f"  ⚠️ 等待 {waited}s 后仍有 {n_steady} 条 open（稳态应 ≤{STEADY_MAX}），"
              f"上一场景可能有残留 —— 本场景的新增判定会偏保守")
    base = _baseline()
    print(f"  基线 open findings: {len(base)} 条"
          + (f"（等待回稳 {waited}s）" if waited > 1 else ""))

    inj = _inject(sid)
    fid = inj.get("fault_id")
    print(f"  已注入 {fid}，影响 {len(inj.get('affected', []))} 个实体，"
          f"等待 {wait_s}s 让数据进入规则窗口…")
    time.sleep(wait_s)

    _scan()
    _require_mock(f"{sid} 注入后扫描时")
    cur = [(r["rule_id"], r["resource_ref"]) for r in _open_findings()]
    now = set(cur)
    added = now - base
    added_rules = sorted({r for r, _ in added})
    now_rules = sorted({r for r, _ in now})
    expected = sc["expected_rules"]

    # A. 规则层面对账
    #    数据类规则按"新增"判；配置类规则（稳态就存在）按"当前在报"判。
    #
    #    ★ 第三种状态：**不可判定**。若某条数据类期望规则在【基线里就已经
    #      open】（上一场景残留未消），那"新增"判定对它天然失效 ——
    #      它报了也看不出是本场景报的。这类必须从 PRF 里排除并如实记录：
    #      算 fn 是把评测污染当成系统漏报，算 tp 则是给自己放水。
    #      两种做法都会让报告失真，只是失真方向不同。
    base_rules = {r for r, _ in base}
    undecidable = sorted(r for r in expected
                         if r not in _CONFIG_RULES and r in base_rules)
    judged = [r for r in expected if r not in undecidable]
    # 检出集里也要去掉不可判定的那几条，否则它们会被当成"期望外的检出"算进 fp。
    # ⚠️ 但**绝不能**写成 detected & judged —— 那会把真正的误报也一起过滤掉，
    #    precision 就永远是 100%。fp 必须留下来被算出来。
    detected = (set(added_rules) | {r for r in judged
                                    if r in _CONFIG_RULES and r in now_rules}
                ) - set(undecidable)
    prf = metrics.compare_sets(sorted(detected), judged)
    print(f"  新增规则: {added_rules or '（无）'}")
    if undecidable:
        print(f"  ⚠️ 不可判定（基线残留中已 open，本轮不计入 PRF）: {undecidable}")
    cfg_hit = sorted(set(detected) - set(added_rules))
    if cfg_hit:
        print(f"  配置类命中: {cfg_hit}（稳态既有，按'当前在报'计）")
    print(f"  期望规则: {expected or '（负样本，期望无新增）'}")
    print(f"  规则对账: {metrics.fmt_prf(prf)}")

    # B/C. 实例定位与串报（接口路径要经映射还原成服务）
    added_refs = [ref for _, ref in added]
    inst = sc.get("expected_instance")
    # 负样本本来就不该产生任何 finding，"实例定位"对它无意义 ——
    # 硬算进去会把"正确地什么都没报"记成"定位失败"，白白拉低指标。
    inst_hit = (_instance_hit(added_refs, inst, api2svc)
                if inst and not sc.get("expect_no_new_findings") else None)
    other = sc.get("must_not_flag_instance")
    cross = _instance_hit(added_refs, other, api2svc) if other else None
    if inst:
        # inst_hit is None 意为"本场景不适用这项判定"（负样本），
        # 不能和"判了但没命中"共用 ❌ —— 汇总里已正确排除它（看的是 is not None），
        # 单场景行却把它打印成失败，让人以为评测有问题。报告里的一个 ❌ 很贵。
        if inst_hit is None:
            print(f"  实例定位: — 负样本不适用此判定（本就不应产生 finding）")
        else:
            print(f"  实例定位: {'✅' if inst_hit else '❌'} 期望 {inst} "
                  f"{'命中' if inst_hit else '未出现在 resource_ref'}")
    if other:
        print(f"  串报检查: {'❌ 串报了 ' + other if cross else '✅ 未串报 ' + other}")

    # D. 负样本
    neg_ok = None
    if sc.get("expect_no_new_findings"):
        neg_ok = not added
        print(f"  负样本判定: {'✅ 未报新风险（正确）' if neg_ok else '❌ 误报了 ' + str(added_rules)}")

    # E. Agent 根因定位
    agent = None
    verdict = None
    agent_runs = []               # 多调度对比时的逐个结果
    if ask:
        case = next((c for c in cases.FAULT_CASES if c["scenario"] == sid), None)
        question = case["question"] if case else GENERIC_ASK
        judged = dict(sc)
        if case and case.get("expect_keywords_any"):
            judged["_expect_keywords_any"] = case["expect_keywords_any"]
        modes = routings or [None]
        arms = profiles or [None]
        # 组合顺序 profile → routing → repeat：基线对比是主变量，同一臂的多次重复
        # 连着跑，中途就能看出"是这一臂整体差"还是"只是某次波动"。
        for profile, routing, rep in itertools.product(arms, modes, range(repeat)):
            label = routing or config.AGENT_ROUTING
            arm = profile or config.HARNESS_PROFILE
            tag = f"（第 {rep + 1}/{repeat} 次）" if repeat > 1 else ""
            print(f"  → 让 Agent 排障（调度={label} 档位={arm}）{tag}…")
            a = _ask_agent(sid, question, routing, profile)
            v = metrics.judge_root_cause(a["answer"], judged)
            bits = []
            if v.instance_hit is not None:
                bits.append(f"实例{'✅' if v.instance_hit else '❌'}")
            if v.keyword_hit is not None:
                bits.append(f"机理{'✅' if v.keyword_hit else '❌'}")
            if v.table_hit is not None:
                bits.append(f"表级{'✅' if v.table_hit else '❌'}")
            if v.cross_flag is not None:
                bits.append(f"串报{'❌有' if v.cross_flag else '✅无'}")
            print(f"     [{arm:5s}/{label:6s}] {' '.join(bits)}  {a['seconds']}s "
                  f"{a['tool_calls']}工具 ¥{a['cost_cny']:.4f}"
                  f"{' 子Agent:' + ','.join(a['dispatches']) if a['dispatches'] else ''}"
                  f"{' ⚠️幻觉报警' if a['verify_warning'] else ''}")
            print(f"     摘要: {(a['answer'] or '（空）')[:120]}")
            agent_runs.append({**a, "verdict": {
                "instance": v.instance_hit, "keyword": v.keyword_hit,
                "table": v.table_hit, "cross": v.cross_flag,
                "strict_ok": v.strict_ok}})
            if agent is None:          # 主结果取第一次，保持旧汇总口径不变
                agent, verdict = a, v
            # 每次排障后再探一次：一次排障可能跑十几分钟，
            # 环境很可能就是在这期间没的（实测就是这么发生的）。
            _require_mock(f"{sid} 排障（{arm}/{label}）结束后")

    if fid:
        _recover(fid)
        time.sleep(10)          # 让恢复动作先生效，真正的回稳等待在下个场景开头做

    return {
        "scenario": sid, "difficulty": sc.get("difficulty"),
        "baseline_n": len(base), "steady_before": steady,
        "added_rules": added_rules, "expected_rules": expected,
        "undecidable_rules": undecidable,
        "detected_rules": sorted(detected), "added_refs": added_refs,
        "prf": {"tp": prf.tp, "fp": prf.fp, "fn": prf.fn},
        "instance_hit": inst_hit, "cross_flag": cross, "negative_ok": neg_ok,
        "agent": agent,
        "agent_runs": agent_runs,
        "verdict": ({"instance": verdict.instance_hit, "keyword": verdict.keyword_hit,
                     "table": verdict.table_hit, "cross": verdict.cross_flag,
                     "strict_ok": verdict.strict_ok} if verdict else None),
    }


def summarize(rows: list):
    print(f"\n{'═' * 74}\n汇总（L2 故障闭环）\n{'═' * 74}")
    total = metrics.PRF()
    pos = [r for r in rows if r["expected_rules"]]
    neg = [r for r in rows if not r["expected_rules"]]

    for r in pos:
        total = total.add(metrics.PRF(**r["prf"]))
    print(f"风险扫描（{len(pos)} 个正样本，微平均）：{metrics.fmt_prf(total)}")

    inst = [r for r in pos if r["instance_hit"] is not None]
    if inst:
        hit = sum(1 for r in inst if r["instance_hit"])
        print(f"故障实例定位：{hit}/{len(inst)} = {hit / len(inst) * 100:.0f}%")
    crossed = [r for r in rows if r["cross_flag"]]
    print(f"串报（同型另一实例被连带告警）：{len(crossed)} 例"
          + (f" → {[r['scenario'] for r in crossed]}" if crossed else ""))
    if neg:
        ok = sum(1 for r in neg if r["negative_ok"])
        print(f"负样本不误报：{ok}/{len(neg)}")

    agents = [r for r in rows if r.get("agent")]
    if agents:
        strict = sum(1 for r in agents if r["verdict"] and r["verdict"]["strict_ok"])
        print(f"\nAgent 根因定位（严格：该判的维度全对且无串报）："
              f"{strict}/{len(agents)} = {strict / len(agents) * 100:.0f}%")
        for dim in ("instance", "keyword", "table"):
            vs = [r["verdict"][dim] for r in agents
                  if r["verdict"] and r["verdict"][dim] is not None]
            if vs:
                print(f"  · {dim:9s} {sum(vs)}/{len(vs)} = {sum(vs) / len(vs) * 100:.0f}%")
        secs = [r["agent"]["seconds"] for r in agents]
        tcs = [r["agent"]["tool_calls"] for r in agents]
        costs = [r["agent"]["cost_cny"] for r in agents]
        n = len(secs)
        print(f"\n排障开销：平均 {sum(secs) / n:.1f}s（{min(secs):.0f}~{max(secs):.0f}s）、"
              f"{sum(tcs) / n:.1f} 次工具调用、¥{sum(costs) / n:.4f}/次")
        warned = sum(1 for r in agents if r["agent"]["verify_warning"])
        print(f"幻觉 A 类报警：{warned}/{n}")

        # 人工基线：排障类任务，公式适用
        avg_sec, avg_tc = sum(secs) / n, int(round(sum(tcs) / n))
        print(f"\n【人工基线估算 —— 估算，非实测对照组】")
        print(f"  模型：人工分钟 = {avg_tc} 次跨系统查询 × 单价 + 4 系统 × 1.0 + 5.0 推断")
        for label, v in metrics.speedup_range(avg_sec, avg_tc, 4, "diagnosis").items():
            print(f"  {label}（{v['per_query_min']} 分/次）：人工约 {v['manual_minutes']:.0f} 分钟"
                  f" → Agent {avg_sec:.0f}s，提速 {v['speedup']}×")

    by_diff: dict = {}
    for r in rows:
        d = r["difficulty"] or "?"
        st = by_diff.setdefault(d, {"n": 0, "ok": 0})
        st["n"] += 1
        if r["verdict"] and r["verdict"]["strict_ok"]:
            st["ok"] += 1
    # --no-agent 时根本没跑 Agent，strict_ok 全是 None，印出来就是一片 0/N，
    # 看起来像"Agent 全错"—— 实际是没测。没测就不要报数字。
    if not agents:
        print(f"\n（本轮 --no-agent，未评测 Agent 根因定位，因此不输出难度分层通过率）")
        return
    print(f"\n按难度分层（Agent 严格通过率）：")
    for d in ("easy", "medium", "hard"):
        if d in by_diff:
            st = by_diff[d]
            print(f"  {d:7s} {st['ok']}/{st['n']}")


def _wilson(k: int, n: int) -> tuple:
    """比例的 Wilson 置信区间（95%）。

    为何不用 k/n ± 1.96·√(p(1-p)/n)：正态近似在小样本或 p 接近 0/1 时会给出
    荒谬的区间（比如 6/6 得到 [1.0, 1.0]，等于宣称"绝不会失败"）。
    Wilson 在同样样本下给 6/6 → 约 [0.61, 1.0]，如实反映"样本太少，说不了这么满"。

    这个区间是本报告里"提升 X 个百分点"可信度的唯一依据：若两臂区间大幅重叠，
    就必须承认差异未达统计显著，而不能把点估计的差值当结论。
    """
    if n <= 0:
        return (0.0, 0.0)
    z = 1.96
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - half), min(1.0, c + half))


def _summarize_profile(rows: list):
    """★ 朴素基线对比：产出「相比朴素基线提升 X 个百分点」（--compare-baseline）。

    这是整份报告最核心、也最容易被质疑的数字，所以口径要摆明：

    1. **失败分两类**，绝不合并：
         · 未完成（incomplete）—— 中断/无回答，多半是步数耗尽而 naive 没有续跑能力；
         · 答错 —— 跑完了但根因判定不通过。
       合并统计会把"工程能力差距"算成"推理能力差距"，那是夸大。
    2. **耗时不预设方向**。naive 不做核对、不并行，很可能更快但更错。
       若实测如此就如实写，不能只报对我们有利的那一半。
    3. **给区间不给单点**。见 _wilson。
    """
    by: dict = {}
    for r in rows:
        for a in r.get("agent_runs") or []:
            by.setdefault(a.get("profile") or "full", []).append((r["scenario"], a))
    if len(by) < 2:
        return

    print(f"\n{'═' * 74}\n★ 朴素基线对比（同一注入、同模型、同业务工具）\n{'═' * 74}")
    stat = {}
    for prof, items in by.items():
        n = len(items)
        ok = sum(1 for _, a in items if a["verdict"]["strict_ok"])
        inc = sum(1 for _, a in items if a.get("incomplete"))
        # 答错 = 没通过 且 跑完了；与未完成互斥，两者相加即全部失败
        wrong = sum(1 for _, a in items
                    if not a["verdict"]["strict_ok"] and not a.get("incomplete"))
        lo, hi = _wilson(ok, n)
        secs = [a["seconds"] for _, a in items]
        costs = [a["cost_cny"] for _, a in items]
        warned = sum(1 for _, a in items if a["verify_warning"])
        crossed = sum(1 for _, a in items if a["verdict"]["cross"])
        stat[prof] = {"n": n, "ok": ok, "rate": ok / n, "lo": lo, "hi": hi,
                      "inc": inc, "wrong": wrong, "sec": statistics.median(secs),
                      "cost": statistics.median(costs), "warned": warned,
                      "crossed": crossed}
        tag = "完整 Harness" if prof == "full" else "朴素基线"
        print(f"\n【{prof}】{tag} — {n} 次排障")
        print(f"  根因严格通过: {ok}/{n} = {ok / n * 100:.1f}%"
              f"  [95% CI {lo * 100:.1f}~{hi * 100:.1f}%]")
        print(f"  失败拆解: 答错 {wrong} 次、未完成（中断/无回答）{inc} 次")
        for dim in ("instance", "keyword", "table"):
            vs = [a["verdict"][dim] for _, a in items if a["verdict"][dim] is not None]
            if vs:
                print(f"    · {dim:9s} {sum(vs)}/{len(vs)} = {sum(vs) / len(vs) * 100:.0f}%")
        print(f"  同型实例串报: {crossed} 次")
        print(f"  耗时中位 {statistics.median(secs):.1f}s、成本中位 ¥{statistics.median(costs):.4f}")
        # 幻觉率两臂都报：full 看拦截报警数，naive 看离线探针命中数。
        # 用同一套 verifier 判定，所以这两个数字**可以直接比**——
        # 差值就是"没有核对机制会被放过的幻觉量"。
        if prof == "full":
            print(f"  幻觉（核对拦截并告警）: {warned}/{n}")
        else:
            probed = sum(1 for _, a in items
                         if (a.get("verify_probe") or {}).get("suspicious"))
            print(f"  幻觉（离线同尺核对，未拦截）: {probed}/{n}"
                  f"  ← 基线无核对机制，这些幻觉会原样交给用户")

    if "full" not in stat or "naive" not in stat:
        return
    f, nv = stat["full"], stat["naive"]
    dp = (f["rate"] - nv["rate"]) * 100
    print(f"\n{'─' * 74}\n结论")
    print(f"  根因定位准确率: 朴素基线 {nv['rate'] * 100:.1f}% → 完整 Harness "
          f"{f['rate'] * 100:.1f}%，**{'提升' if dp >= 0 else '下降'} {abs(dp):.1f} 个百分点**")
    # 区间重叠即不能宣称显著 —— 这一句是防止自己过度解读点估计
    overlap = not (f["lo"] > nv["hi"] or nv["lo"] > f["hi"])
    print(f"  统计显著性: 两臂 95% 置信区间{'**有重叠 → 差异未达显著，需加大样本**' if overlap else '不重叠 → 差异显著'}")
    if nv["inc"] or f["inc"]:
        print(f"  其中未完成占比: 基线 {nv['inc']}/{nv['n']}、完整 {f['inc']}/{f['n']}"
              f"（这部分是工程能力差距，非推理能力）")
    d_sec = (nv["sec"] - f["sec"]) / nv["sec"] * 100 if nv["sec"] else 0
    print(f"  排障耗时中位: 基线 {nv['sec']:.1f}s → 完整 {f['sec']:.1f}s"
          f"（{'降低' if d_sec >= 0 else '增加'} {abs(d_sec):.0f}%）")
    if f["sec"] > nv["sec"]:
        print(f"    ⚠️ 完整 Harness 更慢：它多做了事实核对与更完整的取证。"
              f"若准确率同时更高，这是刻意的取舍而非退步；报告须如实说明。")
    print(f"  同型实例串报: 基线 {nv['crossed']} 次 → 完整 {f['crossed']} 次")


def _summarize_routing(rows: list):
    """按顶层调度方式分组对比（仅 --compare-routing 时有内容）。

    两个关键字段要并排看：**根因准确率**与**开销**。
    只看开销会得出"新调度更快所以更好"的结论 —— 而如果它快是因为少查了几步
    导致根因定错，那就是变差不是变好。
    """
    by_routing: dict = {}
    for r in rows:
        for a in r.get("agent_runs") or []:
            by_routing.setdefault(a["routing"], []).append((r["scenario"], a))
    if len(by_routing) < 2:
        return

    print(f"\n{'═' * 74}\n顶层调度对比（同一注入下每种调度可多次取中位数）\n{'═' * 74}")
    for routing, items in by_routing.items():
        n = len(items)
        strict = sum(1 for _, a in items if a["verdict"]["strict_ok"])
        secs = [a["seconds"] for _, a in items]
        costs = [a["cost_cny"] for _, a in items]
        tcs = [a["tool_calls"] for _, a in items]
        disp = sum(len(a["dispatches"]) for _, a in items)
        warned = sum(1 for _, a in items if a["verify_warning"])
        print(f"\n【{routing}】{n} 次排障（跨场景×重复）")
        print(f"  根因严格通过（该判维度全对且无串报）: {strict}/{n} = {strict / n * 100:.0f}%")
        for dim in ("instance", "keyword", "table"):
            vs = [a["verdict"][dim] for _, a in items if a["verdict"][dim] is not None]
            if vs:
                print(f"    · {dim:9s} {sum(vs)}/{len(vs)}")
        # 中位数而非均值：同量级调用耗时实测可差 7 倍，均值被极端值带偏
        print(f"  开销（中位数）: {statistics.median(secs):.1f}s"
              f"（{min(secs):.0f}~{max(secs):.0f}）、{statistics.median(tcs):.0f} 次工具、"
              f"¥{statistics.median(costs):.4f}/次"
              f"、共派子 Agent {disp} 次、幻觉报警 {warned}/{n}")

    # 逐场景对比：按场景聚合（通过次数 + 耗时中位数），平均值会掉“某场景完胜/完负”
    names = list(by_routing)
    print(f"\n逐场景（严格通过次数 / 耗时中位数）：")
    print(f"  {'场景':30s} " + "  ".join(f"{r:>20s}" for r in names))
    scen_order = []
    for r in rows:
        if r.get("agent_runs") and r["scenario"] not in scen_order:
            scen_order.append(r["scenario"])
    for s in scen_order:
        cells = []
        for r in names:
            runs = [a for sc, a in by_routing[r] if sc == s]
            if not runs:
                cells.append("—".rjust(20))
            else:
                ok = sum(1 for a in runs if a["verdict"]["strict_ok"])
                med = statistics.median([a["seconds"] for a in runs])
                cells.append(f"{ok}/{len(runs)}  {med:6.1f}s".rjust(20))
        print(f"  {s:30s} " + "  ".join(cells))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只跑指定场景（逗号分隔）")
    ap.add_argument("--wait", type=int, default=75, help="注入后等待秒数")
    ap.add_argument("--no-agent", action="store_true", help="只评测规则，不问 Agent")
    ap.add_argument("--compare-routing", action="store_true",
                    help="同一注入下依次用 model / intent 两种调度各排障一次，对比结果")
    ap.add_argument("--compare-baseline", action="store_true",
                    help="★ 同一注入下依次用完整 Harness / 朴素基线各排障，"
                         "产出「相比朴素基线提升 X 个百分点」")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每种组合重复排障 N 次（取中位数消除模型波动、算置信区间）")
    ap.add_argument("--json", help="结果写入 JSON")
    args = ap.parse_args()

    try:
        httpx.get(f"{MOCK}/health", timeout=5).raise_for_status()
    except Exception as e:                                       # noqa: BLE001
        raise SystemExit(f"❌ mock_server 不可达（{MOCK}）：{e}\n"
                         f"   请先启动：cd mock_server && "
                         f"TICK_INTERVAL_S=2 uvicorn app.main:app --port 9001")

    db.init_db()
    registry.ensure_loaded()

    # 真值一律从运行中的 mock_server 取，评测端【不复制一份】——
    # 复制就会漂移，报告会拿过期真值判对错。
    raw = httpx.get(f"{MOCK}/control/scenarios", timeout=10).json()
    items = raw.get("scenarios", raw) if isinstance(raw, dict) else raw
    scen = {it["id"]: it for it in items}

    wanted = list(scen)
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        for s in wanted:
            if s not in scen:
                raise SystemExit(f"❌ 未知场景 {s}；可选：{', '.join(scen)}")

    print(f"数据源       : {config.DATA_SOURCE}")
    print(f"规则窗口     : {config.RULE_WINDOW_MINUTES} 分钟"
          + ("  ⚠️ 窗口 ≤ 1 分钟会与 60 秒采集周期同频，"
             "指标类规则会周期性零样本静默失效 —— 建议 ≥2"
             if config.RULE_WINDOW_MINUTES <= 1 else ""))
    if args.wait < config.RULE_WINDOW_MINUTES * 60:
        print(f"  ⚠️ --wait {args.wait}s 小于窗口时长 "
              f"{config.RULE_WINDOW_MINUTES * 60}s：窗口内会混入故障前的低值，"
              f"均值被稀释后可能越不了阈 —— 漏报会是评测造的，不是系统的")
    cleared = _clear_active_faults()
    if cleared:
        print(f"清理了 {cleared} 个残留故障，等待世界回稳…")
        ok, n, waited = _wait_until_steady(timeout=240)
        print(f"  {'✅ 已回稳' if ok else '⚠️ 未完全回稳'}：{n} 条 open（等待 {waited}s）")
        if not ok:
            print(f"  ⚠️ 环境未回到稳态，本轮数据可信度下降 —— "
                  f"建议重启 mock_server 后再跑")

    api2svc = _api_backend_map()
    print(f"接口→服务映射: {len(api2svc)} 条")
    print(f"待评测场景   : {len(wanted)} 个，注入后各等待 {args.wait}s")
    est = len(wanted) * (args.wait + (45 if not args.no_agent else 0) + 20)
    print(f"预计总耗时   : 约 {est // 60} 分 {est % 60} 秒")

    rows = []
    routings = ["model", "intent"] if args.compare_routing else None
    if routings:
        print(f"调度对比     : {' vs '.join(routings)}（同一注入下各排障一次）")
    profiles = ["full", "naive"] if args.compare_baseline else None
    if profiles:
        print(f"基线对比     : 完整 Harness vs 朴素基线"
              f"（同模型同工具，仅剥离 Harness 机制）")
    for i, sid in enumerate(wanted, 1):
        print(f"\n[{i}/{len(wanted)}]", end="")
        try:
            rows.append(run_scenario(sid, scen[sid], args.wait,
                                     not args.no_agent, api2svc, routings,
                                     repeat=args.repeat, profiles=profiles))
        except QuotaExhausted as e:
            # 与 EnvLost 同理：环境/额度没了就不能继续产出"看起来正常"的数字
            print(f"\n\n❌ LLM 配额耗尽，评测中止：{e}")
            print(f"⚠️ 本轮数据【无效】，不可用于任何结论"
                  f"（已完成 {len(rows)}/{len(wanted)} 个场景）。")
            _clear_active_faults()
            return
        except EnvLost as e:
            # 环境失联不能当成"这个场景挂了"继续跑下一个 ——
            # 后面每个场景都会在无效环境里产出假数据，而且注入的故障无人恢复。
            print(f"\n❌ {e}")
            print(f"\n已完成 {len(rows)}/{len(wanted)} 个场景，以下汇总仅含这些场景。")
            break
        except Exception as e:                                    # noqa: BLE001
            print(f"\n  ❌ 场景 {sid} 评测异常：{e}")
            rows.append({"scenario": sid, "difficulty": scen[sid].get("difficulty"),
                         "added_rules": [], "expected_rules": scen[sid]["expected_rules"],
                         "prf": {"tp": 0, "fp": 0, "fn": len(scen[sid]["expected_rules"])},
                         "instance_hit": None, "cross_flag": None, "negative_ok": None,
                         "agent": None, "agent_runs": [], "verdict": None,
                         "error": str(e)})

    summarize(rows)
    _summarize_profile(rows)
    _summarize_routing(rows)
    if not rows:
        print("\n⚠️ 本轮没有任何场景完成，无指标可报。")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1,
                                              default=str), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")


if __name__ == "__main__":
    main()
