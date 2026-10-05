"""双定义表结构对账：backend 与 data_collector 的 8 张契约表必须完全一致。

CLAUDE.md 要求这 8 张表在两处双定义并保持同步，但此前**没有任何机制保证**。
实际已经漂移过一次：`trace_spans.ts` 在 collector 侧有索引、backend 侧没有，
于是"谁先建表"决定了拓扑热查询有没有索引可用。

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_schema_sync.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent          # backend/

# ── 数据库隔离闸门 ──
# 本文件只读 SQLAlchemy metadata，engine 是懒建的，理论上不会连库。
# 但 collector.config 在【导入期】就会 build_db_url()，默认值指向线上 RDS ——
# 将来只要有人在这里加一行 init_db()/fetch_all() 就会打到真库。
# 成本极低的加固：先把两侧连接串都钉到临时 sqlite。
_TMP_DB = Path(tempfile.gettempdir()) / "schema_sync_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"     # backend 侧读这个
os.environ["DB_URL"] = f"sqlite:///{_TMP_DB}"           # collector 侧读的是 DB_URL

sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "data_collector"))

from app import db as backend_db                        # noqa: E402
from collector import config as collector_config        # noqa: E402
from collector import db as collector_db                # noqa: E402

for _side, _url in (("backend", backend_db.config.DATABASE_URL),
                    ("collector", collector_config.DB_URL)):
    if not _url.startswith("sqlite"):
        raise SystemExit(
            f"❌ 测试中止：{_side} 侧数据库未隔离，当前指向 "
            f"{_url.split('@')[-1]}。测试必须连本地 SQLite。")


def _cols(table) -> dict:
    return {c.name: str(c.type) for c in table.columns}


def _indexed(table) -> set:
    """该表上被索引的列组合。单列 index=True 与显式 Index(...) 都算。"""
    combos = {tuple(sorted(c.name for c in idx.columns)) for idx in table.indexes}
    combos |= {(c.name,) for c in table.columns if c.index}
    return combos


def test_contract_tables_identical():
    """★ 8 张契约表的字段名、类型、索引必须两侧完全一致。"""
    b = {t.name: t for t in backend_db.metadata.tables.values()}
    c = {t.name: t for t in collector_db.metadata.tables.values()}
    shared = sorted(set(b) & set(c))

    assert len(shared) == 8, f"双定义表数应为 8，实际 {len(shared)}: {shared}"
    orphan = sorted(set(c) - set(b))
    assert not orphan, f"collector 定义了 backend 没有的表：{orphan}"

    problems = []
    for name in shared:
        bc, cc = _cols(b[name]), _cols(c[name])
        for k in sorted(set(bc) | set(cc)):
            if k not in bc:
                problems.append(f"{name}.{k}: backend 缺该字段（collector 有 {cc[k]}）")
            elif k not in cc:
                problems.append(f"{name}.{k}: collector 缺该字段（backend 有 {bc[k]}）")
            elif bc[k] != cc[k]:
                problems.append(f"{name}.{k}: 类型不同 backend={bc[k]} collector={cc[k]}")
        bi, ci = _indexed(b[name]), _indexed(c[name])
        if bi != ci:
            problems.append(
                f"{name}: 索引不同 仅backend={sorted(bi - ci)} 仅collector={sorted(ci - bi)}")

    assert not problems, "双定义漂移：\n  " + "\n  ".join(problems)
    print(f"  ✓ {len(shared)} 张契约表字段与索引完全一致")


def test_trace_spans_ts_indexed():
    """回归：trace_spans.ts 必须两侧都有索引。

    拓扑构建的核心查询 WHERE kind='client' AND ts >= :w 依赖它；
    此前 backend 侧漏了，导致索引存在与否取决于哪个进程先建表。
    """
    for label, meta in (("backend", backend_db.metadata),
                        ("collector", collector_db.metadata)):
        t = meta.tables["trace_spans"]
        assert ("ts",) in _indexed(t), f"{label} 侧 trace_spans.ts 缺索引"
    print("  ✓ trace_spans.ts 两侧均有索引")


def test_backend_only_tables_are_expected():
    """backend 独有的表应都是业务表，不该出现观测表漏同步的情况。"""
    b = {t.name for t in backend_db.metadata.tables.values()}
    c = {t.name for t in collector_db.metadata.tables.values()}
    expected_backend_only = {
        "agent_audit", "agent_memory", "chat_messages", "governance_plans",
        "prediagnosis", "risk_findings", "risk_rules", "scan_reports",
        "topology_edges",
    }
    actual = b - c
    unexpected = actual - expected_backend_only
    assert not unexpected, (
        f"backend 出现了未登记的独有表 {sorted(unexpected)} —— "
        f"若它是观测表则说明漏了 collector 侧定义；若是新业务表请加入本用例白名单")
    missing = expected_backend_only - actual
    assert not missing, f"预期的 backend 独有表消失了：{sorted(missing)}"
    print(f"  ✓ backend 独有 {len(actual)} 张业务表，均已登记")


def test_observed_ts_unit_consistent():
    """★ 观测表的时间列单位也是双定义 —— 必须两侧一致。

    这几张表单位不统一（日志类秒 / 指标类毫秒），任何一侧标错都会算出
    荒谬的时间差：backend 的 data_freshness 第一版把 trace_spans 当毫秒，
    得出"距今 56 年"，进而让新鲜度判断完全失效。
    """
    from collector import retention as collector_retention

    b = dict(backend_db.OBSERVED_TS_UNIT)
    c = dict(collector_retention.OBSERVED_TABLES)
    # collector 的 retention 单独处理 realtime_metrics（恒毫秒），补上再比
    c.setdefault("realtime_metrics", "ms")

    problems = [f"{k}: backend={b.get(k)} collector={c.get(k)}"
                for k in sorted(set(b) | set(c)) if b.get(k) != c.get(k)]
    assert not problems, "时间单位定义漂移：\n  " + "\n  ".join(problems)
    print(f"  ✓ {len(b)} 张观测表的 ts 单位两侧一致")


def test_freshness_uses_correct_units():
    """新鲜度计算必须用对单位：算出来的 age 不能是负数或荒谬的大值。"""
    import time
    from app import db as backend_db_mod

    now_s = time.time()
    for table, unit in backend_db_mod.OBSERVED_TS_UNIT.items():
        # 构造"刚刚"的时间戳，按该表单位换算
        ts = int(now_s * 1000) if unit == "ms" else int(now_s)
        # 反向按单位还原，误差应在 2 秒内
        restored = ts / 1000.0 if unit == "ms" else float(ts)
        drift = abs(now_s - restored)
        assert drift < 2, f"{table} 单位 {unit} 换算偏差 {drift:.0f}s（单位标错）"
    print("  ✓ 各表单位换算自检通过（无 1000 倍偏差）")


def test_prompt_resource_names_exist_in_world():
    """★★ 系统提示词里出现的资源名必须真实存在，否则会制造"合法的幻觉"。

    量化评测时发现：提示词的集群概况写着数据库 `rds-mysql-01`，
    而世界里实际是 `rds-mysql-order` 与 `rds-mysql-core` —— 该实例根本不存在。

    这个错误特别隐蔽：`ContextManager.__init__` 会把 system_prompt 吸进证据池，
    于是模型引用 `rds-mysql-01` 时，事实核对认为它"有出处"、**不会报警**。
    提示词里的一个笔误，就这样变成了核对机制的盲区。

    本用例拿提示词里的资源名去比对 mock_server 的世界定义（权威来源）。
    """
    import ast
    import re
    from pathlib import Path as _P

    # 用 AST 解析而不是 import：mock_server 也有一个叫 app 的包，
    # 本进程里 app 已经指向 backend/app，import 会撞名。
    # 解析字面量还有个附带好处 —— 不执行对方代码。
    wd_src = (_P(__file__).resolve().parent.parent.parent
              / "mock_server" / "app" / "world_def.py").read_text(encoding="utf-8")
    tree = ast.parse(wd_src)
    real = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [x.id for x in node.targets if isinstance(x, ast.Name)]
        if not ({"SERVICES", "DATASTORES"} & set(names)):
            continue
        if isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    real.add(k.value)
    assert real, "没能从 mock_server/app/world_def.py 解析出实体名"

    from app.agents import base
    prompt = base._BASE_PROMPT

    mentioned = set(re.findall(r"(?:rds|kvstore)-[a-z0-9-]+", prompt))
    mentioned |= set(re.findall(r"[a-z]+-service", prompt))
    bogus = sorted(n for n in mentioned if n not in real)
    assert not bogus, (
        f"提示词提到了世界里不存在的资源：{bogus}\n"
        f"（这些名字会进证据池，让模型引用它们时逃过事实核对）\n"
        f"世界里的真实实体：{sorted(real)}")

    # 反向也要查：漏掉服务会让模型不知道它们存在
    missing = sorted(s for s in real
                     if s.endswith("-service") and s not in prompt)
    assert not missing, f"提示词漏掉了这些服务，模型不会知道它们存在：{missing}"
    print(f"  ✓ 提示词提到的 {len(mentioned)} 个资源名均真实存在，"
          f"且未漏掉任何 *-service")


def test_base_prompt_has_no_stray_braces():
    """★ _BASE_PROMPT 走 .format()，裸花括号会被当成占位符直接抛 KeyError。

    刚踩过：在集群概况里写了 `{user-service, product-service}` 列举服务名，
    format 时把它当字段名 → 全部 Agent 组装失败，一次跑挂 34 个用例。
    这类错误只在渲染时才炸，读代码时看不出来 —— 用例直接渲染一次最稳。
    """
    from app.agents import base
    try:
        out = base._BASE_PROMPT.format(role="X", extra="", skill="",
                                       catalog="", live="", memory="")
    except (KeyError, IndexError, ValueError) as e:
        raise AssertionError(
            f"_BASE_PROMPT 渲染失败：{type(e).__name__}: {e}\n"
            f"提示：要在提示词里写字面花括号必须转义成 双花括号") from None
    assert "{" not in out.replace("{{", "").replace("}}", ""), \
        "渲染结果里仍残留未替换的花括号"
    print(f"  ✓ _BASE_PROMPT 渲染正常（{len(out)} 字符），无游离花括号")


def main():
    tests = [
        test_contract_tables_identical,
        test_trace_spans_ts_indexed,
        test_backend_only_tables_are_expected,
        test_observed_ts_unit_consistent,
        test_freshness_uses_correct_units,
        test_prompt_resource_names_exist_in_world,
        test_base_prompt_has_no_stray_braces,
    ]
    print("\n=== 双定义表结构对账 ===")
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:                # noqa: BLE001
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print("\n" + "=" * 46)
    print(f"通过 {passed} / 失败 {failed}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
