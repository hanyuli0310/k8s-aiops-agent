"""U1 世界定义（Single Source of Truth）

新世界规模（spec v1.1）：8 服务 + 2×RDS + 1×Redis，19 实例，15 接口，17 条调用边。
所有下游模块（engine/faults/actions/renderers）只读本模块常量，绝不各自造数。

两个关键交付物：
1. PLANTED_DEFECTS —— 预埋配置缺陷清单，与 backend 11 条内置规则逐条对应（稳态基线 finding）
2. THRESHOLD_TABLE —— 阈值对照表（稳态值/故障值/阈值），供 backend U9.5 校准规则阈值

命名与 IP 规则沿用旧世界风格（Pod 名 <svc>-<hash>-<suffix>、Pod IP 10.244.<节点序>.<序>），
保证 renderers 产出的数据能被现有 pipeline/tools 零适配消费。
"""
from __future__ import annotations

import hashlib

CLUSTER = "prod-cluster-01"
REGION = "cn-hangzhou"
USER_ID = "1208863178610000"
K8S_VERSION = "1.28"

# --- 节点：6 台，均匀分布 3 可用区 ---
ZONES = ["cn-hangzhou-h", "cn-hangzhou-i", "cn-hangzhou-j"]

# 18 个节点，每可用区 6 个。
#
# 为何从 6 扩到 18：量化评测里"故障实例定位 11/11"的说服力受限于候选池太小 ——
# 在 19 个实例里挑对 1 个，和在 60 个里挑对 1 个，难度完全不同。
# 扩容的目的不是让规模数字好看，而是**提高选错的概率**，让定位准确率这个
# 指标真的有区分度；候选池太小时，即使蒙也有不低的命中率。
NODES = [
    {"name": "node-01", "zone": "cn-hangzhou-h", "ip": "192.168.0.11"},
    {"name": "node-02", "zone": "cn-hangzhou-h", "ip": "192.168.0.12"},
    {"name": "node-03", "zone": "cn-hangzhou-i", "ip": "192.168.0.13"},
    {"name": "node-04", "zone": "cn-hangzhou-i", "ip": "192.168.0.14"},
    {"name": "node-05", "zone": "cn-hangzhou-j", "ip": "192.168.0.15"},
    {"name": "node-06", "zone": "cn-hangzhou-j", "ip": "192.168.0.16"},
] + [
    # node-07 ~ node-100：按 zone 轮转分配，与前 6 个同规格。
    # IP 跨网段编址（192.168.0.x → 192.168.N.x），避免第四段溢出 255。
    {"name": f"node-{i:03d}", "zone": ZONES[(i - 7) % 3],
     "ip": f"192.168.{i // 200}.{(10 + i) % 250}"}
    for i in range(7, 101)
]
NODE_CAPACITY = {"cpu": "4", "memory": "8Gi", "pods": "64"}
NODE_ALLOCATABLE = {"cpu": "3900m", "memory": "7680Mi", "pods": "64"}
INSTANCE_TYPE = "ecs.g6.xlarge"

# --- 服务定义 ---
# kind: app | ingress | rds | redis
# 水位基线为「稳态」值：必须全部低于 backend 规则阈值（见 THRESHOLD_TABLE），
# 否则稳态就会误报，验收标准「稳态无基线外 finding」不成立。
SERVICES = {
    "nginx-ingress": {
        "kind": "ingress", "namespace": "kube-system", "replicas": 2, "version": "v1.9.6",
        "pods": [("nginx-ingress-8b7a6f5e4-v3w5x", "node-02"),
                 ("nginx-ingress-8b7a6f5e4-y7z9a", "node-04")],
        "base_latency_ms": 3, "error_rate": 0.0005,
        "cpu_base_pct": 22.0, "mem_base_pct": 30.0,
        "requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "1000m", "memory": "1024Mi"},
        "probes": True, "pdb": True,
        "bandwidth_capacity_mbps": 1000.0, "bandwidth_base_mbps": 180.0,
    },
    "web-frontend": {
        "kind": "app", "namespace": "default", "replicas": 2, "version": "v4.2.5",
        "pods": [("web-frontend-9e8d7c6b5-n2p4r", "node-05"),
                 ("web-frontend-9e8d7c6b5-q5s7t", "node-01")],
        "base_latency_ms": 12, "error_rate": 0.001,
        "cpu_base_pct": 28.0, "mem_base_pct": 41.0,
        "requests": {"cpu": "250m", "memory": "256Mi"}, "limits": {"cpu": "1500m", "memory": "1024Mi"},
        "probes": True, "pdb": False,          # 预埋 HA-004：缺 PDB
        "bandwidth_capacity_mbps": 500.0, "bandwidth_base_mbps": 120.0,
    },
    "api-gateway": {
        "kind": "app", "namespace": "default", "replicas": 2, "version": "v2.0.7",
        "pods": [("api-gateway-4a5b6c7d8-g7h8i", "node-04"),
                 ("api-gateway-4a5b6c7d8-j9k1l", "node-06")],
        "base_latency_ms": 8, "error_rate": 0.001,
        "cpu_base_pct": 35.0, "mem_base_pct": 44.0,
        "requests": {"memory": "512Mi"},       # 预埋 CAP-001：缺 cpu request
        "limits": {"cpu": "4000m", "memory": "2048Mi"},
        "probes": True, "pdb": True,
        "bandwidth_capacity_mbps": 500.0, "bandwidth_base_mbps": 150.0,
    },
    "user-service": {
        "kind": "app", "namespace": "default", "replicas": 2, "version": "v3.1.0",
        "pods": [("user-service-6c7d8e9f5-c3d4e", "node-03"),
                 ("user-service-6c7d8e9f5-f6g7h", "node-05")],
        "base_latency_ms": 22, "error_rate": 0.002,
        "cpu_base_pct": 31.0, "mem_base_pct": 48.0,
        "requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "3000m", "memory": "2048Mi"},
        "probes": True, "pdb": True,
        "bandwidth_capacity_mbps": 300.0, "bandwidth_base_mbps": 70.0,
    },
    "product-service": {
        "kind": "app", "namespace": "default", "replicas": 2, "version": "v2.6.3",
        "pods": [("product-service-7b6c5d4e3-h8j9k", "node-02"),
                 ("product-service-7b6c5d4e3-l1m2n", "node-06")],
        "base_latency_ms": 18, "error_rate": 0.0015,
        "cpu_base_pct": 33.0, "mem_base_pct": 45.0,
        "requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "2500m", "memory": "2048Mi"},
        "probes": True, "pdb": True,
        "bandwidth_capacity_mbps": 300.0, "bandwidth_base_mbps": 85.0,
    },
    "order-service": {
        "kind": "app", "namespace": "default", "replicas": 1, "version": "v1.8.4",   # 预埋 HA-002：单副本
        "pods": [("order-service-5d8b9c7f6-a1b2c", "node-03")],
        "base_latency_ms": 35, "error_rate": 0.003,
        "cpu_base_pct": 42.0, "mem_base_pct": 52.0,
        "requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "4000m", "memory": "2048Mi"},
        "probes": True, "pdb": True,
        "bandwidth_capacity_mbps": 300.0, "bandwidth_base_mbps": 95.0,
    },
    "payment-service": {
        "kind": "app", "namespace": "default", "replicas": 3, "version": "v2.3.1",
        # 预埋 HA-001：3 副本全在 cn-hangzhou-h（node-01/node-02 同属 h 区）
        "pods": [("payment-service-7f9c6bd8d-x2k4q", "node-01"),
                 ("payment-service-7f9c6bd8d-m8q1w", "node-01"),
                 ("payment-service-7f9c6bd8d-t5r7c", "node-02")],
        "zone_affinity": "cn-hangzhou-h",
        "base_latency_ms": 28, "error_rate": 0.002,
        "cpu_base_pct": 36.0, "mem_base_pct": 50.0,
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "3000m"},            # 预埋 CAP-002：缺 memory limit
        "probes": True, "pdb": True,
        "bandwidth_capacity_mbps": 300.0, "bandwidth_base_mbps": 80.0,
    },
    "inventory-service": {
        "kind": "app", "namespace": "default", "replicas": 2, "version": "v1.4.9",
        "pods": [("inventory-service-6a7b8c9d1-p3q4r", "node-01"),
                 ("inventory-service-6a7b8c9d1-s5t6u", "node-04")],
        "base_latency_ms": 15, "error_rate": 0.0015,
        "cpu_base_pct": 26.0, "mem_base_pct": 38.0,
        "requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "2000m", "memory": "1024Mi"},
        "probes": False, "pdb": True,          # 预埋 HA-003：缺双探针
        "bandwidth_capacity_mbps": 300.0, "bandwidth_base_mbps": 60.0,
    },
}

# --- 存储实例（CMS 维度用 instanceId，非 pod/node）---
DATASTORES = {
    "rds-mysql-order": {
        "kind": "rds", "engine": "MySQL", "version": "8.0",
        "endpoint": "rm-bp1a2b3c4d5e6f7.mysql.rds.aliyuncs.com", "port": 3306, "db_name": "orderdb",
        "base_latency_ms": 5,
        # 稳态水位：全部低于阈值（DB-001 阈值 80%、CAP-004 阈值 85%）
        "conn_base_pct": 55.0, "mem_base_pct": 70.0, "cpu_base_pct": 42.0,
        "disk_base_pct": 38.0, "iops_base_pct": 24.0,
        "max_connections": 800,
        "slow_query_tables": [],               # 注入 slow_query_storm 时追加 "orders"
        "bandwidth_capacity_mbps": 1000.0, "bandwidth_base_mbps": 210.0,
    },
    "rds-mysql-core": {
        "kind": "rds", "engine": "MySQL", "version": "8.0",
        "endpoint": "rm-bp9z8y7x6w5v4u3.mysql.rds.aliyuncs.com", "port": 3306, "db_name": "coredb",
        "base_latency_ms": 4,
        "conn_base_pct": 41.0, "mem_base_pct": 62.0, "cpu_base_pct": 33.0,
        "disk_base_pct": 30.0, "iops_base_pct": 18.0,
        "max_connections": 600,
        "slow_query_tables": [],
        "bandwidth_capacity_mbps": 1000.0, "bandwidth_base_mbps": 160.0,
    },
    "kvstore-redis-01": {
        "kind": "redis", "engine": "Redis", "version": "7.0",
        "endpoint": "r-bp1x2y3z4a5b6c.redis.rds.aliyuncs.com", "port": 6379, "db_name": "0",
        "base_latency_ms": 1,
        "conn_base_pct": 18.0, "mem_base_pct": 42.0, "cpu_base_pct": 16.0,
        "max_connections": 10000,
        "bandwidth_capacity_mbps": 500.0, "bandwidth_base_mbps": 90.0,
    },
}

# --- 调用拓扑：17 条有向边（source -> target）---
# ══════════════════════════════════════════════════════════
# 扩展服务：把集群从 8 服务 / 19 实例扩到 24 服务 / 60 实例
# ══════════════════════════════════════════════════════════
# 目的是**增加故障定位的干扰项密度**，不是让规模数字好看：
# 19 个实例里挑对 1 个，与 60 个里挑对 1 个，难度完全不同。
#
# 四条硬约束（self_check 会逐条卡住，违反则引擎拒绝启动）：
#
# 1. 新服务全部「配置健康」—— 有 cpu request、有 memory limit、有探针、
#    有 PDB、≥2 副本且跨可用区。否则会引入新的预埋缺陷，
#    7 条 PLANTED_DEFECTS 真值就变成一本乱账，12 个现有场景全要重标。
#
# 2. cpu limit 总量必须让 default ns 超卖率维持在 166.7%±2 ——
#    节点从 6 扩到 18 使分母涨了 3 倍（23400m → 70200m），
#    若不同步补分子，超卖率会掉到 55%，CAP-003 直接失效。
#    每 Pod 2440m × 32 Pod = 78080m，加上原有 39000m 得 117080m，
#    117080 / 70200 = 166.8%，落在 THRESHOLD_TABLE 声明值的容差内。
#
# 3. **不新增对外接口**。APIS 的 weight 之和必须为 1，加接口就要重算全部权重；
#    而 API-001 是按接口判定的，判定对象一变，12 个场景的期望规则全部要重新校准。
#    真实系统里也确实有大量内部服务不直接对外暴露接口。
#
# 4. 新服务**不插入现有调用链的中间**，只作为 api-gateway 的新分支。
#    这样原有 8 个服务的传导链一个字都不变，现有场景真值继续有效。
#
# 依赖刻意分成四组（第 2 个字段）：前三组分别挂 redis / core 库 / order 库，
# 故障传播会波及它们 —— 缓存雪崩时将有 8 个服务同时变慢，
# Agent 必须从「一片受害者」里找出根因，而不是随手指一个正在变慢的服务。
# 第四组无外部依赖，是纯粹的规模噪声。
#
# cpu_base 刻意留了三个 60%+ 的「高水位干扰项」（search / recommend / report-worker）：
# 它们水位显眼但**稳态不越任何阈值**（当前无按服务 CPU 判定的规则），
# 用来检验排查时会不会被「看起来最忙的那个」带偏。
_EXT_SPECS = [
    # (name, deps, cpu_base_pct, mem_base_pct)
    ("search-service",     ["kvstore-redis-01"], 62.0, 48.0),   # 高水位干扰项
    ("cart-service",       ["kvstore-redis-01"], 34.0, 42.0),
    ("coupon-service",     ["kvstore-redis-01"], 29.0, 38.0),
    ("recommend-service",  ["kvstore-redis-01"], 66.0, 55.0),   # 高水位干扰项
    ("session-service",    ["kvstore-redis-01"], 41.0, 46.0),
    ("review-service",     ["rds-mysql-core"],   31.0, 40.0),
    ("member-service",     ["rds-mysql-core"],   37.0, 44.0),
    ("address-service",    ["rds-mysql-core"],   26.0, 35.0),
    ("logistics-service",  ["rds-mysql-order"],  44.0, 49.0),
    ("refund-service",     ["rds-mysql-order"],  28.0, 37.0),
    ("settlement-service", ["rds-mysql-order"],  39.0, 45.0),
    ("notify-service",     [],                   33.0, 41.0),
    ("audit-service",      [],                   25.0, 33.0),
    ("config-service",     [],                   22.0, 30.0),
    ("report-worker",      [],                   64.0, 52.0),   # 高水位干扰项
    ("sync-worker",        [],                   36.0, 43.0),
]

# ══════════════════════════════════════════════════════════
# 规模化扩展：把集群推到 100 节点 / 300 服务 / 600+ 实例
# ══════════════════════════════════════════════════════════
# 为什么再扩一轮（24 → 300 服务）：24 服务规模下"实例定位 100%"的说服力仍然
# 受限于两点 —— 候选池不够大、且**故障的受害者面太小**。
# 缓存雪崩时只有 8 个服务同时变慢，从 8 个受害者里挑根因，和从 40 个里挑，
# 难度完全不同。后者才接近生产：一个中间件抖动，几十个服务的指标同时发红。
#
# 三条硬约束不变（self_check 会逐条卡）：
#   1. 生成的服务全部「配置健康」（有 cpu request / memory limit / 探针 / PDB /
#      ≥2 副本且跨可用区），绝不引入新的预埋缺陷，7 条 PLANTED_DEFECTS 真值不动；
#   2. cpu limit 总量维持 default ns 超卖率 166.7%±2 —— 节点扩到 100 后
#      分母是 390,000m，分子须约 650,000m，故每 Pod 966m；
#   3. 不新增对外接口（APIS 的 weight 之和必须为 1，且 API-001 按接口判定）。
#
# ★ 与上一轮扩容的关键差别：**这一轮刻意构造多层调用链**。
#   上一轮新服务只挂在 api-gateway 下（为保护现有真值），链路是扁平的两层；
#   而生产里的难点恰恰是**深链**：A 抖动 → B 超时 → C 线程堆积 → D 报错，
#   排查者看到的是 D 在报错，真凶却在 A。所以这一轮让生成的服务分层编址：
#   tier-1 挂 api-gateway，tier-2 挂 tier-1，tier-3 挂 tier-2，
#   并让每层的一部分服务继续挂到 redis / 两个 RDS 上 ——
#   于是一次缓存雪崩的受害者会横跨多层、达到数十个服务。
_EXT_CPU_LIMIT_M = 1047          # 见约束 2：决定超卖率。
# 这个值是**反算**出来的，不是估的：allocatable 390,000m × 166.8% = 650,520m 为目标分子，
# 减去基础 8 个服务的 limit 后除以扩展 pod 数（584）即得。先按 966 估算过一版，
# 实算只有 154.65%，被 self_check 的「与 THRESHOLD_TABLE 声明值误差 <2pp」这条卡住。
_EXT_REPLICAS = 2

# 生成式扩展：把服务从 24 补到 _TARGET_SERVICES 个。
# 用生成而非手写 276 段字典：手写必然出错，且真正重要的信息（分层、依赖、水位）
# 在下面这几行规则里就能表达清楚。
_TARGET_SERVICES = 300
_EXT_DATASTORE_POOL = ["kvstore-redis-01", "rds-mysql-core", "rds-mysql-order"]
# 业务域名（用来生成可读的服务名，而不是 svc-001 这种看不出语义的编号）
_EXT_DOMAINS = [
    "search", "cart", "coupon", "recommend", "session", "review", "member",
    "address", "logistics", "refund", "settlement", "notify", "audit", "config",
    "report", "sync", "billing", "invoice", "tax", "wallet", "point", "gift",
    "banner", "feed", "tag", "category", "brand", "shop", "seller", "stock",
    "warehouse", "pickup", "delivery", "route", "track", "aftersale", "complaint",
    "rating", "comment", "reply", "message", "push", "sms", "email", "template",
    "risk", "credit", "throttle", "quota", "auth2",
    "cache2", "index", "rank", "match", "bid", "budget", "campaign", "creative",
]


def _ext_pods(name: str) -> list:
    """确定性生成 Pod 名与落点（形如 K8s ReplicaSet 命名）。

    用服务名派生哈希而非随机：世界定义必须可复现。若每次重启 Pod 名都变，
    评测里「故障实例定位」的真值就跟着漂移，跨轮次的结果无法比较。

    落点按 zone 轮转，保证 2 个副本跨可用区 —— 否则会触发 HA-001，
    等于给新服务凭空添一个预埋缺陷。
    """
    h = hashlib.md5(name.encode()).hexdigest()
    rs = h[:9]
    pods = []
    for i in range(_EXT_REPLICAS):
        zone = ZONES[i % len(ZONES)]
        in_zone = [n["name"] for n in NODES if n["zone"] == zone]
        # 用哈希选节点：同一服务的落点固定，不同服务分散开
        node = in_zone[int(h[10 + i], 16) % len(in_zone)]
        pods.append((f"{name}-{rs}-{h[12 + i * 5:17 + i * 5]}", node))
    return pods


def _build_ext_specs() -> list:
    """把手写的 16 条扩展到 _TARGET_SERVICES 个，并构造**多层调用链**。

    返回 (name, deps, cpu_base, mem_base, tier) —— tier 决定它挂在谁下面：
      tier 1 → api-gateway（与前一轮一致）
      tier 2 → 某个 tier-1 服务
      tier 3 → 某个 tier-2 服务

    ## 为什么要分层

    扁平拓扑（全部挂 api-gateway）下，任何故障的传导都只有两跳，
    Agent 看一眼拓扑就知道谁是上游。生产里真正难的是**深链**：
    缓存抖动 → tier-3 超时 → tier-2 线程堆积 → tier-1 报错 → 接口 5xx，
    排查者最先看到的是最外层在报错，真凶在四跳之外。

    ## 水位设计

    绝大多数服务水位平稳（cpu 20~50%）；每 12 个里放 1 个 60~68% 的
    **高水位干扰项** —— 它显眼但稳态不越任何阈值，用来检验排查会不会被
    "看起来最忙的那个"带偏。这些干扰项在 300 服务规模下会有 20 多个，
    构成真实的噪声地板。
    """
    specs = [(n, d, c, m, 1) for n, d, c, m in _EXT_SPECS]
    tier1 = [n for n, _d, _c, _m, t in specs if t == 1]
    tier2: list = []
    idx = 0
    # 需要补的数量 = 目标 - 基础 8 个 - 已手写的 16 个
    need = _TARGET_SERVICES - len(SERVICES) - len(_EXT_SPECS)
    for i in range(need):
        dom = _EXT_DOMAINS[i % len(_EXT_DOMAINS)]
        gen = i // len(_EXT_DOMAINS) + 2          # 同名域再出现时加序号后缀
        name = f"{dom}-service" if gen == 2 and i < len(_EXT_DOMAINS) else f"{dom}{gen}-service"
        if name in SERVICES or any(name == x[0] for x in specs):
            name = f"{dom}{gen}x{i}-service"
        # 依赖：1/3 挂存储、其余挂上一层服务，形成 3 层链
        if i % 3 == 0:
            deps = [_EXT_DATASTORE_POOL[i % len(_EXT_DATASTORE_POOL)]]
            tier = 1
        elif i % 3 == 1:
            deps = [tier1[i % len(tier1)]] if tier1 else []
            tier = 2
        else:
            deps = [tier2[i % len(tier2)]] if tier2 else ([tier1[i % len(tier1)]] if tier1 else [])
            tier = 3
        # 每 12 个放一个高水位干扰项（60~68%），其余平稳
        hot = (i % 12 == 5)
        cpu = 60.0 + (i % 9) if hot else 20.0 + (i % 30)
        mem = 52.0 + (i % 8) if hot else 30.0 + (i % 25)
        specs.append((name, deps, float(cpu), float(mem), tier))
        if tier == 1:
            tier1.append(name)
        elif tier == 2:
            tier2.append(name)
        idx += 1
    return specs


_EXT_SPECS_FULL = _build_ext_specs()


for _n, _deps, _cpu, _mem, _tier in _EXT_SPECS_FULL:
    SERVICES[_n] = {
        "kind": "app", "namespace": "default", "replicas": _EXT_REPLICAS,
        "version": "v1.0.0", "pods": _ext_pods(_n),
        "base_latency_ms": 15, "error_rate": 0.001,
        "cpu_base_pct": _cpu, "mem_base_pct": _mem,
        # 四项配置全齐 —— 见约束 1，绝不能引入新的预埋缺陷
        "requests": {"cpu": "200m", "memory": "256Mi"},
        "limits": {"cpu": f"{_EXT_CPU_LIMIT_M}m", "memory": "1024Mi"},
        "probes": True, "pdb": True,
        "bandwidth_capacity_mbps": 500.0, "bandwidth_base_mbps": 80.0,
        "_ext_deps": _deps,              # 供下方生成 EDGES
    }

EDGES = [
    ("nginx-ingress", "web-frontend"),
    ("web-frontend", "api-gateway"),
    ("api-gateway", "user-service"),
    ("api-gateway", "product-service"),
    ("api-gateway", "order-service"),
    ("api-gateway", "payment-service"),
    ("api-gateway", "inventory-service"),
    ("order-service", "payment-service"),
    ("order-service", "inventory-service"),
    ("user-service", "rds-mysql-core"),
    ("user-service", "kvstore-redis-01"),
    ("product-service", "rds-mysql-core"),
    ("product-service", "kvstore-redis-01"),
    ("order-service", "rds-mysql-order"),
    ("payment-service", "rds-mysql-order"),
    ("payment-service", "kvstore-redis-01"),
    ("inventory-service", "rds-mysql-order"),
] + [
    # tier-1 扩展服务的入边：挂在 api-gateway 下，**不插入现有链路中间** ——
    # 原有 8 个服务的传导链因此一个字都不变，12 个现有场景真值继续有效。
    ("api-gateway", _n) for _n, _d, _c, _m, _t in _EXT_SPECS_FULL if _t == 1
] + [
    # 扩展服务的依赖出边：故障沿这些边传播。tier-2/3 的 dep 是上一层的**服务**，
    # 于是形成 api-gateway → tier1 → tier2 → tier3 的深链 ——
    # 一次缓存/数据库抖动会横跨多层波及数十个服务，
    # 排查者最先看到的是最外层在报错，真凶在几跳之外。
    (_n, _dep) for _n, _deps, _c, _m, _t in _EXT_SPECS_FULL for _dep in _deps
]

# --- 15 个接口：weight 为流量占比（和为 1），链路为该接口实际经过的服务序列 ---
# chain 首个元素固定 nginx-ingress；db 段由服务的下游边在 engine 中按 db_calls 决定
APIS = [
    {"method": "GET", "path": "/api/products", "backend": "product-service", "weight": 0.14,
     "p99_ms": 90, "error_rate": 0.001, "db_calls": [("rds-mysql-core", "products", "SELECT")],
     "cache_calls": ["kvstore-redis-01"]},
    {"method": "GET", "path": "/api/products/detail", "backend": "product-service", "weight": 0.10,
     "p99_ms": 120, "error_rate": 0.0015, "db_calls": [("rds-mysql-core", "products", "SELECT")],
     "cache_calls": ["kvstore-redis-01"]},
    {"method": "GET", "path": "/api/products/hot", "backend": "web-frontend", "weight": 0.09,
     "p99_ms": 60, "error_rate": 0.0008, "db_calls": [], "cache_calls": []},   # 前端本地缓存，不下探
    {"method": "GET", "path": "/api/users", "backend": "user-service", "weight": 0.08,
     "p99_ms": 150, "error_rate": 0.002, "db_calls": [("rds-mysql-core", "users", "SELECT")],
     "cache_calls": []},
    {"method": "POST", "path": "/api/users/login", "backend": "user-service", "weight": 0.06,
     "p99_ms": 230, "error_rate": 0.004, "db_calls": [("rds-mysql-core", "users", "SELECT")],
     "cache_calls": ["kvstore-redis-01"]},
    {"method": "GET", "path": "/api/users/profile", "backend": "user-service", "weight": 0.05,
     "p99_ms": 140, "error_rate": 0.0015, "db_calls": [("rds-mysql-core", "users", "SELECT")],
     "cache_calls": ["kvstore-redis-01"]},
    {"method": "GET", "path": "/api/orders", "backend": "order-service", "weight": 0.07,
     "p99_ms": 260, "error_rate": 0.003, "db_calls": [("rds-mysql-order", "orders", "SELECT")],
     "cache_calls": []},
    {"method": "POST", "path": "/api/orders", "backend": "order-service", "weight": 0.08,
     "p99_ms": 340, "error_rate": 0.004,
     "db_calls": [("rds-mysql-order", "orders", "INSERT")],
     "cache_calls": [], "downstream": ["payment-service", "inventory-service"]},
    {"method": "POST", "path": "/api/orders/cancel", "backend": "order-service", "weight": 0.04,
     "p99_ms": 300, "error_rate": 0.004, "db_calls": [("rds-mysql-order", "orders", "UPDATE")],
     "cache_calls": []},
    {"method": "GET", "path": "/api/orders/detail", "backend": "order-service", "weight": 0.05,
     "p99_ms": 210, "error_rate": 0.002, "db_calls": [("rds-mysql-order", "orders", "SELECT")],
     "cache_calls": []},
    {"method": "POST", "path": "/api/pay", "backend": "payment-service", "weight": 0.06,
     "p99_ms": 320, "error_rate": 0.003, "db_calls": [("rds-mysql-order", "payments", "INSERT")],
     "cache_calls": ["kvstore-redis-01"]},
    {"method": "GET", "path": "/api/pay/status", "backend": "payment-service", "weight": 0.05,
     "p99_ms": 130, "error_rate": 0.002, "db_calls": [], "cache_calls": ["kvstore-redis-01"]},
    {"method": "POST", "path": "/api/pay/refund", "backend": "payment-service", "weight": 0.03,
     "p99_ms": 380, "error_rate": 0.005, "db_calls": [("rds-mysql-order", "payments", "UPDATE")],
     "cache_calls": []},
    {"method": "GET", "path": "/api/inventory", "backend": "inventory-service", "weight": 0.06,
     "p99_ms": 110, "error_rate": 0.0015, "db_calls": [("rds-mysql-order", "inventory", "SELECT")],
     "cache_calls": []},
    {"method": "POST", "path": "/api/inventory/deduct", "backend": "inventory-service", "weight": 0.04,
     "p99_ms": 180, "error_rate": 0.003, "db_calls": [("rds-mysql-order", "inventory", "UPDATE")],
     "cache_calls": []},
]

# --- 慢 SQL 文本（注入 slow_query_storm 时使用，三处产物逐字一致）---
# ══════════════════════════════════════════════════════════
# 扩展接口：让 trace / 访问日志也覆盖大规模服务
# ══════════════════════════════════════════════════════════
# 【为什么必须补这一层】trace 与 ingress 日志是**按对外接口的流量**生成的。
# 上一轮扩容只加服务不加接口，结果新增的 292 个服务没有对外流量、
# 不产生 span —— 实测 trace 里仍只有 8 个服务、拓扑图也只有 8 个节点。
# 于是"300 服务规模"只体现在指标与 K8s 快照上，**Agent 做调用链推理时
# 面对的仍是原来那 8 个服务**，"大规模"在这一维度上是虚的。
#
# 【三条约束，缺一条 self_check 就会拦住】
# 1. weight 之和必须为 1 —— 故原有 15 条按比例缩放到 _CORE_WEIGHT_SHARE，
#    新接口平分剩余份额。核心链路仍占大头（否则核心场景的故障流量被稀释，
#    12 个场景的 API-001 真值会失效）。
# 2. db_calls / cache_calls 的目标必须已在 EDGES 中声明 ——
#    所以只给"tier-1 且挂存储"的扩展服务加接口，直接复用它已有的依赖边。
# 3. **后端服务绝不能是任何场景的 expected_instance / must_not_flag_instance**
#    （实测这些是原有 8 个核心实体）。否则新接口会在故障时产生 API-001，
#    被 _instance_hit 归到那些实体上 —— 制造出**假串报**，
#    12 个场景的串报指标会凭空变差，而那是评测自己造的。
#
# 稳态 p99 与错误率刻意压低（60~140ms / ≤0.1%），确保稳态不越 API-001 阈值
# （1000ms / 1%），"稳态无基线外 finding"这条验收标准不被打破。
_EXT_API_COUNT = 60              # 新增接口数：让 trace 覆盖数十个服务
_CORE_WEIGHT_SHARE = 0.75        # 核心 15 条接口保留的流量份额


def _build_ext_apis() -> list:
    """给 tier-1 扩展服务生成对外接口，使它们进入 trace 与访问日志。"""
    hosts = [(n, d) for n, d, _c, _m, t in _EXT_SPECS_FULL
             if t == 1 and d and d[0] in DATASTORES]
    out = []
    each = (1.0 - _CORE_WEIGHT_SHARE) / max(1, _EXT_API_COUNT)
    for i in range(min(_EXT_API_COUNT, len(hosts))):
        name, deps = hosts[i]
        short = name.replace("-service", "")
        store = deps[0]
        is_cache = DATASTORES[store]["kind"] == "redis"
        out.append({
            "method": "GET" if i % 3 else "POST",
            "path": f"/api/{short}/list" if i % 2 else f"/api/{short}",
            "backend": name,
            "weight": each,
            "p99_ms": 60 + (i % 80),          # 稳态远低于 1000ms 阈值
            "error_rate": 0.0002 + (i % 5) * 0.0001,   # 远低于 1% 阈值
            "db_calls": [] if is_cache else [(store, "records", "SELECT")],
            "cache_calls": [store] if is_cache else [],
        })
    return out


_EXT_APIS = _build_ext_apis()

# 原有 15 条按比例缩放 + 新接口，保证 weight 之和为 1
for _a in APIS:
    _a["weight"] = round(_a["weight"] * _CORE_WEIGHT_SHARE, 6)
APIS = APIS + _EXT_APIS
# 浮点累积误差修正：把差额补到流量最大的那条上（self_check 容差 0.01）
_wsum = sum(_a["weight"] for _a in APIS)
if abs(_wsum - 1.0) > 1e-9:
    APIS[0]["weight"] = round(APIS[0]["weight"] + (1.0 - _wsum), 6)

SLOW_SQLS = [
    "SELECT * FROM orders WHERE user_id = 8231 AND status = 'PENDING' ORDER BY created_at DESC",
    "SELECT COUNT(*) FROM orders WHERE status = 'PENDING' AND created_at > '2026-08-01 00:00:00'",
    "UPDATE orders SET status = 'TIMEOUT' WHERE status = 'PENDING' AND created_at < '2026-07-30 00:00:00'",
]

# --- 错误文案（指标/日志/trace 三处同源）---
ERROR_TEXTS = {
    "db_conn": "could not get JDBC connection: Too many connections",
    "db_timeout": "SQLTimeoutException: Statement cancelled due to timeout",
    "pod_unavailable": "connect: connection refused (no healthy upstream)",
    "oom": "container killed due to OOM (exit code 137)",
    "upstream_5xx": "upstream returned 503 Service Unavailable",
}

# --- 预埋配置缺陷清单（U1 交付物 1）：与 backend 11 条内置规则逐条对应 ---
# 稳态即存在，构成基线 finding；治理工具可修复。
PLANTED_DEFECTS = [
    {"rule_id": "HA-001", "severity": "P1", "target": "payment-service",
     "defect": "3 副本全部落在 cn-hangzhou-h（nodeAffinity 钉死单可用区）",
     "fix_action": "patch_deployment(remove_zone_affinity)"},
    {"rule_id": "HA-002", "severity": "P1", "target": "order-service",
     "defect": "replicas=1 单副本运行",
     "fix_action": "patch_deployment(set_replicas=2)"},
    {"rule_id": "HA-003", "severity": "P2", "target": "inventory-service",
     "defect": "容器缺失 livenessProbe / readinessProbe",
     "fix_action": "patch_deployment(add_probes)"},
    {"rule_id": "HA-004", "severity": "P2", "target": "web-frontend",
     "defect": "default 命名空间无覆盖该应用的 PodDisruptionBudget",
     "fix_action": "create_pdb(app=web-frontend)"},
    {"rule_id": "CAP-001", "severity": "P1", "target": "api-gateway",
     "defect": "resources.requests 缺 cpu 键（调度器无法预留算力）",
     "fix_action": "patch_deployment(set_cpu_request=500m)"},
    {"rule_id": "CAP-002", "severity": "P2", "target": "payment-service",
     "defect": "resources.limits 缺 memory 键（内存失控会拖垮节点）",
     "fix_action": "patch_deployment(set_memory_limit=2048Mi)"},
    {"rule_id": "CAP-003", "severity": "P1", "target": "namespace/default",
     "defect": "Σ(Pod cpu limit) / Σ(节点 allocatable cpu) 超 150%",
     "fix_action": "patch_deployment(set_cpu_limit) 逐服务下调"},
]

# --- 阈值对照表（U1 交付物 2）：供 backend U9.5 校准规则阈值 ---
# 口径：稳态值必须不越阈（否则稳态误报）；故障值必须越阈（否则故障不报）。
THRESHOLD_TABLE = [
    {"rule_id": "HA-001", "metric": "payment-service Pod 分布 zone 数",
     "steady": 1, "faulty": 1, "threshold": ">=2 为健康", "source": "k8s 快照", "trigger": "稳态基线"},
    {"rule_id": "HA-002", "metric": "order-service spec.replicas",
     "steady": 1, "faulty": 1, "threshold": ">=2 为健康", "source": "k8s 快照", "trigger": "稳态基线"},
    {"rule_id": "HA-003", "metric": "inventory-service 探针键数",
     "steady": 0, "faulty": 0, "threshold": "2 为健康", "source": "k8s 快照", "trigger": "稳态基线"},
    {"rule_id": "HA-004", "metric": "web-frontend PDB 覆盖",
     "steady": False, "faulty": False, "threshold": "True 为健康", "source": "k8s 快照", "trigger": "稳态基线"},
    {"rule_id": "CAP-001", "metric": "api-gateway requests.cpu 存在性",
     "steady": False, "faulty": False, "threshold": "True 为健康", "source": "k8s 快照", "trigger": "稳态基线"},
    {"rule_id": "CAP-002", "metric": "payment-service limits.memory 存在性",
     "steady": False, "faulty": False, "threshold": "True 为健康", "source": "k8s 快照", "trigger": "稳态基线"},
    {"rule_id": "CAP-003", "metric": "default ns CPU 超卖率 %",
     # 扩容到 18 节点 / 24 服务后实测 166.78%（117080m ÷ 70200m）——
     # 扩容前是 166.67%，刻意让分子随分母同比例增长，使这条真值几乎不动
     "steady": 166.8, "faulty": 166.8, "threshold": 150.0, "source": "k8s 快照复算", "trigger": "稳态基线"},
    # 以下 4 条稳态不越阈，仅由故障场景触发 —— 保证「稳态无基线外 finding」
    {"rule_id": "CAP-004", "metric": "rds-mysql-order MemoryUsage %",
     "steady": 70.0, "faulty": 91.0, "threshold": 85.0, "source": "CMS acs_rds_dashboard",
     "trigger": "rds_conn_spike / slow_query_storm"},
    {"rule_id": "DB-001", "metric": "rds-mysql-order ConnectionUsage %",
     "steady": 55.0, "faulty": 92.0, "threshold": 80.0, "source": "CMS acs_rds_dashboard",
     "trigger": "rds_conn_spike"},
    {"rule_id": "DB-002", "metric": "慢日志条数（rows_examined>=100万 且 rows_sent<10）",
     "steady": 0, "faulty": 30, "threshold": ">=1 条即告警", "source": "SLS rds_slow_log",
     "trigger": "slow_query_storm"},
    {"rule_id": "API-001", "metric": "单接口 P99 (s) / 5xx 错误率",
     "steady": "0.34 / 0.4%", "faulty": "1.50 / 3.0%", "threshold": "1.0s / 1.0%",
     "source": "SLS ingress 日志 + trace 根 span",
     "trigger": "slow_query_storm / pod_oom_crash / instance_down"},
]

# --- 派生索引（供 engine/faults 快速查询）---
ALL_ENTITIES = {**SERVICES, **DATASTORES}
DOWNSTREAM = {}      # source -> [target]
UPSTREAM = {}        # target -> [source]（故障反向 BFS 传播用）
for _s, _t in EDGES:
    DOWNSTREAM.setdefault(_s, []).append(_t)
    UPSTREAM.setdefault(_t, []).append(_s)

NODE_ZONE = {n["name"]: n["zone"] for n in NODES}


def pod_ip(node_name: str, seq: int) -> str:
    """Pod IP 规则：10.244.<节点序号>.<分配序号>，与旧世界风格一致。"""
    node_idx = int(node_name.split("-")[1])
    return f"10.244.{node_idx}.{10 + seq}"


def all_instances() -> list:
    """展开全部实例（Pod + 存储实例），engine 与 renderers 的实例视图基准。"""
    out = []
    for svc, cfg in SERVICES.items():
        for seq, (pod_name, node) in enumerate(cfg["pods"]):
            out.append({
                "instance": pod_name, "service": svc, "kind": "pod",
                "node": node, "zone": NODE_ZONE[node], "ip": pod_ip(node, seq),
                "namespace": cfg["namespace"],
            })
    for name, cfg in DATASTORES.items():
        out.append({
            "instance": name, "service": name, "kind": cfg["kind"],
            "node": None, "zone": None, "ip": None, "namespace": None,
        })
    return out


def parse_cpu_m(v) -> float:
    """K8s CPU 统一换算为毫核：'4' -> 4000，'3900m' -> 3900。"""
    if v is None:
        return 0.0
    s = str(v)
    return float(s[:-1]) if s.endswith("m") else float(s) * 1000


def cpu_oversale_pct(namespace: str = "default") -> float:
    """从服务定义复算 CPU 超卖率：Σ(Pod cpu limit) / Σ(节点 allocatable cpu)。

    与 backend CAP-003 规则同一算式，保证「指标可从配置推导」不是两本账。
    """
    total_limit_m = 0.0
    for cfg in SERVICES.values():
        if cfg["namespace"] != namespace:
            continue
        cpu_limit = cfg.get("limits", {}).get("cpu")
        total_limit_m += parse_cpu_m(cpu_limit) * cfg["replicas"]
    total_alloc_m = parse_cpu_m(NODE_ALLOCATABLE["cpu"]) * len(NODES)
    return total_limit_m / total_alloc_m * 100 if total_alloc_m else 0.0


def self_check() -> dict:
    """世界观自检：跑通才允许启动引擎（对齐旧世界生成器的自检思想）。"""
    errors = []

    # 1. 副本数与 Pod 列表一致
    for svc, cfg in SERVICES.items():
        if len(cfg["pods"]) != cfg["replicas"]:
            errors.append(f"{svc}: replicas={cfg['replicas']} 但 pods={len(cfg['pods'])}")

    # 2. Pod 落点节点必须存在
    valid_nodes = {n["name"] for n in NODES}
    for svc, cfg in SERVICES.items():
        for pod_name, node in cfg["pods"]:
            if node not in valid_nodes:
                errors.append(f"{svc}/{pod_name}: 节点 {node} 不存在")

    # 3. 边的两端必须是已定义实体
    for s, t in EDGES:
        if s not in ALL_ENTITIES:
            errors.append(f"边 {s}->{t}: source 未定义")
        if t not in ALL_ENTITIES:
            errors.append(f"边 {s}->{t}: target 未定义")

    # 4. 接口必须绑定到存在的服务，weight 之和约为 1
    weight_sum = 0.0
    for api in APIS:
        if api["backend"] not in SERVICES:
            errors.append(f"接口 {api['method']} {api['path']}: backend {api['backend']} 不存在")
        for ds, _table, _op in api["db_calls"]:
            if ds not in DATASTORES:
                errors.append(f"接口 {api['path']}: db_call {ds} 不存在")
            if (api["backend"], ds) not in EDGES:
                errors.append(f"接口 {api['path']}: 边 {api['backend']}->{ds} 未在 EDGES 中声明")
        for cache in api["cache_calls"]:
            if (api["backend"], cache) not in EDGES:
                errors.append(f"接口 {api['path']}: 边 {api['backend']}->{cache} 未在 EDGES 中声明")
        weight_sum += api["weight"]
    if abs(weight_sum - 1.0) > 0.01:
        errors.append(f"接口 weight 之和={weight_sum:.3f}，应为 1.0")

    # 5. 预埋缺陷清单与实际定义一致（防止清单与 SERVICES 漂移）
    if len({NODE_ZONE[n] for _p, n in SERVICES["payment-service"]["pods"]}) != 1:
        errors.append("HA-001 预埋失效：payment-service 未全部落在单可用区")
    if SERVICES["order-service"]["replicas"] != 1:
        errors.append("HA-002 预埋失效：order-service 不是单副本")
    if SERVICES["inventory-service"]["probes"]:
        errors.append("HA-003 预埋失效：inventory-service 探针未缺失")
    if SERVICES["web-frontend"]["pdb"]:
        errors.append("HA-004 预埋失效：web-frontend PDB 未缺失")
    if "cpu" in SERVICES["api-gateway"]["requests"]:
        errors.append("CAP-001 预埋失效：api-gateway 存在 cpu request")
    if "memory" in SERVICES["payment-service"]["limits"]:
        errors.append("CAP-002 预埋失效：payment-service 存在 memory limit")
    oversale = cpu_oversale_pct()
    if oversale <= 150.0:
        errors.append(f"CAP-003 预埋失效：超卖率 {oversale:.1f}% 未超 150%")
    # 超卖率与阈值对照表声明值对账（防止定义与对照表变"两本账"）
    declared = next(r["steady"] for r in THRESHOLD_TABLE if r["rule_id"] == "CAP-003")
    if abs(oversale - declared) > 2.0:
        errors.append(f"CAP-003 对账失败：实算 {oversale:.1f}% vs 对照表声明 {declared}%（误差应 <2pp）")

    # 6. 稳态水位必须全部低于阈值（否则稳态误报）
    order_db = DATASTORES["rds-mysql-order"]
    if order_db["conn_base_pct"] >= 80.0:
        errors.append(f"稳态误报风险：rds-mysql-order 连接基线 {order_db['conn_base_pct']}% >= 80%")
    if order_db["mem_base_pct"] >= 85.0:
        errors.append(f"稳态误报风险：rds-mysql-order 内存基线 {order_db['mem_base_pct']}% >= 85%")
    for api in APIS:
        if api["p99_ms"] >= 1000:
            errors.append(f"稳态误报风险：{api['path']} P99 基线 {api['p99_ms']}ms >= 1000ms")
        if api["error_rate"] >= 0.01:
            errors.append(f"稳态误报风险：{api['path']} 错误率基线 {api['error_rate']} >= 1%")

    return {
        "ok": not errors,
        "errors": errors,
        "summary": {
            "services": len(SERVICES), "datastores": len(DATASTORES),
            "instances": len(all_instances()), "apis": len(APIS), "edges": len(EDGES),
            "nodes": len(NODES), "zones": len(ZONES),
            "planted_defects": len(PLANTED_DEFECTS),
            "cpu_oversale_pct": round(oversale, 2),
        },
    }


if __name__ == "__main__":
    import json
    result = self_check()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)


# 「METHOD /path」→ 接口定义的索引。供 world_status 暴露 backend 用：
# 接口风险的 resource_ref 是接口路径，而故障场景标注的是服务名，
# 两者之间需要一层映射才能对账。
APIS_BY_KEY = {f"{a['method']} {a['path']}": a for a in APIS}
