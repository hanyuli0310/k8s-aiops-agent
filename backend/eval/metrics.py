"""评测指标计算：**全部机械判定**，不含人工打分。

设计原则（决定这份报告能不能被信）：
1. 判定必须可复现 —— 同一份回答重跑一次，结论必须一样。所以不用"看起来对不对"，
   只用集合运算、字符串包含、数值比对这类确定性操作。
2. 判不准的地方要**明说判不准**，而不是给个看起来精确的数字。
   例如"回答是否遗漏了重要信息"没法机械判定，那就不做这项指标。
3. 每条指标都保留原始证据（回答摘要、命中项），方便人工抽查复核 ——
   一个不能被复核的指标等于没有。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set


# ══════════════════════════════════════════════════════════
# 一、集合类指标：precision / recall / F1
# ══════════════════════════════════════════════════════════

@dataclass
class PRF:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> Optional[float]:
        """预测为正的里面有多少是对的。没做任何预测时【没有定义】，返回 None。

        为什么不返回 0：没预测 ≠ 预测全错。返回 0 会在汇总时把平均值拉低，
        制造"准确率很差"的假象。宁可空着，也不要一个会误导的数。
        """
        d = self.tp + self.fp
        return self.tp / d if d else None

    @property
    def recall(self) -> Optional[float]:
        d = self.tp + self.fn
        return self.tp / d if d else None

    @property
    def f1(self) -> Optional[float]:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    def add(self, other: "PRF") -> "PRF":
        return PRF(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)


def compare_sets(predicted: Sequence[str], expected: Sequence[str]) -> PRF:
    """把"预测出的规则集"与"应当命中的规则集"对账。"""
    p, e = set(predicted), set(expected)
    return PRF(tp=len(p & e), fp=len(p - e), fn=len(e - p))


# ══════════════════════════════════════════════════════════
# 二、数值判定：回答里有没有给出正确的那个数
# ══════════════════════════════════════════════════════════

# 千分位与小数都要认；后置不拒字母，因为本项目数值常带单位（1.2s / 39300m）
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\d.])")


def numbers_in(text: str) -> List[float]:
    out = []
    for raw in _NUM.findall(text or ""):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def hits_number(answer: str, truth: float, *, tol: float = 0.0) -> bool:
    """回答里是否出现了正确数字。

    ⚠️ 这是**宽松判定**：只要正确数字出现过就算命中，不追究回答里是否还出现了
    别的数字。原因是回答里天然会有别的数字（级别分布、时间窗、条数明细），
    要求"只出现正确数字"会把正常回答判成错。
    代价是可能放过"20 条（其中 P1 有 15 条）"这类局部错误 ——
    所以每条用例都保留回答原文，报告里附上供抽查。
    """
    nums = numbers_in(answer)
    if tol <= 0:
        return any(abs(n - truth) < 1e-9 for n in nums)
    return any(abs(n - truth) <= tol for n in nums)


# ══════════════════════════════════════════════════════════
# 三、根因定位判定
# ══════════════════════════════════════════════════════════

@dataclass
class RootCauseVerdict:
    """一次故障定位的判定结果。四个维度分开记，不合成一个笼统的'对/错'。"""
    instance_hit: Optional[bool] = None      # 是否点出了正确的故障实体
    cross_flag: Optional[bool] = None        # 是否误报了同型的另一个实例（串报）
    keyword_hit: Optional[bool] = None       # 是否说出了正确的机理关键词
    table_hit: Optional[bool] = None         # 是否精确到表（仅慢查询类场景）
    rules: PRF = field(default_factory=PRF)  # 规则层面的 P/R

    @property
    def strict_ok(self) -> bool:
        """严格通过：该判的维度全部为真，且没有串报。

        "该判的维度"= 场景标注了什么就判什么。没标 expected_table 的场景
        不因为缺少表级信息而被扣分 —— 那是标注没要求，不是 Agent 的问题。
        """
        checks = [v for v in (self.instance_hit, self.keyword_hit, self.table_hit)
                  if v is not None]
        if not checks:
            return False
        return all(checks) and not self.cross_flag


def judge_root_cause(answer: str, scenario: dict) -> RootCauseVerdict:
    """按场景标注机械判定根因定位质量。

    判定口径刻意分成四个维度而不是一个总分：一个"定位准确率 70%"说明不了
    到底是找错了实例、还是实例对了但机理说错了 —— 而这两种错的后果完全不同
    （前者会把治理动作打到无辜实例上，后者只是解释不到位）。
    """
    text = answer or ""
    v = RootCauseVerdict()

    inst = scenario.get("expected_instance")
    if inst:
        v.instance_hit = inst in text

    other = scenario.get("must_not_flag_instance")
    if other:
        # 串报判定：提到了另一个实例，且【不是】在做排除性说明
        mentioned = other in text
        v.cross_flag = bool(mentioned and not _looks_excluded(text, other))

    kws = scenario.get("_expect_keywords_any")
    if kws:
        v.keyword_hit = _keyword_hit(text, kws)

    table = scenario.get("expected_table")
    if table:
        v.table_hit = _table_hit(text, table)

    return v


# 机理关键词前出现这些否定词时，不算命中：回答说的是"不是慢查询"。
# 只看紧邻前缀（而不是整句）："不是慢查询而是缓存雪崩"里的"慢查询"应判未命中，
# 但"慢查询不是唯一原因"里的"慢查询"仍是命中（否定词在后，不影响）。
_NEG_PREFIX = ("不是", "非", "并非", "不属于", "不是因为", "排除", "不涉及")
_NEG_WINDOW = 6                          # 否定词只往前看几个字


def _keyword_hit(text: str, kws: list) -> bool:
    """机理关键词命中，但排除它出现在否定短语里的情形。

    纯子串匹配的问题：回答写"本次**不是慢查询**问题，而是缓存雪崩"，
    会因为含"慢查询"而被判机理命中 —— 实际上它把机理说反了。
    只要有任一关键词在**非否定语境**下出现就算命中（expect_keywords_any 语义）。
    """
    for k in kws:
        for m in re.finditer(re.escape(k), text):
            prefix = text[max(0, m.start() - _NEG_WINDOW): m.start()]
            if not any(n in prefix for n in _NEG_PREFIX):
                return True
    return False


def _table_hit(text: str, table: str) -> bool:
    """表级命中，但不能被**同名前缀的服务名**假命中。

    本项目里 `inventory` 表与 `inventory-service` 服务真实共存：
    回答只提"inventory-service 的 CPU 饱和"时，纯子串匹配会把 `inventory`
    当成表级命中 —— 但它根本没说到那张表。这会让表级定位分虚高。

    判法：表名后紧跟连字符+字母（如 `inventory-service`）时，这一次不算。
    只要有一处是"干净的"表名引用（后面不接着服务名后缀）就算命中。
    """
    for m in re.finditer(re.escape(table), text):
        tail = text[m.end(): m.end() + 2]
        # 表名后紧跟 "-字母"（服务名形态）则跳过这一次
        if re.match(r"-[a-z]", tail):
            continue
        return True
    return False


_EXCLUSION_HINTS = (
    # 直接否定
    "未发现", "不涉及", "没有问题", "无异常", "无问题", "排除", "不是", "并非",
    "无需", "未受影响", "不受影响", "未告警", "未命中", "无告警",
    # 状态正常
    "正常", "健康", "平稳", "阈值内", "未越阈", "低于阈值", "在正常范围",
    # 对比语境（"相比 xxx"、"另一台 xxx"通常出现在排除性对照里）
    "另一台", "另一个", "相比", "对比", "而非", "区别于", "作为对照",
    # 时态性排除：“那个故障已经过去了”同样是把它排除在当前根因之外。
    # 这类词是实测补的（见下方 ⚠️ 段）：两份**正确**的回答都因为缺这类词而被误判为串报。
    "已平息", "平息", "已回落", "回落", "已恢复", "已解决", "已治理",
    "resolved", "此前", "先前", "之前", "早前", "上一轮", "上一次", "已不再",
    # 附带提及：明确标注了"这不是主结论"
    "附带观察", "附注", "另注", "仅供参考", "顺带", "不是本次",
)

# 判定窗口取 ±80 字符：中文技术表述一句话常有 40~60 字，
# ±40 会把"rds-mysql-order 的连接率 52%，处于正常范围"这类
# 明确的排除性说明切掉一半，从而把正确诊断误判成串报。
_EXCL_WINDOW = 80

# 否决词：窗口里同时出现这些词时，**即使有排除措辞也算串报**。
#
# 为何需要：补完时态排除词后立即出现了反方向误判 ——
# “rds-mysql-order 连接率虽有**回落**，但它**仍是**本次故障的**根因之一**”
# 这句话是真串报，却因为含“回落”而被当成排除放过了。
# 放宽指标很容易把它改成“永远及格”，所以必须有这道反向门。
#
# 词选得很窄，只要**明确把它断言成当前根因**的表述；
# 刻意不收 “仍有”/“两个库”/“同时” 这类词 —— 真实回答里
# “order 库**仍有** 843 条未治理慢查询”、“**两个库**呈跷跷板” 都是正确的补充说明，
# 收进来就会把刚修好的误判又造回来。
_CROSS_OVERRIDE = (
    "仍是", "仍然是", "依然是", "还是根因", "也是根因",
    "根因之一", "共同根因", "共同导致", "双重根因",
    "一起治理", "均需治理", "两者都需", "都需升配", "同时被打满",
)


def _looks_excluded(text: str, name: str) -> bool:
    """判断某实例名是否出现在"排除性说明"里（如"另一台 rds-mysql-order 正常"）。

    做法：取该名字周围的窗口，看有没有排除类措辞。

    ⚠️ 这是**启发式判定，有误差**，所以：
    1. 评测报告必须同时给出回答原文，让人能复核这一项；
    2. 误差方向刻意选成"对 Agent 不利"——只有明确出现排除措辞才算排除，
       否则一律计为串报。宁可低估，不可高估。

    实测教训（**这个判定已经错了两次**）：
    · 第一版窗口只有 ±40 字符、排除词只有 11 个，把一次**正确**的对照说明
      判成了串报（Agent 明确写了 order 库正常）。
    · 第二版补到 27 个词后仍然漏了**时态性排除**：两种调度模式在
      core_db_conn_exhausted 上各写了一句很好的区分——
      “附带观察：order 库连接率从 82% 回落到 55%（上一轮慢查询风暴平息）”、
      “此前 order 库的全表扫描故障已平息，相关风险项已 resolved”——
      却因为词表里没有“平息/回落/此前/resolved”而被判成串报，
      导致我得出了“串报是 Agent 推理层弱点”这个**错结论**并写进了报告。

    两次都是同一类错：把正确判成错误。**指标本身出错比 Agent 出错更糟** ——
    它会把优化引向不存在的问题。所以真机语料已固化成回归用例（见 tests）。
    """
    for m in re.finditer(re.escape(name), text):
        window = text[max(0, m.start() - _EXCL_WINDOW): m.end() + _EXCL_WINDOW]
        # 否决优先：明说它仍是当前根因的，不管同句里有没有排除措辞，都算串报
        if any(o in window for o in _CROSS_OVERRIDE):
            return False
        if any(h in window for h in _EXCLUSION_HINTS):
            return True
    return False


# ══════════════════════════════════════════════════════════
# 四、人工排障基线（**估算，不是实测**）
# ══════════════════════════════════════════════════════════

# 假设值集中放在这里，报告必须连同假设一起给出 —— 只给结论数字是不诚实的。
#
# ⚠️ **必须按任务类型分别取值**，这是第一版评测踩的坑：
#    第一版对所有任务都套"每次查询 2.5 分钟 + 5 分钟因果推断 + 4 次系统切换"，
#    结果连"当前 open 风险有几条"这种一句话查询都被算成"人工需 11.5 分钟"，
#    进而得出"提速 62 倍"。那是公式误用造出来的假象 ——
#    人工查一个计数打开控制台看一眼就够了，根本不需要因果推断。
#    把简单查询的提速倍数写进报告，等于虚报能力。
TASK_PROFILES = {
    # 一句话就能查到的事实（计数、单指标当前值）。人工也很快，
    # Agent 在这类任务上的价值是"自然语言接口"，**不是速度**。
    "simple_query": {
        "per_query": 1.0,      # 打开控制台/执行一条 SQL
        "switch": 0.0,         # 通常只碰一个系统
        "reasoning": 0.5,      # 读一下结果
        "note": "人工也只需一两分钟，不宜用来宣称提速",
    },
    # 多步排障：跨系统取证 + 时间轴对齐 + 因果推断，这才是原公式适用的场景。
    "diagnosis": {
        "per_query": 2.5,
        "switch": 1.0,
        "reasoning": 5.0,
        "note": "跨系统取证与因果推断，人工耗时的主要来源",
    },
}


def manual_baseline_minutes(tool_calls: int, systems_touched: int = 4,
                            task_type: str = "diagnosis") -> float:
    """估算人工完成同等任务所需时间。

    ⚠️ **这是估算，不是实测的人工对照组。** 项目没有真人计时数据，
    任何"人工 45 分钟"的说法都只能是模型化推算。公式：

        人工分钟 = 工具调用次数 × per_query + 涉及系统数 × switch + reasoning

    模型只主张一件事：**Agent 的一次工具调用，对应人工的一次跨系统查询**。
    这点站得住 —— 工具本身就是照着人工查询路径设计的。
    每次查询的单价则取决于人的熟练度，所以对外只给区间、不给单值。
    """
    prof = TASK_PROFILES.get(task_type) or TASK_PROFILES["diagnosis"]
    return (tool_calls * prof["per_query"]
            + systems_touched * prof["switch"]
            + prof["reasoning"])


def speedup_range(agent_seconds: float, tool_calls: int,
                  systems_touched: int = 4,
                  task_type: str = "diagnosis") -> Dict[str, dict]:
    """给出提速倍数的**区间**，而不是一个精确得可疑的单值。

    区间来自"每次人工查询耗时"的乐观/中位/保守取值（相对该任务类型基准的
    0.6× / 1.0× / 1.6×）——熟练工与新手的差别就在这里。
    """
    prof = TASK_PROFILES.get(task_type) or TASK_PROFILES["diagnosis"]
    out = {}
    for label, k in (("乐观", 0.6), ("中位", 1.0), ("保守", 1.6)):
        per_query = prof["per_query"] * k
        manual = (tool_calls * per_query + systems_touched * prof["switch"]
                  + prof["reasoning"])
        out[label] = {
            "per_query_min": round(per_query, 2),
            "manual_minutes": round(manual, 1),
            "speedup": round(manual * 60 / agent_seconds, 1) if agent_seconds > 0 else None,
        }
    return out


# ══════════════════════════════════════════════════════════
# 五、格式化
# ══════════════════════════════════════════════════════════

def pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def fmt_prf(p: PRF) -> str:
    return (f"P={pct(p.precision)} R={pct(p.recall)} F1={pct(p.f1)} "
            f"(tp={p.tp} fp={p.fp} fn={p.fn})")
