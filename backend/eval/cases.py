"""评测用例集：**人工标注的期望值**集中放在这里。

为什么单独一个文件：其余评测代码都是机械计算，只有这里含"人的判断"。
把它隔离出来，才能清楚区分「客观测量」与「基于标注的测量」——
标注错了，指标就没有意义，所以这份文件的每一条都要能说出理由。

标注原则：
1. 用运维人员**真实会说的话**，不是为了让路由器好过而写的标准句式；
2. 每类意图都要有**易混淆样本**（如"体检"同时命中 risk_scan 与 full_checkup）；
3. 数据查询类用例的真值必须能从数据库**当场算出来**，不写死数字 ——
   写死的期望值会随数据变化而失效，那种"失败"是假失败。
"""
from __future__ import annotations

# ══════════════════════════════════════════════════════════
# 一、意图路由用例（人工标注 expected_intent）
# ══════════════════════════════════════════════════════════
# 覆盖 intent.INTENTS 的 9 类。每条都标了标注理由，便于日后复核。

INTENT_CASES = [
    # --- data_ingest：明确要求把数据搬进来 ---
    ("采集集群可观测数据", "data_ingest"),
    ("把最新的监控指标同步一下", "data_ingest"),
    ("帮我拉取一下 SLS 的日志入库", "data_ingest"),

    # --- topology：问的是服务之间的关系 ---
    ("梳理一下服务拓扑", "topology"),
    ("这些服务之间的调用关系是什么样的", "topology"),
    ("下单链路都经过哪些服务", "topology"),

    # --- risk_scan：要求做检查/巡检，产出风险清单 ---
    ("做一次全面风险扫描", "risk_scan"),
    ("集群现在有哪些隐患", "risk_scan"),
    ("跑一遍巡检看看有没有问题", "risk_scan"),

    # --- full_checkup：一条龙（易与 risk_scan 混淆，刻意放进来）---
    ("做个全面体检", "full_checkup"),
    ("采集数据、梳理拓扑再扫一遍风险，一条龙走完", "full_checkup"),

    # --- fault_diagnose：报告了具体现象，要求定位原因 ---
    ("用户反馈下单接口很慢，帮我定位", "fault_diagnose"),
    ("支付接口错误率突然升高了，什么原因", "fault_diagnose"),
    ("订单服务响应超时，排查一下根因", "fault_diagnose"),
    ("为什么 P99 延迟涨到 1 秒以上了", "fault_diagnose"),

    # --- data_query：查具体数值，不要求分析原因 ---
    ("当前 open 风险有几条", "data_query"),
    ("MySQL 的连接使用率是多少", "data_query"),
    ("order-service 现在几个副本", "data_query"),
    ("查一下最近的慢查询", "data_query"),

    # --- remediation：要求执行动作 ---
    ("按你给的方案执行治理", "remediation"),
    ("把 order-service 扩到 3 个副本", "remediation"),
    ("给 orders 表加上那个复合索引", "remediation"),

    # --- rule_create：要求新增规则 ---
    ("帮我生成一条 CPU 超过 80% 就告警的规则", "rule_create"),
    ("新建一个内存告警规则", "rule_create"),

    # --- chat：与运维无关 ---
    ("你好，你是谁", "chat"),
    ("今天天气怎么样", "chat"),
]


# ══════════════════════════════════════════════════════════
# 二、数据查询准确性用例（真值当场从 DB 算，不写死）
# ══════════════════════════════════════════════════════════
# 每条给一个问题 + 一条求真值的 SQL + 判定方式。
# 判定"回答里是否出现了正确数字"是机械的，不需要人来读。
#
# ⚠️ 只挑**答案唯一且不随时间漂移**的问题：
#    "最近 5 分钟平均延迟"这类会在跑评测的几十秒里变化，不能用来判对错。

QUERY_CASES = [
    {
        "id": "Q1-open-risk-count",
        "question": "当前 open 状态的风险有几条？只回答条数。",
        "truth_sql": "SELECT COUNT(*) AS v FROM risk_findings WHERE status='open'",
        "why": "风险总数是最基本的事实，答错说明连自己的库都读不准",
    },
    {
        "id": "Q2-p1-risk-count",
        "question": "当前 open 风险里 P1 级别的有几条？只回答条数。",
        "truth_sql": ("SELECT COUNT(*) AS v FROM risk_findings "
                      "WHERE status='open' AND severity='P1'"),
        "why": "带过滤条件的计数，考验它会不会漏掉 status 或 severity 之一",
    },
    {
        "id": "Q3-topology-edges",
        "question": "当前拓扑里一共有多少条调用边？只回答数量。",
        "truth_sql": "SELECT COUNT(*) AS v FROM topology_edges",
        "why": "拓扑规模，跨表查询",
    },
    {
        "id": "Q4-resolved-count",
        "question": "已经 resolved 的风险有几条？只回答条数。",
        "truth_sql": "SELECT COUNT(*) AS v FROM risk_findings WHERE status='resolved'",
        "why": "与 Q1 互补，防止它把'全部风险'当成'open 风险'",
    },
    {
        "id": "Q5-rule-count",
        "question": "系统里一共内置了多少条风险规则？只回答数量。",
        "truth_sql": "SELECT COUNT(*) AS v FROM risk_rules",
        "why": "规则数是静态事实，答错说明没查库而是凭记忆编",
    },
]


# ══════════════════════════════════════════════════════════
# 三、稳定性用例（同一问题重复跑，看结论是否一致）
# ══════════════════════════════════════════════════════════
# 关注的不是"答案对不对"（那是 QUERY_CASES 的事），而是**同样输入是否给出
# 同样结论**。Agent 的不确定性来自采样温度与工具调用顺序，
# 一个连自己都不能复现的结论，在运维场景里没法用。

STABILITY_CASE = {
    "question": "当前 open 状态的风险有几条？各是什么级别？",
    "repeats": 3,
    # 判定：从每次回答里抽出"条数"这个关键数字，看是否每次相同
    "truth_sql": "SELECT COUNT(*) AS v FROM risk_findings WHERE status='open'",
}


# ══════════════════════════════════════════════════════════
# 四、故障定位用例（L2，需要 mock_server 注入）
# ══════════════════════════════════════════════════════════
# 真值直接取自 mock_server 的 faults.SCENARIOS，**不在这里重复抄一遍** ——
# 抄一遍就会漂移，评测报告会拿过期真值判对错。
# 这里只写"给 Agent 的提问"，以及该场景下判定命中所需的额外线索。

FAULT_CASES = [
    {
        "scenario": "slow_query_storm",
        "question": "用户反馈下单接口很慢，帮我定位根因，并说明是哪个数据库实例的问题。",
        # 除 expected_rules 外还要求答案点出故障实例
        "expect_instance": "rds-mysql-order",
        # 这个场景的因果链：缺索引 → 慢查询 → 连接占用 → 接口超时
        "expect_keywords_any": ["索引", "全表扫描", "慢查询"],
    },
    {
        "scenario": "rds_conn_spike",
        "question": "订单相关接口报错变多了，帮我定位根因，说明是哪个实例的问题。",
        "expect_instance": "rds-mysql-order",
        "expect_keywords_any": ["连接", "连接数", "连接池"],
    },
    {
        "scenario": "instance_down",
        "question": "有服务不可用了，帮我定位是哪里出了问题。",
        "expect_instance": None,          # 该场景 target 是服务实例，不固定校验
        "expect_keywords_any": ["实例", "副本", "不可用", "下线"],
    },
]
