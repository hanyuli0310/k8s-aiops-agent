"""Skill / Agent 文档准确性常驻测试。

## 为什么需要这一档测试

`test_harness_step7.py` 只校验 frontmatter 完整性 —— 那保证的是「有描述」，
不是「描述是对的」。而 8 篇 Skill + 7 个 Agent 定义里密集出现着**对代码的引用**：

    工具名  参数名  枚举值  表名  列名  evidence 字段名  规则编号  阈值常量

这些引用一旦随代码漂移，症状是模型照着旧名字调用，得到
`no such column` / `unknown parameter` / 拿不到 evidence 字段 ——
**而且只在真实对话里才暴露**，任何单元测试都不会红。

本文件把每一类引用都做成机械对账：文档里出现的标识符必须能在代码里找到出处。
唯一权威来源分别是：

| 引用类型 | 权威来源 |
|---|---|
| 工具名 / 参数名 / 枚举值 | `tools/registry` 的注册表 |
| 表名 / 列名 | `db.metadata`（SQLAlchemy 表定义） |
| 可查询表白名单 | `tools/data_tools._ALLOWED_TABLES` |
| evidence 字段名 | AST 解析 `rules/builtin.py` 里的 `"evidence": {...}` |
| 规则编号 / 阈值 | `rules/builtin.BUILTIN_RULE_META` 与阈值常量 |
| Skill / reference 名 | `harness/skills.discover()` |
| 可派发子 Agent | `agents/base.dispatchable_keys()` |

零依赖，直接运行：
    cd backend && .venv/bin/python tests/test_skill_accuracy.py
"""
from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "skill_accuracy_test.db"
_TMP_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as _cfg                                   # noqa: E402

# ── 数据库隔离闸门（本文件只读元数据，仍按统一规范挡一道）──
if not _cfg.DATABASE_URL.startswith("sqlite"):
    raise SystemExit(
        f"❌ 测试中止：数据库未隔离，当前指向 "
        f"{_cfg.DATABASE_URL.split('@')[-1]}。测试必须连本地 SQLite。")

from app import db                                               # noqa: E402
from app.agents import base as agents_base                       # noqa: E402
from app.harness import scheduler, skills                        # noqa: E402
from app.rules import builtin                                    # noqa: E402
from app.tools import data_tools, registry                       # noqa: E402

registry.ensure_loaded()

APP_DIR = Path(__file__).resolve().parent.parent / "app"


# ══════════════════════════════════════════════════════
# 语料：待校验的全部 markdown
# ══════════════════════════════════════════════════════

def _load_corpus() -> dict:
    """{文档标识: 正文}。Skill 走 skills 模块（与运行时同一条读取路径）。"""
    corpus = {}
    for name, meta in skills.discover(force=True).items():
        corpus[name] = skills.load_body(name)
        for ref in meta.references:
            corpus[f"{name}/{ref}"] = skills.load_reference(name, ref)
    for path in sorted(agents_base.DEFS_DIR.glob("*.md")):
        if not path.stem.startswith("_"):
            corpus[f"agent:{path.stem}"] = path.read_text(encoding="utf-8")
    return corpus


CORPUS = _load_corpus()


# ══════════════════════════════════════════════════════
# 权威来源
# ══════════════════════════════════════════════════════

TOOLS = set(registry.list_tools())
TOOL_PARAMS = {}          # tool -> {param: schema}
for _t in TOOLS:
    TOOL_PARAMS[_t] = registry.get_spec(_t).schema["function"]["parameters"].get(
        "properties", {})
ALL_PARAM_NAMES = {p for props in TOOL_PARAMS.values() for p in props}
ALL_ENUM_VALUES = {str(v) for props in TOOL_PARAMS.values() for s in props.values()
                   for v in (s.get("enum") or [])}

TABLES = set(db.metadata.tables)
COLUMNS = {c.name for t in db.metadata.tables.values() for c in t.columns}
QUERYABLE_TABLES = set(data_tools._ALLOWED_TABLES)

SKILL_NAMES = set(skills.discover())
REFERENCE_NAMES = {r for m in skills.discover().values() for r in m.references}
RULE_IDS = {r[0] for r in builtin.BUILTIN_RULE_META}


def _evidence_keys() -> set:
    """AST 解析 builtin.py 里所有 `"evidence": {...}` 字面量的键（含嵌套）。

    用 AST 而不是执行规则函数：规则要连库跑扫描才有 finding，
    而空库跑不出 CAP-003 这类需要真实快照的规则，键集会不完整。
    """
    tree = ast.parse((APP_DIR / "rules" / "builtin.py").read_text(encoding="utf-8"))
    keys = set()

    def collect(node: ast.Dict):
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            if isinstance(v, ast.Dict):
                collect(v)

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "evidence"
                        and isinstance(v, ast.Dict)):
                    collect(v)
    return keys


EVIDENCE_KEYS = _evidence_keys()


def _fault_scenarios() -> set:
    """mock_server 定义的故障场景 id。

    做成权威来源而不是登记豁免：dbops.md 已经引用了 `slow_query_storm`
    来说明"稳态下 slow_logs 为空"，将来别的 Skill 也可能引用场景名。
    从 faults.py 的 SCENARIOS 字面量提取，场景改名会立刻被抓到。
    用 AST 而不是 import：mock_server 是独立进程的包，导入它会牵进 FastAPI 等依赖。
    """
    path = APP_DIR.parent.parent / "mock_server" / "app" / "faults.py"
    if not path.is_file():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "SCENARIOS":
                value = node.value
                if isinstance(value, ast.Dict):
                    return {k.value for k in value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


FAULT_SCENARIOS = _fault_scenarios()


def _world_actions() -> set:
    """mock_server 支持的世界动作名（ACTION_TYPES）。

    为何做成权威来源而不是登记豁免：cache_rules 里需要告知模型
    `scale_out` 对缓存实例不适用（实测报 service not found），
    这类“平台能力边界”的叙述就会提到动作名。放进 _KNOWN_OTHER 等于不再校验，
    写成 `scale_up` 也没人拦；从 ACTION_TYPES 解析则动作改名会立即被抓到。
    同样用 AST（mock_server 是独立进程的包，导入会牵进 FastAPI）。
    """
    path = APP_DIR.parent.parent / "mock_server" / "app" / "actions.py"
    if not path.is_file():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "ACTION_TYPES":
                if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                    return {e.value for e in node.value.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return set()


WORLD_ACTIONS = _world_actions()


def _module_constants(*rel_paths) -> set:
    """模块级 UPPER_SNAKE 常量名（文档里引用常量名时用它对账）。"""
    out = set()
    for rel in rel_paths:
        tree = ast.parse((APP_DIR / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target] if isinstance(node, ast.AnnAssign) else [])
            for t in targets:
                if isinstance(t, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]*", t.id):
                    out.add(t.id)
    return out


CONSTANTS = _module_constants("rules/builtin.py", "config.py")


def _full_ingest_tables() -> list:
    """run_full_ingest 里被清空重建的表 —— data_ingestion.md 的「七张观测表」即此。"""
    tree = ast.parse((APP_DIR / "ingest" / "pipeline.py").read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "run_full_ingest":
            for node in ast.walk(fn):
                if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
                    return [e.value for e in node.iter.elts
                            if isinstance(e, ast.Constant)]
    return []


INGEST_TABLES = _full_ingest_tables()


# 既不是工具/参数/枚举/表/列/Skill/evidence 字段，也不是规则编号的标识符，
# 必须在这里显式登记来源 —— 这份清单刻意保持短小：
# 每加一条都是在说「这个 token 不受代码约束」，多了就说明对账在放水。
_KNOWN_OTHER = {
    # 数据源侧的字段名（阿里云 SLS / CMS 的原始字段，不由本项目定义）
    "__source__", "__time__", "timestamp", "attribute", "resource", "rds_slow_log",
    "trace",
    # 运行模式（config.DATA_SOURCE 的两个取值）
    "static", "live",
    # 工具结果预览的结构键（harness/tool_results.py 生成）
    "_total",
    # finding 结构里的字段名（不是 evidence 内部的键）
    "evidence",
    # 被管 RDS 上的业务库对象（不在本平台白名单内，仅作为慢 SQL 样本出现）
    "orders", "user_id",
    # K8s / 容器语义词
    "cpu", "default",
    # SQLite JSON 函数
    "json_extract",
    # ★ 故意写错的反面例子 —— 文档明确说「不是 avg_value」「容易写成 k8s_resource」
    "avg_value", "k8s_resource",
}

# SQL 关键字与函数（校验 sql 代码块里的标识符时的允许集）
_SQL_WORDS = {
    "select", "from", "where", "group", "by", "order", "having", "limit", "offset",
    "and", "or", "not", "in", "is", "null", "like", "as", "case", "when", "then",
    "else", "end", "distinct", "asc", "desc", "join", "left", "inner", "on",
    "union", "all", "between", "delete", "insert", "update",
    "count", "min", "max", "avg", "sum", "round", "cast", "coalesce", "abs",
    "strftime", "substr", "length", "json_extract", "datetime", "date", "now",
}


# ══════════════════════════════════════════════════════
# 解析工具
# ══════════════════════════════════════════════════════

_FENCE_RE = re.compile(r"```([\w-]*)\n(.*?)```", re.S)
_INLINE_RE = re.compile(r"`([^`\n]+)`")
_IDENT_SPAN_RE = re.compile(r"^([a-z_][a-z0-9_]*)\s*(?:\(.*\))?$")
_UPPER_SPAN_RE = re.compile(r"^([A-Z][A-Z0-9_]{3,})$")
# 调用式：允许名字与左括号之间夹一个反引号（`query_logs`(logstore=slow) 这种写法）
_CALL_RE = re.compile(r"\b([a-z_][a-z0-9_]*)`?\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)")


def _prose(text: str) -> str:
    """去掉围栏代码块后的正文（代码块单独校验，避免两套规则互相干扰）。"""
    return _FENCE_RE.sub("", text)


def _split_args(s: str) -> list:
    """按顶层逗号切参数 —— columns=["status","created_at"] 里的逗号不能切。"""
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def _tool_calls() -> list:
    """扫出全部 `tool(args)` 形式的引用，返回 (doc, tool, {param: 值原文})。"""
    out = []
    for doc, text in CORPUS.items():
        for m in _CALL_RE.finditer(text):
            fn, raw = m.group(1), m.group(2)
            if fn not in TOOLS:
                continue
            kv = {}
            for part in _split_args(raw):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if re.fullmatch(r"[a-z_][a-z0-9_]*", k.strip()):
                        kv[k.strip()] = v.strip()
            out.append((doc, fn, kv))
    return out


TOOL_CALLS = _tool_calls()


def _sql_statements() -> list:
    """从 ```sql 块里切出语句，返回 (doc, sql, negative, external)。

    两类需要跳过的语句都靠**块内注释标记**识别，而不是靠文件名硬编码：
      · `-- ❌ …`      故意写错的反面例子（下一条 `-- ✅` 复位）
      · `-- [外部SQL]` 跑在被管 RDS 业务库上的语句，表不在本平台白名单里
    """
    out = []
    for doc, text in CORPUS.items():
        for lang, block in _FENCE_RE.findall(text):
            if lang.lower() != "sql":
                continue
            negative = external = False
            buf = []

            def flush(neg=None, ext=None):
                if buf:
                    out.append((doc, "\n".join(buf), neg, ext))
                    buf.clear()

            for line in block.splitlines():
                stripped = line.strip()
                if stripped.startswith("--"):
                    flush(negative, external)
                    if "❌" in stripped:
                        negative = True
                    if "✅" in stripped:
                        negative = False
                    if "[外部SQL]" in stripped:
                        external = True
                    continue
                if not stripped:
                    flush(negative, external)
                    continue
                buf.append(stripped)
            flush(negative, external)
    return out


SQL_STATEMENTS = _sql_statements()


def _sql_idents(stmt: str) -> set:
    """语句里的标识符（先剥掉字符串字面量，避免把 'ERROR' 当列名）。"""
    body = re.sub(r"'[^']*'", " ", stmt)
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body))


# ══════════════════════════════════════════════════════
# 1. 工具引用
# ══════════════════════════════════════════════════════

def test_skill_allowed_tools_exist():
    """★ Skill 的 allowed-tools 必须是真实工具名。

    这个字段的作用是告诉读者/模型「这篇方法论配套哪些工具」，
    写错不会报错，只会让人按不存在的工具名去调。
    """
    bad = []
    for name, meta in skills.discover().items():
        for t in meta.allowed_tools:
            if t not in TOOLS:
                bad.append(f"{name}: allowed-tools 含不存在的工具 {t!r}")
    assert not bad, "\n  " + "\n  ".join(bad)
    total = sum(len(m.allowed_tools) for m in skills.discover().values())
    print(f"  ✓ Skill 声明的 {total} 处 allowed-tools 全部命中注册表")


def test_documented_tool_params_exist():
    """★ 文档里 `tool(param=…)` 的参数名必须真实存在。

    参数名写错的后果是模型原样照抄 → 工具报 unexpected keyword。
    HA-004 的 `create_pdb(app=…)`（参数名是 app 不是 name）就是文档专门强调的点，
    它本身也必须被验证。
    """
    bad = []
    for doc, fn, kv in TOOL_CALLS:
        for k in kv:
            if k not in TOOL_PARAMS[fn]:
                bad.append(f"[{doc}] {fn}({k}=…) —— {fn} 没有参数 {k!r}，"
                           f"实际参数：{sorted(TOOL_PARAMS[fn])}")
    assert not bad, "\n  " + "\n  ".join(bad)
    print(f"  ✓ {len(TOOL_CALLS)} 处工具调用示例的参数名全部有效")


def test_documented_enum_values_valid():
    """★ 枚举型参数的取值必须在 enum 里（patch_deployment 的 6 个 action 是重点）。"""
    bad = []
    checked = 0
    for doc, fn, kv in TOOL_CALLS:
        for k, raw in kv.items():
            enum = (TOOL_PARAMS[fn].get(k) or {}).get("enum")
            if not enum:
                continue
            v = raw.strip().strip("\"'`").strip()
            # 占位符（value=…、app=服务名）不是真实取值，跳过
            if not v or not v.isascii() or v in ("...", "…"):
                continue
            checked += 1
            if v not in enum:
                bad.append(f"[{doc}] {fn}({k}={v!r}) 不在 enum {enum} 内")
    assert not bad, "\n  " + "\n  ".join(bad)
    print(f"  ✓ {checked} 处枚举取值全部合法")


def test_documented_load_skill_targets_exist():
    """★ 文档里示范的 load_skill 目标必须存在，否则模型照抄就拿到 error。"""
    bad = []
    for doc, fn, kv in TOOL_CALLS:
        if fn != "load_skill":
            continue
        ref = kv.get("reference", "").strip().strip("\"'`")
        if ref in ("", "...", "…"):              # 占位符写法，不是真实细则名
            continue
        if ref not in REFERENCE_NAMES:
            bad.append(f"[{doc}] load_skill(reference={ref!r}) 不存在，"
                       f"现有细则：{sorted(REFERENCE_NAMES)}")
    assert not bad, "\n  " + "\n  ".join(bad)
    print(f"  ✓ load_skill 示例指向的细则均存在（共 {len(REFERENCE_NAMES)} 篇）")


# ══════════════════════════════════════════════════════
# 2. 表名与列名
# ══════════════════════════════════════════════════════

def test_sql_examples_use_queryable_tables():
    """★ ```sql 示例里的表必须在 sql_query 白名单内。

    示例是给模型抄的。抄一张白名单外的表，得到的是「不允许访问表」而不是数据。
    """
    bad = []
    for doc, stmt, negative, external in SQL_STATEMENTS:
        if external:
            continue
        for t in re.findall(r"\b(?:from|join)\s+([a-z0-9_]+)", stmt, re.I):
            if t.lower() not in QUERYABLE_TABLES:
                bad.append(f"[{doc}] 示例查了非白名单表 {t!r}：{stmt[:70]}")
    assert not bad, ("\n  " + "\n  ".join(bad)
                     + "\n  （若确实是被管 RDS 业务库上的语句，请加 `-- [外部SQL]` 标记）")
    print(f"  ✓ {len(SQL_STATEMENTS)} 条 SQL 示例的表名均在 {len(QUERYABLE_TABLES)} 张白名单内")


def test_sql_examples_columns_exist():
    """★★ ```sql 示例里的列名必须真实存在 —— 本文件最核心的一条。

    列改名是最容易发生的漂移，而文档里的 SQL 会被模型整段抄走。
    允许集 = SQL 关键字/函数 ∪ 涉及表的真实列 ∪ 语句里 AS 定义的别名。
    """
    bad = []
    for doc, stmt, negative, external in SQL_STATEMENTS:
        if negative or external:
            continue
        tables = {t.lower() for t in
                  re.findall(r"\b(?:from|join)\s+([a-z0-9_]+)", stmt, re.I)}
        allowed = set(_SQL_WORDS) | tables
        for t in tables:
            table = db.metadata.tables.get(t)
            if table is not None:
                allowed |= {c.name for c in table.columns}
        # AS 定义的别名在本语句内合法
        allowed |= {a.lower() for a in re.findall(r"\bas\s+([a-z_][a-z0-9_]*)",
                                                 stmt, re.I)}
        unknown = sorted(i for i in _sql_idents(stmt) if i.lower() not in allowed)
        if unknown:
            bad.append(f"[{doc}] 未知标识符 {unknown}：{stmt[:70]}")
    assert not bad, "\n  " + "\n  ".join(bad)
    checked = sum(1 for _, _, n, e in SQL_STATEMENTS if not n and not e)
    print(f"  ✓ {checked} 条可执行 SQL 示例的列名全部对得上表定义")


def test_negative_sql_example_is_really_wrong():
    """反面例子必须真的错 —— 若代码后来加了 avg_value 列，那段警告就该删掉。"""
    negatives = [(d, s) for d, s, n, e in SQL_STATEMENTS if n]
    assert negatives, "sql_analytics 里的 ❌ 反例块没被识别出来（解析逻辑坏了）"
    for doc, stmt in negatives:
        idents = {i.lower() for i in _sql_idents(stmt)}
        assert idents - set(_SQL_WORDS) - COLUMNS - TABLES, \
            f"[{doc}] 标为 ❌ 的示例里所有标识符都合法，这个反例已经失效：{stmt}"
    print(f"  ✓ {len(negatives)} 段 ❌ 反例仍然是真的错（引用了不存在的列）")


def test_ts_unit_claims_match_code():
    """★ 「哪些表是秒、哪些是毫秒」必须与 db.OBSERVED_TS_UNIT 一致。

    这个知识点在两篇 Skill 里各写了一遍。写反的后果是模型算出几十年的时间差
    —— data_freshness 第一版就踩过（把 trace_spans 当毫秒，得出"距今 56 年"）。
    """
    sec = {t for t, u in db.OBSERVED_TS_UNIT.items() if u == "s"}
    ms = {t for t, u in db.OBSERVED_TS_UNIT.items() if u == "ms"}
    bad = []
    for doc in ("sql_analytics", "data_ingestion"):
        seen = set()
        # 「清单行」＝出现单位词且列出 >= 2 张表的行。只提到 1 张表的行是行文说明
        # （如"注意 trace_spans 的 ts 是秒"），不是清单，不能拿来对账。
        for line in CORPUS[doc].splitlines():
            listed = {t for t in TABLES if f"`{t}`" in line}
            if len(listed) < 2:
                continue
            if "毫秒" in line:
                seen.add("ms")
                if listed != ms:
                    bad.append(f"[{doc}] 毫秒表清单 {sorted(listed)} != 代码 {sorted(ms)}")
            elif re.search(r"(^|[^毫])秒", line):
                seen.add("s")
                if listed != sec:
                    bad.append(f"[{doc}] 秒表清单 {sorted(listed)} != 代码 {sorted(sec)}")
        # 覆盖度断言：万一文档把清单删了或改写成识别不出的形式，不能静默通过
        if seen != {"s", "ms"}:
            bad.append(f"[{doc}] 没找到完整的秒/毫秒表清单（只识别出 {sorted(seen) or '无'}）")
    assert not bad, "\n  " + "\n  ".join(bad)
    print(f"  ✓ 两篇 Skill 的时间单位清单与代码一致（秒 {len(sec)} 张 / 毫秒 {len(ms)} 张）")


def test_queryable_table_list_in_doc_matches_code():
    """★ sql_analytics 里逐个列出的白名单表必须与代码集合完全相等。

    多写一张 → 模型被拒；少写一张 → 那张表实际上没人会去查。
    """
    text = CORPUS["sql_analytics"]
    listed = {t for t in TABLES if f"`{t}`" in text}
    missing = QUERYABLE_TABLES - listed
    extra = listed - QUERYABLE_TABLES
    assert not missing, f"白名单表未在文档中出现：{sorted(missing)}"
    assert not extra, f"文档提到了非白名单表：{sorted(extra)}"
    # description 在 frontmatter 里（load_body 只返回正文），单独取
    desc = skills.discover()["sql_analytics"].description
    assert f"{len(QUERYABLE_TABLES)} 张白名单表" in desc, \
        f"description 里的表数量与代码不符（应为 {len(QUERYABLE_TABLES)}）：{desc}"
    print(f"  ✓ 文档列出的 {len(listed)} 张可查询表与代码集合完全一致")


def test_ingest_table_list_matches_pipeline():
    """★ data_ingestion 讲的「七张观测表」必须与 run_full_ingest 清空的表一致。"""
    assert INGEST_TABLES, "没能从 pipeline.py 解析出全量采集的表清单"
    text = CORPUS["data_ingestion"]
    missing = [t for t in INGEST_TABLES if f"`{t}`" not in text]
    assert not missing, f"采集会重建但文档没讲的表：{missing}"
    cn = {7: "七", 6: "六", 8: "八", 9: "九", 10: "十"}.get(len(INGEST_TABLES))
    assert cn and f"{cn}张观测表" in text, \
        f"文档里的表数量措辞与实际 {len(INGEST_TABLES)} 张不符"
    print(f"  ✓ 全量采集的 {len(INGEST_TABLES)} 张表在文档中逐张有交代")


# ══════════════════════════════════════════════════════
# 3. 规则、evidence 字段与阈值
# ══════════════════════════════════════════════════════

def test_referenced_rule_ids_exist():
    """★ 文档提到的规则编号必须是内置规则（AI-xxx 是用户自建示例，排除）。"""
    bad = []
    for doc, text in CORPUS.items():
        for rid in set(re.findall(r"\b(HA|CAP|DB|API)-(\d{3})\b", text)):
            full = f"{rid[0]}-{rid[1]}"
            if full not in RULE_IDS:
                bad.append(f"[{doc}] 引用了不存在的规则 {full}")
    assert not bad, "\n  " + "\n  ".join(bad)
    print(f"  ✓ 文档引用的规则编号全部属于 {len(RULE_IDS)} 条内置规则")


def test_all_rules_are_documented():
    """★ 反向覆盖：11 条规则每条都要在某篇细则里讲到，不能有「没人管」的规则。"""
    refs = " ".join(v for k, v in CORPUS.items() if k.startswith("risk_governance"))
    missing = [r for r in sorted(RULE_IDS) if r not in refs]
    assert not missing, f"这些规则没有任何细则文档讲解：{missing}"
    assert f"{len(RULE_IDS)} 条内置规则" in CORPUS["risk_governance"], \
        f"总纲里的规则条数与代码不符（应为 {len(RULE_IDS)}）"
    print(f"  ✓ {len(RULE_IDS)} 条规则全部有细则覆盖，总纲条数与代码一致")


def test_referenced_evidence_keys_exist():
    """★ 文档里 `evidence.xxx` 与细则表格里的 evidence 字段名必须真实存在。

    细则的核心价值就是「这条规则的 evidence 里有哪些字段可以直接拿来算」，
    字段名错了这份价值直接归零，而且模型会去读一个 None。
    """
    bad = []
    for doc, text in CORPUS.items():
        for key in re.findall(r"evidence\.([a-z_][a-z0-9_]*)", text):
            if key not in EVIDENCE_KEYS:
                bad.append(f"[{doc}] evidence.{key} 不存在，"
                           f"现有字段：{sorted(EVIDENCE_KEYS)}")
    assert not bad, "\n  " + "\n  ".join(bad)
    print(f"  ✓ evidence 字段引用全部命中（代码里共 {len(EVIDENCE_KEYS)} 个键）")


def test_referenced_constants_exist():
    """★ 文档里直接点名的常量（如 `API_P99_THRESHOLD_S`）必须还在代码里。"""
    bad = []
    for doc, text in CORPUS.items():
        for span in _INLINE_RE.findall(_prose(text)):
            m = _UPPER_SPAN_RE.match(span.strip())
            if not m:
                continue
            token = m.group(1)
            if token.lower() in _SQL_WORDS or token in CONSTANTS:
                continue
            bad.append(f"[{doc}] 引用了不存在的常量 {token}")
    assert not bad, "\n  " + "\n  ".join(bad)
    print("  ✓ 文档点名的阈值常量均在代码中存在")


# 阈值数值断言：**期望字符串由常量算出**，常量一改期望值随之改变，
# 文档没跟着改就会红。硬编码期望值做不到这一点（改了常量测试照样绿）。
_THRESHOLD_CLAIMS = [
    ("CAP-003 超卖阈值（总纲）", "risk_governance",
     lambda: f"{builtin.CPU_OVERSALE_THRESHOLD:.0f}%"),
    ("CAP-003 超卖阈值（细则）", "risk_governance/capacity_rules",
     lambda: f"{builtin.CPU_OVERSALE_THRESHOLD:.0f}%"),
    ("CAP-003 反算用的倍数", "risk_governance/capacity_rules",
     lambda: f"× {builtin.CPU_OVERSALE_THRESHOLD / 100:.1f}"),
    ("CAP-004 RDS 内存阈值", "risk_governance/capacity_rules",
     lambda: f"{builtin.RDS_MEM_THRESHOLD:.0f}%"),
    ("DB-001 连接阈值", "risk_governance/db_rules",
     lambda: f"{builtin.RDS_CONN_THRESHOLD:.0f}%"),
    ("DB-002 扫描行数阈值", "risk_governance/db_rules",
     lambda: f"{builtin.SLOW_ROWS_EXAMINED // 10000} 万"),
    ("API-001 P99 阈值", "risk_governance/api_rules",
     lambda: f"{builtin.API_P99_THRESHOLD_S:.1f}s"),
    ("API-001 错误率阈值", "risk_governance/api_rules",
     lambda: f"{builtin.API_ERR_THRESHOLD * 100:.0f}%"),
]


def test_threshold_numbers_match_constants():
    """★★ 细则里写的阈值数字必须与代码常量一致。"""
    bad = []
    for desc, doc, fmt in _THRESHOLD_CLAIMS:
        want = fmt()
        if want not in CORPUS[doc]:
            bad.append(f"[{doc}] {desc}：文档里找不到当前值 {want!r}，"
                       f"常量改了但文档没改？")
    assert not bad, "\n  " + "\n  ".join(bad)
    print(f"  ✓ {len(_THRESHOLD_CLAIMS)} 处阈值数字与代码常量一致")


# ══════════════════════════════════════════════════════
# 4. 兜底：所有标识符都要有出处
# ══════════════════════════════════════════════════════

def test_every_backticked_identifier_is_explainable():
    """★★ 兜底对账：正文里每个反引号标识符都必须能在某个权威来源里找到。

    前面几条测的是「已知类别」，这条测的是「有没有漏掉的类别」——
    比如某个 evidence 键被改名、某张表被删、某个工具被下线，
    对应 token 会同时从所有来源里消失，只能靠这条兜住。

    失败时的处理方式：先确认是文档写错还是代码改名；
    若这个 token 确实属于一类新概念（不由本项目代码约束），
    再把它登记进 _KNOWN_OTHER 并注明来源。
    """
    universes = {
        "工具": TOOLS, "工具参数": ALL_PARAM_NAMES, "枚举取值": ALL_ENUM_VALUES,
        "表": TABLES, "列": COLUMNS, "Skill": SKILL_NAMES,
        "细则": REFERENCE_NAMES, "evidence 字段": EVIDENCE_KEYS,
        "故障场景": FAULT_SCENARIOS,
        "世界动作": WORLD_ACTIONS,
        "已登记的其他": _KNOWN_OTHER,
    }
    known = set().union(*universes.values())
    unexplained = {}
    for doc, text in CORPUS.items():
        for span in _INLINE_RE.findall(_prose(text)):
            m = _IDENT_SPAN_RE.match(span.strip())
            if not m:
                continue
            token = m.group(1)
            if token in known or token.lower() in _SQL_WORDS:
                continue
            unexplained.setdefault(token, []).append(doc)
    assert not unexplained, (
        "以下标识符在代码里找不到出处：\n  "
        + "\n  ".join(f"{k}  （出现在 {sorted(set(v))}）"
                      for k, v in sorted(unexplained.items()))
        + "\n  若是文档写错请改文档；若属新概念请登记进 _KNOWN_OTHER 并注明来源。")
    print(f"  ✓ 全部反引号标识符可溯源（{len(universes)} 类来源，"
          f"其中仅 {len(_KNOWN_OTHER)} 个为登记豁免）")


def test_fault_scenarios_are_extracted():
    """★ 故障场景 id 这个权威来源必须真的解析出来了。

    若 AST 提取悄悄失败（返回空集），兜底对账会把引用了场景名的文档
    报成"找不到出处"，或者反过来 —— 空集会让这一类完全失去校验。
    """
    assert len(FAULT_SCENARIOS) >= 4, FAULT_SCENARIOS
    assert "slow_query_storm" in FAULT_SCENARIOS, FAULT_SCENARIOS
    print(f"  ✓ 从 mock_server/faults.py 解析出 {len(FAULT_SCENARIOS)} 个故障场景："
          f"{sorted(FAULT_SCENARIOS)}")


def test_tool_description_columns_are_real():
    """★ 工具描述里内联的列清单也是文档 —— sql_query 的描述就列了 9 张表的列。

    它跟 Skill 一样会被模型当成事实，同样会漂移，所以一起对账。
    """
    bad = []
    for name in sorted(TOOLS):
        desc = registry.get_spec(name).schema["function"]["description"]
        for table, cols in re.findall(r"\b([a-z][a-z0-9_]*)\(([a-z0-9_,\s]+)\)", desc):
            t = db.metadata.tables.get(table)
            if t is None:
                continue
            real = {c.name for c in t.columns}
            wrong = [c.strip() for c in cols.split(",")
                     if c.strip() and c.strip() not in real]
            if wrong:
                bad.append(f"{name} 的描述里 {table}({','.join(wrong)}) 列不存在，"
                           f"真实列：{sorted(real)}")
    assert not bad, "\n  " + "\n  ".join(bad)
    print("  ✓ 工具描述里内联的表列清单与表定义一致")


# ══════════════════════════════════════════════════════
# 5. Agent 资产不能有死角
# ══════════════════════════════════════════════════════

def test_dispatchable_types_come_from_defs():
    """★★ dispatch_agent 的枚举必须由定义文件生成，不能手写。

    这条是被真实缺陷催出来的：新增 capacity / dbops 两个专家 Agent 后，
    定义、Skill、测试全就位，但 agent_tools 里硬编码的
    SUBAGENT_TYPES = ["topology","risk","diagnose"] 没同步 ——
    两个 Agent 存在却永远派不出去，属于静默失效。
    """
    from app.tools import agent_tools
    want = agents_base.dispatchable_keys()
    assert agent_tools.SUBAGENT_TYPES == want, \
        f"模块常量 {agent_tools.SUBAGENT_TYPES} != 定义文件 {want}"
    enum = (registry.get_spec("dispatch_agent")
            .schema["function"]["parameters"]["properties"]["subagent_type"]["enum"])
    assert list(enum) == want, f"工具 schema 的 enum {enum} != 定义文件 {want}"
    desc = registry.get_spec("dispatch_agent").schema["function"]["description"]
    for key in want:
        assert f"- {key}:" in desc, f"工具描述里没有 {key} 的说明"
    print(f"  ✓ 可派发子 Agent {want} 三处（常量/enum/描述）同源")


def test_no_unreachable_agent():
    """★ 每个 Agent 定义都要有到达路径：顶层编排入口、意图路由、可派发、或兜底 general。

    否则就是写了却永远不会被用到的死资产。
    orchestrator 不在意图表也不可派发，它的到达路径是 AGENT_ROUTING=model 时的顶层入口，
    所以从 config 取 —— 不能用字面量放行，否则改名后这条就形同失效。
    """
    reachable = (set(scheduler.INTENT_AGENT.values())
                 | set(agents_base.dispatchable_keys())
                 | {"general", _cfg.ORCHESTRATOR_AGENT})
    dead = sorted(set(agents_base.AGENT_SPECS) - reachable)
    assert not dead, f"无法到达的 Agent 定义：{dead}"
    assert _cfg.ORCHESTRATOR_AGENT in agents_base.AGENT_SPECS, \
        f"编排 Agent {_cfg.ORCHESTRATOR_AGENT!r} 没有对应的定义文件，" \
        f"model 路由下 build_agent 会直接 KeyError"
    print(f"  ✓ {len(agents_base.AGENT_SPECS)} 个 Agent 均有到达路径"
          f"（编排入口 {_cfg.ORCHESTRATOR_AGENT} / 意图路由 "
          f"{len(set(scheduler.INTENT_AGENT.values()))} / "
          f"可派发 {len(agents_base.dispatchable_keys())}）")


def test_orchestrator_can_do_governance_and_dispatch():
    """★ 编排 Agent 必须同时具备治理与派发两件事。

    子 Agent 在代码层被过滤成只读，所以治理动作只能由顶层 Agent 发起；
    若编排 Agent 没有治理工具，“确认后执行修复”这条链路会直接断掉。
    """
    tools = set(agents_base.effective_tools(_cfg.ORCHESTRATOR_AGENT))
    assert "dispatch_agent" in tools, "编排 Agent 拿不到 dispatch_agent，无法派子 Agent"
    destructive = {t for t in tools if registry.get_spec(t).is_destructive}
    assert destructive, "编排 Agent 没有任何治理工具，治理链路会断"
    # 反向：它不应被当成子 Agent 派发（否则等于给子 Agent 开了治理与繁殖的口子）
    assert _cfg.ORCHESTRATOR_AGENT not in agents_base.dispatchable_keys(), \
        "编排 Agent 不应可派发"
    print(f"  ✓ 编排 Agent 持有 {len(tools)} 个工具（含 {len(destructive)} 个治理类）且不可被派发")


def test_intent_agent_map_points_to_real_agents():
    """★ 意图路由表的取值必须是真实 Agent key，否则 build_agent 直接 KeyError。"""
    bad = [f"{k} → {v}" for k, v in scheduler.INTENT_AGENT.items()
           if v not in agents_base.AGENT_SPECS]
    assert not bad, f"意图路由指向不存在的 Agent：{bad}"
    # 反向：意图表的键必须都是合法意图
    from app.harness import intent as intent_mod
    unknown = [k for k in scheduler.INTENT_AGENT if k not in intent_mod.INTENTS]
    assert not unknown, f"意图路由表里有非法意图名：{unknown}"
    print(f"  ✓ {len(scheduler.INTENT_AGENT)} 条意图路由的两端都合法")


def test_describe_tools_equal_runtime_tools():
    """★ /api/status 暴露的工具集必须等于 build_agent 实际给出的那一份。

    前端资产面板会显示「某 Agent 有 N 个工具、其中 M 个破坏性」。
    若 describe 用 md 里的声明而 build_agent 另有增删（live 摘 ingest_data、
    统一补 load_skill），面板就会展示模型手里根本没有的工具 —— 排查时会被带偏。
    """
    bad = []
    for item in agents_base.describe():
        runtime = sorted(agents_base.build_agent(item["key"])["tools"])
        if item["tools"] != runtime:
            bad.append(f"{item['key']}: describe {item['tools']} != 运行时 {runtime}")
    assert not bad, "\n  " + "\n  ".join(bad)
    # 顺带确认两处差异确实生效（否则上面的相等可能只是巧合）
    tools = agents_base.build_agent("data")["tools"]
    assert "load_skill" in tools, "load_skill 未自动补入"
    if _cfg.is_live():
        assert "ingest_data" not in tools, "live 模式下 ingest_data 应被摘除"
    print(f"  ✓ {len(agents_base.AGENT_SPECS)} 个 Agent 的对外工具集与运行时一致"
          f"（当前 DATA_SOURCE={_cfg.DATA_SOURCE}）")


def test_every_skill_is_reachable():
    """★ 每篇 Skill 都得有人用：要么是某 Agent 的主 Skill，要么在 L1 目录里可被按需加载。

    L1 目录是全量的，所以这条实际校验的是「目录没漏篇」——
    漏了的 Skill 模型根本不知道它存在。
    """
    catalog = skills.catalog_prompt()
    main = {v["skill"] for v in agents_base.AGENT_SPECS.values() if v["skill"]}
    missing = [n for n in sorted(SKILL_NAMES) if n not in catalog and n not in main]
    assert not missing, f"既不是主 Skill 也不在目录里的 Skill：{missing}"
    print(f"  ✓ {len(SKILL_NAMES)} 篇 Skill 均可达（主 Skill {len(main)} 篇 / 其余在目录中）")


def main():
    # build_agent 会注入长期记忆（要读 agent_memory 表），临时库需要先建表
    db.init_db()
    groups = [
        ("工具引用", [
            test_skill_allowed_tools_exist,
            test_documented_tool_params_exist,
            test_documented_enum_values_valid,
            test_documented_load_skill_targets_exist,
            test_tool_description_columns_are_real,
        ]),
        ("表名与列名", [
            test_sql_examples_use_queryable_tables,
            test_sql_examples_columns_exist,
            test_negative_sql_example_is_really_wrong,
            test_ts_unit_claims_match_code,
            test_queryable_table_list_in_doc_matches_code,
            test_ingest_table_list_matches_pipeline,
        ]),
        ("规则 / evidence / 阈值", [
            test_referenced_rule_ids_exist,
            test_all_rules_are_documented,
            test_referenced_evidence_keys_exist,
            test_referenced_constants_exist,
            test_threshold_numbers_match_constants,
        ]),
        ("兜底对账", [
            test_every_backticked_identifier_is_explainable,
            test_fault_scenarios_are_extracted,
        ]),
        ("Agent 资产无死角", [
            test_dispatchable_types_come_from_defs,
            test_no_unreachable_agent,
            test_intent_agent_map_points_to_real_agents,
            test_describe_tools_equal_runtime_tools,
            test_every_skill_is_reachable,
        ]),
    ]
    print(f"语料：{len(CORPUS)} 份文档（{len(SKILL_NAMES)} 篇 Skill + "
          f"{len(REFERENCE_NAMES)} 篇细则 + {len(agents_base.AGENT_SPECS)} 个 Agent 定义）")
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
    print("\n" + "=" * 46)
    print(f"通过 {passed} / 失败 {failed}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
