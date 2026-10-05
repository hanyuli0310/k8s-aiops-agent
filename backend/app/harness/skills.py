"""Skill 渐进式披露（P2-1）。

## 为什么需要

原实现 `_load_skill()` 把整篇 md 拼进系统提示词。3 篇时还能接受，
扩到 8 篇后常驻成本会超过 5000 token，且大部分轮次根本用不到那些内容。

## 三层结构（对齐 Claude Code 的实现，见 CC/src/skills/loadSkillsDir.ts）

    L1  frontmatter（name / description / when_to_use）→ 常驻系统提示词，每篇约 50 token
    L2  SKILL.md 正文                                  → load_skill(name) 按需取
    L3  references/*.md 深度内容                        → load_skill(name, reference=...) 按需取

CC 的 `estimateSkillTokens` 只统计 name+description+whenToUse，印证了 L1 的边界。

## 一个刻意的偏离

Agent 的【主 Skill】仍然注入正文，而不是只给 L1。理由：
RiskAgent 的 risk_governance 是它每一轮都要用的核心方法论，若要求它先调一次
load_skill 才能拿到，等于给每个治理请求多加一轮 LLM 往返。
把细节下沉到 references 之后主体已经很薄，这个折中的代价很小。
其余 Skill 一律只给 L1。

## 目录布局（两种都支持）

    app/skills/xxx.md                    单文件（旧布局，仍可用）
    app/skills/xxx/SKILL.md              目录布局
    app/skills/xxx/references/yyy.md     深度内容
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@dataclass
class SkillMeta:
    name: str
    description: str = ""
    when_to_use: str = ""
    body_path: Path = None
    refs_dir: Path = None
    references: list = field(default_factory=list)
    allowed_tools: list = field(default_factory=list)

    def catalog_line(self) -> str:
        """L1：进系统提示词的一行摘要。"""
        parts = [f"- **{self.name}**：{self.description}"]
        if self.when_to_use:
            parts.append(f"（何时用：{self.when_to_use}）")
        if self.references:
            parts.append(f"[含 {len(self.references)} 篇细则：{', '.join(self.references)}]")
        return " ".join(parts)


_CACHE: dict = None


def _parse_frontmatter(text: str) -> tuple:
    """拆出 (frontmatter dict, 正文)。无 frontmatter 时返回 ({}, 全文)。

    解析失败一律降级为"无 frontmatter"而不是抛异常 —— 一篇 Skill 写错格式
    不该让整个 Agent 起不来。
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw, body = text[3:end], text[end + 4:]
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        logger.warning("Skill frontmatter 解析失败，按无 frontmatter 处理: %s", e)
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, body.lstrip("\n")


def _split_list(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).replace("，", ",").split(",") if x.strip()]


def discover(force: bool = False) -> dict:
    """扫描 skills 目录，返回 {name: SkillMeta}。结果缓存（进程内）。"""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE

    found = {}
    if not SKILLS_DIR.is_dir():
        _CACHE = found
        return found

    for entry in sorted(SKILLS_DIR.iterdir()):
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_file() and entry.suffix == ".md":
            name, body_path, refs_dir = entry.stem, entry, None
        elif entry.is_dir() and (entry / "SKILL.md").is_file():
            name, body_path, refs_dir = entry.name, entry / "SKILL.md", entry / "references"
        else:
            continue

        try:
            meta, _ = _parse_frontmatter(body_path.read_text(encoding="utf-8"))
        except OSError as e:
            logger.warning("读取 Skill 失败 %s: %s", body_path, e)
            continue

        refs = []
        if refs_dir and refs_dir.is_dir():
            refs = sorted(p.stem for p in refs_dir.glob("*.md"))

        found[name] = SkillMeta(
            name=str(meta.get("name") or name),
            description=str(meta.get("description") or "").strip(),
            when_to_use=str(meta.get("when_to_use") or meta.get("whenToUse") or "").strip(),
            body_path=body_path,
            refs_dir=refs_dir,
            references=refs,
            allowed_tools=_split_list(meta.get("allowed-tools") or meta.get("allowed_tools")),
        )
    _CACHE = found
    logger.info("发现 %d 个 Skill: %s", len(found), sorted(found))
    return found


def catalog_prompt(exclude: str = None) -> str:
    """L1：全部 Skill 的目录摘要，注入系统提示词。

    exclude 用于跳过已全文注入的主 Skill，避免同一篇出现两次。
    """
    skills = discover()
    lines = [m.catalog_line() for k, m in sorted(skills.items()) if k != exclude]
    if not lines:
        return ""
    return ("\n\n[可按需加载的 Skill 目录]\n"
            "以下方法论文档未展开，判断需要时用 load_skill(name) 取正文，"
            "用 load_skill(name, reference=...) 取细则：\n" + "\n".join(lines))


def body_prompt(name: str) -> str:
    """主 Skill 的正文片段（含 references 索引），直接拼进系统提示词。"""
    body = load_body(name)
    if not body:
        return ""
    m = discover().get(name)
    tail = ""
    if m and m.references:
        tail = ("\n\n[本 Skill 的细则文档，需要时用 load_skill(\"%s\", reference=\"...\") 取]\n%s"
                % (name, "\n".join(f"- {r}" for r in m.references)))
    return f"\n\n[已加载 Skill「{name}」，严格按其方法执行]\n{body}{tail}"


def load_body(name: str) -> str:
    """L2：Skill 正文（去掉 frontmatter）。"""
    m = discover().get(name)
    if m is None:
        return ""
    try:
        _, body = _parse_frontmatter(m.body_path.read_text(encoding="utf-8"))
        return body.strip()
    except OSError as e:
        logger.warning("读取 Skill 正文失败 %s: %s", name, e)
        return ""


def load_reference(name: str, reference: str) -> str:
    """L3：references/<reference>.md。越界一律拒绝（reference 来自模型输出）。"""
    m = discover().get(name)
    if m is None or not m.refs_dir:
        return ""
    # 只接受已登记的文件名，天然挡掉 ../ 之类的路径穿越
    stem = reference[:-3] if reference.endswith(".md") else reference
    if stem not in m.references:
        return ""
    try:
        _, body = _parse_frontmatter((m.refs_dir / f"{stem}.md").read_text(encoding="utf-8"))
        return body.strip()
    except OSError as e:
        logger.warning("读取 Skill 细则失败 %s/%s: %s", name, stem, e)
        return ""


def describe() -> list:
    """供 /api/status 暴露，便于前端与排查。

    带上各层的字符数：前端资产面板据此显示「常驻多少 / 按需多少」，
    这是判断渐进式披露有没有起作用的唯一硬指标。
    """
    out = []
    for k, m in sorted(discover().items()):
        refs_chars = sum(len(load_reference(k, r)) for r in m.references)
        out.append({
            "name": k, "display_name": m.name,
            "description": m.description, "when_to_use": m.when_to_use,
            "allowed_tools": m.allowed_tools,
            "references": m.references, "layout": "dir" if m.refs_dir else "file",
            "catalog_chars": len(m.catalog_line()),
            "body_chars": len(load_body(k)),
            "refs_chars": refs_chars,
        })
    return out


def stats() -> dict:
    """L1 目录 vs 全部内容的体量对比（渐进式披露的收益量化）。"""
    items = describe()
    total = sum(i["body_chars"] + i["refs_chars"] for i in items)
    catalog = len(catalog_prompt())
    return {
        "count": len(items),
        "reference_count": sum(len(i["references"]) for i in items),
        "catalog_chars": catalog,
        "total_chars": total,
        # 常驻占比，向上取整到整数百分比；total 为 0 时不做除法
        "catalog_pct": round(catalog * 100 / total, 1) if total else 0,
    }
