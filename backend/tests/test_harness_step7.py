"""第 7 步（P2-1 Skill 渐进式披露 / P2-2 Agent 定义外移 markdown）回归测试。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_harness_step7.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "harness_step7_test.db"
_TMP_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db                                       # noqa: E402

# ── 数据库隔离闸门 ──
from app import config as _cfg                                   # noqa: E402

if not _cfg.DATABASE_URL.startswith("sqlite"):
    raise SystemExit(
        f"❌ 测试中止：数据库未隔离，当前指向 "
        f"{_cfg.DATABASE_URL.split('@')[-1]}。测试必须连本地 SQLite。")

from app.agents import base as agents_base                       # noqa: E402
from app.harness import skills                                   # noqa: E402
from app.tools import data_tools, registry                                   # noqa: E402


# ══════════════════════════════════════════════════════
# P2-1 三层披露
# ══════════════════════════════════════════════════════

def test_all_skills_have_frontmatter():
    """★ 每篇 Skill 都必须有 name/description/when_to_use。

    缺 description 会让 L1 目录变成一行空壳，模型无法判断该不该加载它 ——
    等于这篇 Skill 白写。
    """
    found = skills.discover(force=True)
    assert len(found) >= 8, f"Skill 数量异常：{sorted(found)}"
    bad = []
    for name, m in found.items():
        if not m.description:
            bad.append(f"{name}: 缺 description")
        if not m.when_to_use:
            bad.append(f"{name}: 缺 when_to_use")
    assert not bad, "frontmatter 不完整：\n  " + "\n  ".join(bad)
    print(f"  ✓ {len(found)} 篇 Skill 的 frontmatter 完整")


def test_l1_catalog_is_compact():
    """★ L1 目录必须显著小于全文之和，否则渐进式披露没有意义。"""
    found = skills.discover()
    full = sum(len(skills.load_body(n)) for n in found)
    full += sum(len(skills.load_reference(n, r))
                for n, m in found.items() for r in m.references)
    catalog = len(skills.catalog_prompt())
    assert catalog < full / 8, \
        f"目录 {catalog} 未显著小于全文 {full}（要求 < 1/8）"
    print(f"  ✓ 全部内容 {full} 字符 → L1 目录仅 {catalog} 字符"
          f"（压缩至 {catalog * 100 // full}%）")


def test_main_skill_body_injected_but_refs_not():
    """★ 主 Skill 注入正文，但它的 references 不得常驻。"""
    body = skills.body_prompt("risk_governance")
    assert "治理动作存在顺序依赖" in body, "主 Skill 正文未注入"
    # 四篇细则的独有内容都不该出现在提示词里
    for marker in ("PodDisruptionBudget 时可能一次性摘掉",
                   "rows_examined / rows_sent",
                   "目标合计 = sum_node_allocatable_m"):
        assert marker not in body, f"细则内容泄漏进常驻提示词：{marker!r}"
    # 但必须告诉模型有哪些细则可取
    for ref in ("ha_rules", "capacity_rules", "db_rules", "api_rules"):
        assert ref in body, f"未列出细则 {ref}"
    print("  ✓ 主 Skill 正文常驻、4 篇细则不常驻但已列出索引")


def test_catalog_excludes_main_skill():
    """主 Skill 已全文注入，不该在目录里重复出现。"""
    cat = skills.catalog_prompt(exclude="risk_governance")
    assert "risk_governance" not in cat, "主 Skill 在目录里重复了"
    assert "sql_analytics" in cat, "其他 Skill 应出现在目录里"
    print("  ✓ 目录已排除主 Skill，无重复")


def test_load_skill_three_layers():
    """load_skill 工具能取到 L2 正文与 L3 细则。

    references 不写死清单，而是与磁盘上的文件对账：
    原来硬编码了四篇（ha/capacity/db/api），新增 cache_rules 后这条就挂了 ——
    而它想验的本意是“load_skill 能把细则列全”，不是“细则恰好有四篇”。
    """
    registry.ensure_loaded()
    r = json.loads(registry.execute("load_skill", {"name": "risk_governance"}))
    assert "治理流程" in r["content"], r.get("error")
    on_disk = {p.stem for p in (skills.SKILLS_DIR / "risk_governance" /
                                "references").glob("*.md")}
    assert set(r["references"]) == on_disk, \
        f"load_skill 列出的细则 {sorted(r['references'])} 与磁盘上的 {sorted(on_disk)} 不一致"

    r3 = json.loads(registry.execute(
        "load_skill", {"name": "risk_governance", "reference": "db_rules"}))
    assert "rows_examined / rows_sent" in r3["content"], r3.get("error")
    print(f"  ✓ L2 正文 {len(r['content'])} 字符、L3 细则 {len(r3['content'])} 字符均可取回")


def test_load_skill_rejects_unknown_and_traversal():
    """★ name/reference 都来自模型输出，必须挡住未知名与路径穿越。"""
    registry.ensure_loaded()
    bad = json.loads(registry.execute("load_skill", {"name": "no_such_skill"}))
    assert "error" in bad and "available" in bad, bad

    for evil in ("../../../etc/passwd", "../SKILL", "ha_rules/../../secrets"):
        out = json.loads(registry.execute(
            "load_skill", {"name": "risk_governance", "reference": evil}))
        assert "error" in out, f"路径穿越未拦截：{evil} → {out}"
    print("  ✓ 未知 Skill 与 3 种路径穿越全部拦截")


def test_broken_frontmatter_degrades_gracefully():
    """★ 一篇 Skill 写坏 frontmatter，不能让整个发现流程崩掉。"""
    bad_file = skills.SKILLS_DIR / "_tmp_broken.md"
    bad_file.write_text("---\nname: [unclosed\n  bad: : :\n---\n\n正文\n", encoding="utf-8")
    try:
        found = skills.discover(force=True)
        assert len(found) >= 8, "坏文件导致其他 Skill 也丢了"
        # 以 _ 开头的文件本就被跳过，这里再验证解析器自身的容错
        meta, body = skills._parse_frontmatter("---\nname: [unclosed\n---\n\nX\n")
        assert meta == {} and "X" in body, (meta, body)
    finally:
        bad_file.unlink(missing_ok=True)
        skills.discover(force=True)
    print("  ✓ frontmatter 解析失败降级为无 frontmatter，不影响其他 Skill")


def test_both_layouts_supported():
    """单文件与目录两种布局都要能发现。"""
    found = skills.discover()
    layouts = {d["name"]: d["layout"] for d in skills.describe()}
    assert layouts["risk_governance"] == "dir", layouts
    assert layouts["sql_analytics"] == "file", layouts
    print(f"  ✓ 目录布局 1 篇 + 单文件布局 {sum(1 for v in layouts.values() if v == 'file')} 篇")


# ══════════════════════════════════════════════════════
# P2-2 Agent 定义外移
# ══════════════════════════════════════════════════════

def test_agents_loaded_from_markdown():
    """★ Agent 全部从 defs/*.md 加载，不再是 Python dict。

    不写死个数，而是与目录里的文件对账：原来硬编码 7，新增 orchestrator 后就挂了，
    而它想验的本意是"每个定义文件都被加载到了"，不是"恰好有七个"。
    """
    specs = agents_base.reload_defs()
    on_disk = {p.stem for p in agents_base.DEFS_DIR.glob("*.md")
               if not p.stem.startswith("_")}
    assert set(specs) == on_disk, f"加载结果 {sorted(specs)} 与磁盘 {sorted(on_disk)} 不一致"
    for key in ("data", "topology", "risk", "diagnose", "capacity", "dbops",
                "general", "orchestrator"):
        assert key in specs, f"缺 Agent 定义：{key}"
    print(f"  ✓ {len(specs)} 个 Agent 从 markdown 加载：{sorted(specs)}")


def test_new_expert_agents_present():
    """新增的 capacity / dbops 两个专家 Agent 配置正确。"""
    specs = agents_base.AGENT_SPECS
    cap, dbo = specs["capacity"], specs["dbops"]
    assert cap["name"] == "CapacityAgent" and dbo["name"] == "DBOpsAgent"
    # 两者都应只读（专家 Agent 负责分析，治理动作留给 RiskAgent）
    registry.ensure_loaded()
    for key in ("capacity", "dbops"):
        for t in specs[key]["tools"]:
            spec = registry.get_spec(t)
            assert spec is not None, f"{key} 引用了不存在的工具 {t}"
            assert spec.is_read_only, f"{key} 含非只读工具 {t}（分析型 Agent 应只读）"
    print("  ✓ CapacityAgent / DBOpsAgent 就位，且工具集全为只读")


def test_all_referenced_tools_exist():
    """★ 定义文件里的工具名必须真实存在 —— 写错名字会让该工具静默失效。"""
    registry.ensure_loaded()
    known = set(registry.list_tools())
    problems = []
    for key, spec in agents_base.AGENT_SPECS.items():
        for t in spec["tools"]:
            if t not in known:
                problems.append(f"{key}.md 引用了不存在的工具：{t}")
    assert not problems, "\n  ".join(problems)
    print(f"  ✓ 7 个定义引用的工具全部存在（共 {len(known)} 个已注册）")


def test_all_referenced_skills_exist():
    """★ 定义里的 skill 名必须真实存在，否则主 Skill 静默不注入。"""
    found = set(skills.discover())
    problems = [f"{k}.md 的 skill={v['skill']} 不存在"
                for k, v in agents_base.AGENT_SPECS.items()
                if v["skill"] and v["skill"] not in found]
    assert not problems, "\n  ".join(problems)
    used = {v["skill"] for v in agents_base.AGENT_SPECS.values() if v["skill"]}
    print(f"  ✓ 引用的 {len(used)} 篇主 Skill 全部存在；"
          f"另有 {len(found - used)} 篇仅按需加载")


def test_model_tier_resolves_semantically():
    """★ model 用 primary/fast 语义档位，换模型只改 config 不动定义文件。"""
    specs = agents_base.AGENT_SPECS
    assert specs["data"]["model_tier"] == "fast"
    assert specs["data"]["model"] == config.LLM_MODEL_FAST
    assert specs["risk"]["model_tier"] == "primary"
    assert specs["risk"]["model"] == config.LLM_MODEL
    assert config.LLM_MODEL != config.LLM_MODEL_FAST, \
        "两个模型配成同一个，分档路由等于空转"
    # 每个 Agent 都必须显式声明档位
    for k, v in specs.items():
        assert v["model_tier"] in ("primary", "fast"), f"{k}: {v['model_tier']}"
    print(f"  ✓ fast→{config.LLM_MODEL_FAST}  primary→{config.LLM_MODEL}，7 个全部显式声明")


def test_build_agent_keeps_contract():
    """★ build_agent 返回结构不变 —— dispatch_agent / scheduler / 旧测试都依赖它。"""
    a = agents_base.build_agent("risk", "扫描风险")
    assert set(a) >= {"name", "system_prompt", "tools", "model"}, sorted(a)
    assert isinstance(a["tools"], list) and a["tools"]
    assert a["name"] == "RiskAgent"
    print(f"  ✓ 契约未变：{sorted(a)}")


def test_load_skill_always_available():
    """每个 Agent 都要有 load_skill，否则拿不到按需加载的 Skill。"""
    for key in agents_base.AGENT_SPECS:
        tools = agents_base.build_agent(key)["tools"]
        assert "load_skill" in tools, f"{key} 缺 load_skill，无法按需加载"
    print("  ✓ 7 个 Agent 均自动获得 load_skill")


def test_prompt_order_memory_last():
    """P2-3 不能被这次改造破坏：memory 仍须在提示词末尾。"""
    from app.harness import memory
    db.execute("DELETE FROM agent_memory")
    memory.remember("conclusion", "svc-x", "MEMORY_TAIL_MARKER")
    p = agents_base.build_agent("risk", "svc-x")["system_prompt"]
    assert "MEMORY_TAIL_MARKER" in p, "记忆未注入"
    tail = p[p.index("MEMORY_TAIL_MARKER") + len("MEMORY_TAIL_MARKER"):]
    assert tail.strip() == "", f"记忆后面还有内容：{tail[:80]!r}"
    print("  ✓ memory 仍在末尾（volatile 收尾未被破坏）")


def test_reload_without_restart():
    """改完 md 能热加载，不必重启进程（外移的核心价值之一）。"""
    path = agents_base.DEFS_DIR / "_tmp_probe.md"
    path.write_text(
        "---\nname: ProbeAgent\ndescription: 临时探针\n"
        "when_to_use: 测试\nmodel: fast\nallowed-tools: sql_query\n---\n\n正文\n",
        encoding="utf-8")
    try:
        specs = agents_base.reload_defs()
        assert "_tmp_probe" in specs, sorted(specs)
        assert specs["_tmp_probe"]["name"] == "ProbeAgent"
        assert specs["_tmp_probe"]["model"] == config.LLM_MODEL_FAST
    finally:
        path.unlink(missing_ok=True)
        agents_base.reload_defs()
    assert "_tmp_probe" not in agents_base.AGENT_SPECS
    print("  ✓ 新增/删除定义文件后 reload_defs() 即生效")


def test_api001_min_samples_scales_with_window():
    """★★ 接口防抖的最小样本数必须随规则窗口缩放，否则窗口调小会静默失效。

    背景（量化评测时实测发现）：原实现硬编码 `cnt < 20`。默认 5 分钟窗口下
    这个值是合理的；但把 RULE_WINDOW_MINUTES 调成 1 分钟后，
    15 个接口里有 9 个（60%）样本数掉到 20 以下被直接跳过 ——
    API-001 对它们彻底不生效，日志里却没有任何提示。
    运维为了"更快发现问题"调小窗口，反而丢掉大半覆盖面。

    这条用例同时钉住两件事：
    1. 缩放公式生效（每分钟 4 个样本）；
    2. **5 分钟仍然等于 20** —— 生产默认行为不能被这次修改改掉。
    """
    import importlib
    from app import config as cfg
    from app.rules import builtin as b

    orig = cfg.RULE_WINDOW_MINUTES
    try:
        cfg.RULE_WINDOW_MINUTES = 5
        assert b._min_samples() == 20, "5 分钟窗口下必须与历史行为一致（20）"
        cfg.RULE_WINDOW_MINUTES = 1
        assert b._min_samples() == 5, b._min_samples()
        cfg.RULE_WINDOW_MINUTES = 10
        assert b._min_samples() == 40, b._min_samples()
    finally:
        cfg.RULE_WINDOW_MINUTES = orig
    print("  ✓ 最小样本数随窗口缩放（1→5 / 5→20 / 10→40），5 分钟行为不变")


def test_query_logs_returns_recent_not_oldest():
    """★★ query_logs 必须返回【最近】的日志，并默认限定时间窗口。

    这是量化评测抓到的、后果最严重的一个缺陷：原实现
    `WHERE 1=1 ... ORDER BY ts LIMIT n` 既无时间窗口、排序还是升序，
    返回的是库里**最旧**的日志。

    真机后果：库里存着两小时前一次故障演练的日志，Agent 排查"当前"故障时
    拿到的全是那次演练的记录，于是报告"orderdb 慢查询风暴 + payment OOM
    叠加"，而实际注入的是 Redis 缓存雪崩 —— 结论完全错误，
    但它引用的每条日志都真实存在，事实核对也查不出来。

    构造：写两条 app_log，一条 3 小时前、一条刚刚。
    默认窗口下只应看到新的那条；minutes=0 时两条都能取到。
    """
    import time as _t
    now = int(_t.time())
    db.bulk_insert(db.app_logs, [
        {"ts": now - 3 * 3600, "source_pod": "old-pod", "level": "ERROR",
         "pod_ip": "10.0.0.1", "message": "VERY_OLD_MARKER 三小时前的历史故障"},
        {"ts": now, "source_pod": "new-pod", "level": "ERROR",
         "pod_ip": "10.0.0.2", "message": "FRESH_MARKER 刚刚发生"},
    ])
    out = data_tools.query_logs("app", keyword="MARKER", limit=10)
    msgs = " ".join(r["message"] for r in out["rows"])
    assert "FRESH_MARKER" in msgs, f"最新日志没返回：{msgs[:120]}"
    assert "VERY_OLD_MARKER" not in msgs, \
        f"三小时前的历史日志落进了默认窗口（会导致排障结论错误）：{msgs[:120]}"
    assert out["window_minutes"], "必须把窗口回给模型，否则它分不清'没有'与'没查到'"

    wide = data_tools.query_logs("app", keyword="MARKER", limit=10, minutes=0)
    wide_msgs = " ".join(r["message"] for r in wide["rows"])
    assert "VERY_OLD_MARKER" in wide_msgs, "minutes=0 应能取到全部历史（导出场景）"
    print("  ✓ 默认窗口只返回最新日志，minutes=0 可取全量")


def test_sql_query_flags_stale_data():
    """★★ sql_query 拿到旧数据时必须在返回里明说。

    这是上一条缺陷的**未修补面**，由新加的 C 类核对在真机上暴露出来：
    query_logs 已经有了默认时间窗口，但 sql_query 是通用查询、**没有任何时间约束**。
    模型写 `SELECT ... FROM app_logs ORDER BY ts DESC LIMIT 50` 时，
    若窗口内本来就没新数据，拿回的就是一小时前的记录 —— 而它会当成"当前状况"写进结论。
    实测：缓存雪崩场景下两种调度模式都报了
    "sql_query 返回的数据最新只到 56 / 69 分钟前，而结论在描述当前状态"。

    不强制加窗口（查历史、做趋势对比都是正当用法），但必须让模型知道手里是旧数据。
    """
    import time as _t
    now = int(_t.time())
    db.execute("DELETE FROM app_logs WHERE source_pod IN ('stale-pod', 'fresh-pod')")

    # 只有旧数据（两小时前）→ 必须提醒
    db.bulk_insert(db.app_logs, [
        {"ts": now - 7200, "source_pod": "stale-pod", "level": "ERROR",
         "pod_ip": "10.0.0.9", "message": "STALE_ONLY"}])
    out = data_tools.sql_query(
        "SELECT ts, message FROM app_logs WHERE source_pod='stale-pod' ORDER BY ts DESC")
    assert out.get("row_count"), out
    note = out.get("data_age_note", "")
    assert note, f"两小时前的数据没有任何提醒：{out}"
    assert "120 分钟前" in note or "119 分钟前" in note, note

    # 有新数据 → 不应打扰（误报会让这条提醒被忽略）
    db.bulk_insert(db.app_logs, [
        {"ts": now, "source_pod": "fresh-pod", "level": "INFO",
         "pod_ip": "10.0.0.8", "message": "FRESH_ONLY"}])
    out2 = data_tools.sql_query(
        "SELECT ts, message FROM app_logs WHERE source_pod='fresh-pod' ORDER BY ts DESC")
    assert not out2.get("data_age_note"), f"新鲜数据不该报陈旧：{out2.get('data_age_note')}"

    # 无 ts 列的查询（如配置表）不该报错也不该提醒
    out3 = data_tools.sql_query("SELECT kind, name FROM k8s_resources LIMIT 3")
    assert "data_age_note" not in out3, out3
    print("  ✓ sql_query 对陈旧结果提醒、对新数据与无时间列查询不打扰")


# ══════════════════════════════════════════════════════
# 顶层调度：model（模型自主编排） vs intent（预制意图硬映射）
# ══════════════════════════════════════════════════════

def _capture_routing(text: str, routing: str) -> dict:
    """跑一轮 handle_message，只看它选了哪个 Agent / 发了几次 LLM 分类调用。

    不真的跑模型：stub 掉 run_agent 与 llm，只验证**调度决策**本身。
    """
    from app.harness import scheduler, intent as intent_mod, llm as llm_mod
    orig = (config.AGENT_ROUTING, scheduler._run_single, intent_mod.classify,
            llm_mod.available)
    seen = {"agent": None, "llm_classify": 0}

    def fake_run_single(session_id, txt, agent_key, run=None):
        seen["agent"] = agent_key
        yield {"type": "answer", "text": "ok"}

    def counting_classify(t):
        seen["llm_classify"] += 1
        return orig[2](t)

    try:
        config.AGENT_ROUTING = routing
        scheduler._run_single = fake_run_single
        intent_mod.classify = counting_classify
        llm_mod.available = lambda: True
        seen["events"] = list(scheduler.handle_message("route-test", text))
    finally:
        (config.AGENT_ROUTING, scheduler._run_single, intent_mod.classify,
         llm_mod.available) = orig
    return seen


def test_model_routing_skips_intent_classification():
    """★ model 模式：直接交给编排 Agent，且**不花那一次 LLM 意图调用**。

    省掉的不只是一次请求：意图分类是串在用户等待路径上的，
    它返回前一个工具都发不出去。
    """
    seen = _capture_routing("先看下拓扑，再对比两个库的连接水位差异", "model")
    assert seen["agent"] == config.ORCHESTRATOR_AGENT, \
        f"model 模式应走编排 Agent，实际走了 {seen['agent']}"
    assert seen["llm_classify"] == 0, "model 模式不应再发 LLM 意图分类调用"
    ev = next(e for e in seen["events"] if e["type"] == "intent")
    assert ev["router"] == "keyword" and ev["decides_routing"] is False, \
        f"model 模式下意图只是展示标签，必须标明不决定路由：{ev}"
    print("  ✓ model 模式直达编排 Agent，零 LLM 意图调用，且意图事件标明不决定路由")


def test_intent_routing_still_works_when_switched_back():
    """★ 切回 intent 模式：旧的硬映射与 LLM 意图分类依旧生效。

    这条守的是可回退能力 —— 两种调度要能用同一套评测对比，
    旧路径就不能在重构里静默坏掉。
    """
    seen = _capture_routing("执行风险扫描", "intent")
    assert seen["llm_classify"] == 1, "intent 模式应调一次意图分类"
    assert seen["agent"] in scheduler_intent_agents(), \
        f"intent 模式应按硬映射选 Agent，实际 {seen['agent']}"
    ev = next(e for e in seen["events"] if e["type"] == "intent")
    assert ev["decides_routing"] is True
    print(f"  ✓ intent 模式可回退（选了 {seen['agent']}，意图决定路由）")


def scheduler_intent_agents() -> set:
    from app.harness import scheduler
    return set(scheduler.INTENT_AGENT.values())


def test_keyword_classify_costs_no_llm():
    """classify_keyword 必须真的不碰 LLM（否则“省一次往返”就是空话）。"""
    from app.harness import intent as intent_mod, llm as llm_mod
    orig = llm_mod.available
    try:
        # 就算 LLM 可用，关键词分类也不得去调它
        llm_mod.available = lambda: (_ for _ in ()).throw(
            AssertionError("classify_keyword 里不应碰 llm.available"))
        r = intent_mod.classify_keyword("做一次全面体检")
    finally:
        llm_mod.available = orig
    assert r == {"intent": "full_checkup", "entities": {}, "router": "keyword"}, r
    print("  ✓ classify_keyword 零 LLM 依赖，分类结果正确")


def main():
    db.init_db()
    registry.ensure_loaded()
    groups = [
        ("P2-1 · 三层披露", [
            test_query_logs_returns_recent_not_oldest,
            test_sql_query_flags_stale_data,
            test_api001_min_samples_scales_with_window,
            test_all_skills_have_frontmatter,
            test_l1_catalog_is_compact,
            test_main_skill_body_injected_but_refs_not,
            test_catalog_excludes_main_skill,
            test_both_layouts_supported,
        ]),
        ("P2-1 · load_skill 工具与安全", [
            test_load_skill_three_layers,
            test_load_skill_rejects_unknown_and_traversal,
            test_broken_frontmatter_degrades_gracefully,
        ]),
        ("P2-2 · Agent 定义外移", [
            test_agents_loaded_from_markdown,
            test_new_expert_agents_present,
            test_all_referenced_tools_exist,
            test_all_referenced_skills_exist,
            test_model_tier_resolves_semantically,
        ]),
        ("顶层调度：model vs intent", [
            test_model_routing_skips_intent_classification,
            test_intent_routing_still_works_when_switched_back,
            test_keyword_classify_costs_no_llm,
        ]),
        ("P2-2 · 契约与兼容", [
            test_build_agent_keeps_contract,
            test_load_skill_always_available,
            test_prompt_order_memory_last,
            test_reload_without_restart,
        ]),
    ]
    passed = failed = 0
    for title, tests in groups:
        print(f"\n=== {title} ===")
        for fn in tests:
            try:
                fn()
                passed += 1
            except Exception as e:                # noqa: BLE001
                failed += 1
                print(f"  ✗ {fn.__name__}: {e}")
                import traceback
                traceback.print_exc()
    print("\n" + "=" * 46)
    print(f"通过 {passed} / 失败 {failed}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
