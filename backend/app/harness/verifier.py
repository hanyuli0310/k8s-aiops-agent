"""结论事实核对（E-4 verifier）：回答里的标识符与数值必须能在模型见过的内容里找到出处。

## 为什么不做通用反思 / LLM-as-judge

系统提示词第 1 条写着"一切结论必须来自工具返回的真实数据，禁止编造数值、资源名、traceID"。
那是**约束**，不是**校验** —— 模型违反了没人发现。

但本项目的结论恰好是**可机械核对**的：一个 trace_id 要么出现在工具结果里，要么没有。
让另一个模型来判断"这个结论靠不靠谱"既贵又不确定，字面比对既便宜又确定。
所以这里做的是窄而硬的事实核对，不是通用反思环。

## 证据池的定义（整个设计的关键）

    evidence pool = 模型在本轮里【实际看到过】的全部文本
                  = system prompt（含集群概况、Skill 全文）+ 用户输入 + 全部工具结果

这个定义让逻辑自洽：**模型没看到的东西，它说出来就是编的**。
工具结果被截断/落盘时模型同样看不到全文，所以池里没有也合理 —— 不构成误报。

池按 token 增量提取（只存标识符集合与数值集合，不留原文），因此：
  · 内存开销极小；
  · 续跑重置上下文后**池不会丢**（`reset_for_continuation` 不动它）。

## 三类核对，强度不同（刻意区别对待）

| 类 | 对象 | 判据 | 处置 |
|---|---|---|---|
| A1 | 长 hex 标识符（trace_id / span_id） | 不在池中 | **报警**：模型没有任何理由说出一个没见过的 hex 串 |
| A2 | 连字符资源名（order-service / rds-mysql-01） | 不在池中，**且与池中某项编辑距离 ≤ 2** | **报警**：这是最危险的一类 —— 看着像真的，其实编号错了 |
| B | 数值 | 归一化后不在池中 | **只统计不报警**（见下） |
| C | 证据的**时效**（哪些工具结果的数据已陈旧） | 某份结果的最新数据时间远于当下 | **提示不自纠**（见下） |

A2 为什么要加"编辑距离近"这个条件：`read-only`、`fail-closed` 这类英文词组也符合
连字符形态，但它们与任何真实资源名都相距很远；而 `rds-mysql-03`（世界里只有 01/02）
与 `rds-mysql-01` 距离为 1。**只报"近似但不相等"的，误报率远低于"不在池里就报"。**

B 类为什么不报警：模型对数值做单位换算（39300m → 39.3 核）、聚合（116 万）、
四则运算（1162951 / 7 ≈ 166135）都是**应该鼓励**的行为，字面上却对不上池子。
归一化能覆盖一部分（见 `_normalize_forms`），但覆盖不全，硬报警必然制造噪声。
所以 B 类只作为附带信息列出，供人判断，不参与报警决策。

## C 类：时间维度（为什么前三类不够）

A/B 类共同的盲区是：**证据池是个无时间概念的集合**。一条两小时前的日志进了池，
与一条一分钟前的日志没有任何区别。量化评测里真实发生过一次最严重的错误就落在这里：
`query_logs` 当时无时间窗口且升序返回，拿回的全是库里**最旧**的日志（两小时前
另一次演练的记录），Agent 据此报出"三源叠加"的根因 —— 结论完全错，
而**它引用的每一条日志都真实存在**，事实核对一声不响。

C 类把时间戳**按工具结果逐份记录**，而不是汇进一个全局集合：
排障时往往新旧数据混用（指标是新的、日志是旧的），只看全局最大时间会被新数据掩盖，
逐份看才能指名道姓地说出"query_logs 返回的数据最新只到 2 小时前"。

**为什么 C 类不触发自纠正**：数据陈旧不是模型措辞的问题，是取数窗口的问题。
让模型重写一遍回答既多花一次 LLM 往返，也解决不了根本问题（它手里就只有旧数据）。
所以 C 类如实呈现给用户与日志，不进入自纠正回路。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 长 hex 标识符：trace_id 32 位、span_id 16 位。
# 要求至少含一个 a-f 字母 —— 否则会把 16 位以上的纯数字串（罕见但存在）当成标识符。
_HEX_ID_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{16,64}\b")

# 连字符资源名：order-service / rds-mysql-01 / cn-hangzhou-h / order-service-7d9f8b6c5-x2k4p
_RES_RE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){1,5}\b")

# 数值：支持千分位与小数。
# 前置断言拒绝"数字紧跟在单词字符后面"（挡掉 order-01 之外的 a1b2 / x2k4p 这类 pod 名后缀）。
# 后置只拒绝紧跟数字或点 —— **不能拒绝字母**：本项目的数值几乎都带单位后缀
# （1.244s / 336.5ms / 39300m / 2048Mi），拒绝字母会让它们整体提取不到
# （第一版就是这样，E-3 的用例才把它暴露出来）。
_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\d.])")

# 纯整数的忽略门槛：序号、条数、副本数、规则编号里的数字没有信息量。
# 注意**只对整数生效** —— 见 _is_informative。
_NUM_IGNORE_BELOW = 11


def _is_informative(v: float) -> bool:
    """这个数值值不值得核对 / 保真。

    有小数部分的一律算：本项目最关键的度量值恰恰是小数
    （P99 1.244s、错误率 1.78%、query_time 2.369s、request_time 0.333）。
    第一版用绝对值 11 一刀切，把这些全滤掉了 —— 等于漏掉了最该核对的一类。
    """
    if v != int(v):
        return True
    return abs(v) >= _NUM_IGNORE_BELOW

# 这些连字符词是通用英文/技术术语，不是本项目的资源名。
# 它们与真实资源名的编辑距离通常很远（A2 的距离条件已能挡住绝大多数），
# 这份清单只是为高频词省掉一次距离计算，不承担正确性责任。
_GENERIC_HYPHENATED = frozenset({
    "read-only", "fail-closed", "fail-open", "in-progress", "up-to-date",
    "well-known", "self-healing", "end-to-end", "trade-off", "real-time",
    "content-type", "user-agent", "x-request-id",
})

# --- C 类：数据时效 ---

# 数据时间的两种形态（本项目工具返回里实际出现的）：
#   1. ISO 风格字符串：2026-08-11 21:30:15 / 2026-08-11T21:30:15
#   2. epoch：10 位（秒）与 13 位（毫秒）纯数字
# 为何不抽取更多格式：形式每多一种，误判风险就多一分，
# 而判定一旦开始误报，C 类就会被当成噪声忽略 —— 宁可少报。
_ISO_TS_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
_EPOCH_RE = re.compile(r"(?<![\w.])(1[0-9]{9}|1[0-9]{12})(?![\d.])")

# 判定一份工具结果“陈旧”的阈值。
# 取 1800 秒的依据：提示词声明 SLS 日志/Trace 只保留 30 分钟，
# 且 live 模式下采集器 10~60s 一轮 —— 正常取数拿到的最新数据应在分钟级以内。
# 最新数据都已超过一个保留窗，基本只有两种可能：查询排序/窗口写错，
# 或采集已经停了 —— 两者都必须让人知道。
STALE_DATA_SECONDS = 1800

# 时间戳少于这个数量的结果不参与时效判定：单个时间戳很可能是
# “上次治理动作发生在 XX”这类元信息，而不是观测数据的时间轴。
_MIN_TS_FOR_STALENESS = 2


def extract_data_times(text: str) -> set:
    """从文本里提取数据时间（epoch 秒）。

    只保留“看上去像真实观测时间”的值：与当下相差不超过 30 天。
    这道过滤很关键 —— Skill 文档里写着旧数据集的示例时间戳
    （如 1785754200000），它们不是本次查到的数据，不能拿去判时效。
    """
    if not text:
        return set()
    now = time.time()
    horizon = 30 * 86400
    out = set()
    for m in _ISO_TS_RE.finditer(text):
        y, mo, d, hh, mm, ss = m.groups()
        try:
            ts = time.mktime((int(y), int(mo), int(d), int(hh), int(mm),
                              int(ss or 0), 0, 0, -1))
        except (ValueError, OverflowError):
            continue
        if abs(now - ts) <= horizon:
            out.add(ts)
    for raw in _EPOCH_RE.findall(text):
        v = float(raw)
        ts = v / 1000 if len(raw) == 13 else v
        if abs(now - ts) <= horizon:
            out.add(ts)
    return out

@dataclass
class Evidence:
    """模型见过的内容的指纹。增量累积，不保留原文。"""

    ids: set = field(default_factory=set)        # 长 hex 标识符
    resources: set = field(default_factory=set)  # 连字符资源名
    numbers: set = field(default_factory=set)    # 归一化后的数值（float）
    # C 类：逐份工具结果的数据时间跨度 [(工具名, 最旧 ts, 最新 ts, 样本数)]。
    # 用 list 而不是汇总成一个区间：排障常新旧数据混用，必须能指名是哪一份陈旧。
    sources: list = field(default_factory=list)

    def absorb(self, text: str):
        if not text:
            return
        low = text.lower()
        self.ids.update(_HEX_ID_RE.findall(low))
        self.resources.update(_RES_RE.findall(low))
        for raw in _NUM_RE.findall(text):
            v = _to_float(raw)
            if v is not None:
                self.numbers.add(v)

    def absorb_tool_result(self, name: str, text: str):
        """吸收工具结果：除了常规指纹，额外记下这份数据的时间跨度。

        时间戳**只从工具结果**里取，不从 system prompt / 用户输入里取。
        因为提示词与 Skill 文档里包含旧数据集的示例时间戳，
        把它们当成“本次查到的数据”会让时效判定彻底失真。
        """
        self.absorb(text)
        times = extract_data_times(text)
        if len(times) >= _MIN_TS_FOR_STALENESS:
            self.sources.append((name, min(times), max(times), len(times)))

    def stale_sources(self, *, threshold: int = None) -> list:
        """数据已陈旧的工具结果：[(工具名, 最新数据距now 多少秒)]。"""
        thr = STALE_DATA_SECONDS if threshold is None else threshold
        now = time.time()
        out = []
        for name, _oldest, newest, _n in self.sources:
            age = now - newest
            if age > thr:
                out.append((name, int(age)))
        return out

    def stats(self) -> dict:
        return {"ids": len(self.ids), "resources": len(self.resources),
                "numbers": len(self.numbers), "timed_sources": len(self.sources)}


def _to_float(raw: str):
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def extract_numbers(text: str, *, min_abs: float = None) -> set:
    """提取文本里的数值。公开给上下文压缩做摘要保真校验复用（E-3）。

    min_abs 默认用与核对同一条口径（忽略小整数）—— 序号、条数这类数字
    在摘要里丢了无所谓，真正不能丢的是水位、行数、耗时这类度量值。
    """
    out = set()
    for raw in _NUM_RE.findall(text or ""):
        v = _to_float(raw)
        if v is None:
            continue
        if min_abs is None:
            if _is_informative(v):
                out.add(v)
        elif abs(v) >= min_abs:
            out.add(v)
    return out


def _normalize_forms(v: float) -> list:
    """一个数值在证据里可能以哪些形态出现。

    覆盖本项目真实存在的换算关系：
      毫核 ↔ 核（39300m / 39.3）、毫秒 ↔ 秒（333 / 0.333）、
      万（116 万 / 1162951）、百分比（1.78% / 0.0178）。
    覆盖不全是已知的，所以 B 类只统计不报警。
    """
    forms = {v, v * 1000, v / 1000, v * 100, v / 100, v * 10000, v / 10000}
    return [f for f in forms if f == f]          # 过滤 NaN


def _num_matched(v: float, pool: set) -> bool:
    """数值是否能在池里找到出处（含单位换算、四舍五入与量级约等）。"""
    for form in _normalize_forms(v):
        for p in (form, round(form), round(form, 1), round(form, 2), round(form, 3)):
            if p in pool:
                return True
    # 反向：池里的数被四舍五入后等于候选值（1.244 → 1.24 → 1.2）
    for p in pool:
        for nd in (0, 1, 2, 3):
            try:
                if round(p, nd) == v or round(p / 1000, nd) == v or round(p * 1000, nd) == v:
                    return True
            except (OverflowError, ValueError):
                continue
    return _approx_matched(v, pool)


# 中文里"约 116 万"这类量级化表述的容差。1% 足以覆盖
# 1162951 → "116 万"（误差 0.25%），又不至于让任意两个数互相匹配。
_APPROX_TOLERANCE = 0.01
_MAGNITUDES = (10_000, 100_000_000)          # 万、亿


def _approx_matched(v: float, pool: set) -> bool:
    """量级约等匹配：模型说"116 万"，池里是 1162951。

    只对**万/亿量级**启用（中文语境最常见的概括方式），且要求相对误差 < 1%。
    刻意不做通用近似匹配 —— 容差一放开，任何数都能"匹配"上，
    B 类的命中率就变成一个没有意义的数字。
    """
    if v <= 0:
        return False
    for mag in _MAGNITUDES:
        target = v * mag
        for p in pool:
            if p > 0 and abs(p - target) / p < _APPROX_TOLERANCE:
                return True
    return False


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein 距离，超过 cap 就提前返回 cap+1（只关心"是否很近"）。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def check(answer: str, ev: Evidence, *, max_distance: int = 2) -> dict:
    """核对一段回答。返回结构见下，`suspicious` 为 True 才值得打扰用户。

    {
      "suspicious": bool,          # A 类是否命中（只有 A 类进自纠正）
      "fake_ids": [...],           # A1：没见过的 hex 标识符
      "near_miss_resources": [(说出的, 池里最近的, 距离), ...],   # A2
      "unverified_numbers": [...], # B：对不上的数值（仅供参考，不触发报警）
      "stale_sources": [(工具名, 数据陈旧秒数), ...],            # C
      "time_risk": bool,           # C 类命中且回答声称的是"当前"状态
      "checked": {...},            # 各类核对了多少个，便于判断信号密度
    }
    """
    low = (answer or "").lower()

    fake_ids = sorted({i for i in _HEX_ID_RE.findall(low) if i not in ev.ids})

    near_miss = []
    seen_res = {r for r in _RES_RE.findall(low)}
    for r in sorted(seen_res):
        if r in ev.resources or r in _GENERIC_HYPHENATED:
            continue
        best, best_d = None, max_distance + 1
        for known in ev.resources:
            d = _edit_distance(r, known, cap=max_distance)
            if d < best_d:
                best, best_d = known, d
        if best is not None and best_d <= max_distance:
            near_miss.append((r, best, best_d))

    unverified_nums = []
    checked_nums = 0
    for raw in _NUM_RE.findall(answer or ""):
        v = _to_float(raw)
        if v is None or not _is_informative(v):
            continue
        checked_nums += 1
        if not _num_matched(v, ev.numbers):
            unverified_nums.append(raw)

    stale = ev.stale_sources()
    # 回答把陈旧数据讲成了"当下正在发生"，才是真正危险的情形：
    # 若回答本身就在做历史回顾（"两小时前那次演练"），旧数据是应该的。
    _PRESENT_TENSE = ("当前", "目前", "现在", "正在", "此刻", "实时")
    time_risk = bool(stale) and any(w in (answer or "") for w in _PRESENT_TENSE)

    return {
        "suspicious": bool(fake_ids or near_miss),
        "fake_ids": fake_ids,
        "near_miss_resources": near_miss,
        "unverified_numbers": sorted(set(unverified_nums))[:20],
        "stale_sources": stale,
        "time_risk": time_risk,
        "checked": {"ids": len(set(_HEX_ID_RE.findall(low))),
                    "resources": len(seen_res), "numbers": checked_nums,
                    "timed_sources": len(ev.sources)},
    }


def describe(result: dict) -> str:
    """给用户看的一句话说明（前端在回答上方显示）。"""
    parts = []
    if result["fake_ids"]:
        parts.append(f"{len(result['fake_ids'])} 个 traceID/spanID 未在任何工具结果中出现"
                     f"（{', '.join(result['fake_ids'][:2])}…）")
    if result["near_miss_resources"]:
        sample = "、".join(f"{a}（最接近的真实资源是 {b}）"
                          for a, b, _ in result["near_miss_resources"][:2])
        parts.append(f"{len(result['near_miss_resources'])} 个资源名疑似写错：{sample}")
    # 注意这里必须先把后缀算好再拼：写成 f"…" + "…" if cond else "" 会因为
    # 条件表达式优先级低于 +，在 cond 为假时 append 一个空串，
    # join 出多余的"；" —— 已经踩过一次。
    tense = "，而结论在描述当前状态" if result.get("time_risk") else ""
    for name, age in result.get("stale_sources", [])[:2]:
        parts.append(f"{name} 返回的数据最新只到 {age // 60} 分钟前{tense}")
    return "；".join(parts) or "未发现可疑引用"


def refresh_prompt(result: dict) -> str:
    """C 类（数据时效）自动刷新复核指令。

    与 correction_prompt 的根本区别：那个只让模型**改写文字**（所以调用它时
    不给工具）；这个要求模型**真的再查一次最新数据**，所以调用方必须带上工具。

    原因是 C 类问题不在表述而在数据本身：回答里每个数字都真实存在、
    没有一处编造，只是它们描述的是半小时前的世界。让模型换个说法毫无意义，
    唯一有效的动作是重新取数、再看结论还成不成立。

    指令里三件事都必须写清，缺一条这次复核就白花：
      1. 只重查**过期的那几个数据源**，不要从头再排一遍（成本会失控）；
      2. 明确对比新旧：情况变了要说"初判 X、复核后 Y"，而不是悄悄改掉数字 ——
         用户有权知道结论被修正过，以及被修正成什么；
      3. 若最新数据与原结论一致，也要**明说已复核**，否则用户无法区分
         "确认过没变" 与 "根本没去查"。
    """
    stale = result.get("stale_sources") or []
    lines = ["[系统] 数据时效核对发现：你的结论描述的是**当前状态**，"
             "但它依赖的数据已经过期。请**用最新数据重新确认一次**，不要只改文字。"]
    for name, age in stale[:5]:
        lines.append(f"· `{name}` 返回的数据最新只到 **{age // 60} 分 {age % 60} 秒前**。")
    lines.append("")
    lines.append("请按以下要求处理：")
    lines.append("1. **只重新查询上面这几个过期的数据源**（同样的查询条件即可），"
                 "不要从头重排一遍 —— 其余已确认的证据无需重复取证；")
    lines.append("2. 拿到最新数据后，**逐项对比原结论**："
                 "若结论发生变化，必须写成「初判：⋯（基于 N 分钟前数据）→ "
                 "复核后：⋯（最新数据）」，把变化点和变化原因说清楚；")
    lines.append("3. 若最新数据与原结论**一致**，也要明确写一句"
                 "「已用最新数据复核，结论不变」—— 不要省略，"
                 "否则用户无法区分\"确认过没变\"和\"没去确认\"；")
    lines.append("4. 最后输出**完整的最终回答**（含复核说明），不要只输出差异部分。")
    return "\n".join(lines)


def correction_prompt(result: dict) -> str:
    """自纠正指令：只针对 A 类，且明确要求"删掉或改对"，不许换个说法糊过去。"""
    lines = ["[系统] 事实核对发现你的回答里存在**未经证实的引用**，请修正后重新输出完整回答。"]
    if result["fake_ids"]:
        lines.append(f"以下 traceID/spanID 没有出现在任何工具返回里，"
                     f"要么是编造的、要么记错了：{', '.join(result['fake_ids'][:5])}。"
                     f"请删除它们，或重新调用 query_traces 取回真实的 trace_id 再引用。")
    if result["near_miss_resources"]:
        pairs = "；".join(f"你写了 {a}，但真实存在的是 {b}"
                         for a, b, _ in result["near_miss_resources"][:5])
        lines.append(f"以下资源名与真实资源仅差几个字符，极可能是笔误或臆造：{pairs}。"
                     f"请按工具返回里的真实名称改正。")
    lines.append("修正时不要删掉结论本身，也不要用模糊表述替代具体数值 —— "
                 "拿不到证据的部分应当明说\"该项证据不足\"。")
    return "\n".join(lines)
