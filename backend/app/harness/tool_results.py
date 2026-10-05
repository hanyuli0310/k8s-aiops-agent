"""超长工具结果落盘 + 结构感知预览（P0-4）。

对应 Claude Code 的 toolResultStorage 思路：不做无脑截断，而是给模型
「保留结构与总量的预览 + 一条可取回的路径」。

为什么不能硬截断：运维场景 query_logs 一次几万字符，砍到 2000 只剩十几条日志，
剩下的永久丢失。提示词让模型"用 sql_query 精查"，但模型得先知道该查什么 ——
而它连数据概貌和字段结构都没看到。

落盘目录是【缓存】语义：可随时删除、可淘汰，不承载任何状态判定
（与 memory.py 里 governance 记录的"状态标记"语义相反，那种绝不能淘汰）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

STORE = config.BACKEND_DIR / ".tool_results"
SAMPLE_N = 3                       # 预览里每个数组字段保留的样本条数
MAX_AGE_S = 24 * 3600              # 超过此时长的落盘文件会被清理
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(part: str) -> str:
    """文件名消毒：session_id / tool_call_id 都来自外部，不能直接拼进路径。"""
    return _SAFE_NAME.sub("_", str(part))[:80] or "_"


def persist_and_preview(session_id: str, tool_call_id: str, tool_name: str,
                        result: str, limit: int) -> str:
    """超限结果落盘，返回结构感知预览 + 取回说明；未超限原样返回。"""
    if len(result) <= limit:
        return result

    path = None
    try:
        d = STORE / _safe(session_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_safe(tool_name)}-{_safe(tool_call_id)}.json"
        # 幂等：同一个 tool_call_id 的结果内容是确定的，重试时不重复写
        if not path.exists():
            path.write_text(result, encoding="utf-8")
    except OSError as e:
        # 落盘失败不能让整轮挂掉 —— 退回截断，但如实告知内容已丢失
        logger.warning("工具结果落盘失败 %s: %s", path, e)
        return (result[:limit]
                + f"\n...[结果超长已截断，原始 {len(result)} 字符，落盘失败故余下内容丢失。"
                  f"请改用 sql_query 精确查询]")

    return f"{_structural_preview(result, limit)}\n\n{_retrieval_note(path, len(result))}"


def _structural_preview(result: str, limit: int) -> str:
    """结构感知预览：数组字段只留前 N 条 + 总数，字段结构完整保留。

    比截断前 N 个字符有用一个数量级 —— 模型看到
    {"logs": {"_total": 487, "_sample": [{完整字段}]}}
    就知道有 487 条、每条什么结构，能直接写出精确的 sql_query。
    """
    try:
        obj = json.loads(result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return result[:limit]                      # 非 JSON（纯文本）只能截断

    wrapped = {"_root_list": obj} if isinstance(obj, list) else obj
    if not isinstance(wrapped, dict):
        return result[:limit]

    # 样本条数逐级下调，保证预览本身是【合法 JSON】而不是被切坏的半段
    for n in (SAMPLE_N, 2, 1, 0):
        summary = {}
        for k, v in wrapped.items():
            if isinstance(v, list):
                summary[k] = {"_total": len(v), "_sample": v[:n]} if n else {"_total": len(v)}
            else:
                summary[k] = v
        text = json.dumps(summary, ensure_ascii=False, default=str)
        if len(text) <= limit:
            return text
    return text[:limit]                            # 连 n=0 都装不下才硬切


def _retrieval_note(path: Path, size: int) -> str:
    return (f"<persisted-output>\n"
            f"完整结果 {size} 字符，已保存到：{path}\n"
            f"上方为各字段前 {SAMPLE_N} 条样本 + 总数，字段结构已完整呈现。\n"
            f"需要全量分析时：优先用 sql_query 精确查询（数据都在库里，可聚合过滤）；\n"
            f"确需原始内容时用 read_tool_result(path=\"{path}\", offset=0, limit=200)。\n"
            f"</persisted-output>")


def resolve_in_store(path: str) -> Path:
    """把外部传入的路径限制在 STORE 内，越界抛 ValueError（防路径逃逸）。

    用 relative_to 而非 startswith：macOS 上 /var 是 /private/var 的符号链接，
    字符串前缀比较会误判。
    """
    p = Path(path).resolve()
    p.relative_to(STORE.resolve())                 # 越界时抛 ValueError
    return p


def prune_old(max_age_s: int = MAX_AGE_S) -> int:
    """清理过期落盘文件，防止磁盘无限增长。返回删除数量。

    这里可以放心删：落盘内容是缓存，原始数据都在数据库里。
    """
    if not STORE.exists():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for f in STORE.rglob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:                            # noqa: PERF203
            logger.debug("清理落盘文件失败: %s", f, exc_info=True)
    for d in STORE.iterdir() if STORE.exists() else []:
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    if removed:
        logger.info("清理过期工具结果落盘文件 %d 个", removed)
    return removed


def cleanup(session_id: str) -> int:
    """清理某会话的全部落盘文件。返回删除数量。"""
    d = STORE / _safe(session_id)
    if not d.exists():
        return 0
    removed = 0
    for f in d.iterdir():
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass
    return removed
