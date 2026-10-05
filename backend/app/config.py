import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")

# --- LLM ---
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-max")
LLM_MODEL_FAST = os.getenv("LLM_MODEL_FAST", "qwen-turbo")

# --- 数据库：优先 MySQL，未配置或连不上时由 db.py 降级 SQLite ---
# 两种配置方式：DATABASE_URL 直接给完整 URL，或分开给 DB_* 变量（推荐，
# 密码含 # @ : / 等特殊字符时会自动 URL 转义，不用手工编码）。
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "ai_devops_cmdb_lin")


def _build_database_url() -> str:
    explicit = os.getenv("DATABASE_URL", "")
    if explicit:
        return explicit
    if DB_HOST:
        return (f"mysql+pymysql://{quote_plus(DB_USER)}:{quote_plus(DB_PASSWORD)}"
                f"@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4")
    return ""


DATABASE_URL = _build_database_url()
SQLITE_URL = f"sqlite:///{BACKEND_DIR / 'aiops.db'}"

# --- Mock 数据集（团队标准位置：仓库根目录 data/data）---
MOCK_DATA_DIR = Path(os.getenv("MOCK_DATA_DIR", str(BACKEND_DIR.parent / "data" / "data"))).resolve()
if not MOCK_DATA_DIR.is_absolute():
    MOCK_DATA_DIR = (BACKEND_DIR / MOCK_DATA_DIR).resolve()

# --- Agent loop ---
# 单段的工具调用步数上限。调小到 3 就能在真实模型上快速触发续跑。
#
# 为何从 12 提到 30：续跑的第三道闸门要求【模型维护了 plan】，
# 而模型并不总会调 update_plan。没有 plan 时步数耗尽就直接收场，
# 单段太小会把本来能一口气做完的任务提前截断。
# 质量优先：单段 30 步在多故障并发这类任务上会频繁触发续跑，而每次续跑都要
# 重建上下文、生成交接摘要（额外一轮 LLM + 信息损耗）。放宽到 80 让绝大多数
# 任务能在一段内做完 —— 实测最重的三故障并发用了 51 次工具调用。
# 仍保留有限值：模型自己维护 plan，无限步数会让"跑不完"变成真正的死循环。
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "80"))
TOOL_RESULT_MAX_CHARS = 2000

# --- 上下文压缩阶梯（E-3）---
# token 维度的上限。为什么不能只看字符数：中文与 JSON 的 token 密度差一倍，
# 只用字符阈值会让中文为主的会话压缩得太晚。
#
# 取值依据（均为真机实测，不是估算）：
#   · qwen3.6-flash 实测吃下 150,831 prompt_tokens，窗口 ≥150k（与 max 同级，官方称 200k）；
#   · est_tokens() 校准后稳定高估真实值约 1.2 倍，所以 est 180000 对应真实约 150k；
#   · 单个 Agent 的工具 schema 另占 ~1.7~2.1k token，且【不计入 est_tokens】；
#   → 180000 留出约 25% 窗口给 schema、模型输出与估算波动。
CONTEXT_MAX_TOKENS = int(os.getenv("CONTEXT_MAX_TOKENS", "180000"))

# 字符维度的上限。按【最省 token 的内容】标定：纯 ASCII/JSON 约 0.5 token/字符，
# 所以 = CONTEXT_MAX_TOKENS / 0.5。这样两个口径各自负责一种极端内容：
#   纯 JSON  → 360k 字符时两边同时触顶；
#   纯中文  → 240k 字符时 token 口径先触顶（字符只到 67%）。
# pressure() 取两者较大者，于是“哪个先接近上限就听哪个”。
CONTEXT_MAX_CHARS = int(os.getenv("CONTEXT_MAX_CHARS", str(CONTEXT_MAX_TOKENS * 2)))
# 压力（字符占比与 token 占比的较大者）超过这个比例就做【微压缩】：
# 把最老的超长工具结果换成落盘指针，零 LLM 调用。超过 1.0 才做 LLM 摘要。
CONTEXT_SOFT_RATIO = float(os.getenv("CONTEXT_SOFT_RATIO", "0.6"))
# 微压缩的目标：单条工具结果被压到多少字符（仍保留结构感知预览 + 取回路径）
MICRO_COMPACT_TARGET_CHARS = int(os.getenv("MICRO_COMPACT_TARGET_CHARS", "600"))
# 小于这个长度的工具结果不值得微压缩（换成指针后省不下多少）
MICRO_COMPACT_MIN_CHARS = int(os.getenv("MICRO_COMPACT_MIN_CHARS", "900"))

# 开发期严格模式：每次调 LLM 前校验 tool_call/tool_result 配对不变量，
# 发现「孤立 tool 消息」立刻抛错而不是等线上 400。本地开发/CI 建议开启。
HARNESS_STRICT = os.getenv("HARNESS_STRICT", "").lower() in ("1", "true", "yes")

# --- 单轮运行预算（RunContext）---
# ★ 当前目标是【让 Agent 跑到任务完成，暂不计成本】，因此预算上限已停用。
#
# 【语义】<= 0 表示不限制（should_stop 跳过该维度的检查）。
# 刻意保留整套预算机制而不删代码：以后要恢复封顶，只需把环境变量设回正数。
# 用量统计与花费估算【继续正常工作】，只是不再拿来做熔断。
#
# ⚠️ 停用后仍然存在的防跑飞手段（否则无限循环无人能停）：
#   ① 用户中断（abort）—— 永远有效，是最后的人工开关；
#   ② 单段步数上限 MAX_AGENT_STEPS；
#   ③ 续跑次数上限 RUN_MAX_CONTINUATIONS（仍为正数，见下）；
#   ④ 续跑还要求 plan 里真的有未完成步骤。
RUN_MAX_TOKENS = int(os.getenv("RUN_MAX_TOKENS", "0"))
RUN_MAX_WALL_S = float(os.getenv("RUN_MAX_WALL_S", "0"))
# 子 Agent 预算同样停用（否则子任务依旧会在 40000 tokens 处被截断，
# 整轮“跑到完成”就无从谈起）。嵌套深度限制仍在，不会无限繁殖。
SUBAGENT_MAX_TOKENS = int(os.getenv("SUBAGENT_MAX_TOKENS", "0"))
SUBAGENT_MAX_WALL_S = float(os.getenv("SUBAGENT_MAX_WALL_S", "0"))
# 子 Agent 嵌套深度上限。子 Agent 拿不到 dispatch_agent（代码过滤），
# 这是第二道护栏：即使过滤被绕过也不会无限递归。
SUBAGENT_MAX_DEPTH = int(os.getenv("SUBAGENT_MAX_DEPTH", "2"))

# --- 顶层调度方式 ---
# "model"（默认）：不做意图分类，用户请求直接交给编排 Agent，
#            由模型自己决定调哪些工具、派几个子 Agent、以及并行还是串行。
# "intent"：旧路径 —— 先把请求对齐到 8 个预制意图，再按硬映射选一个 Agent，
#            full_checkup 走写死的三阶段串行。
#
# 为何改：预制意图覆盖不了真实请求的分布（"先看拓扑再对比两个库的水位"
# 归不到任何一个意图），而即使分对了，"派几个 Agent、串行还是并行" 也是人写死的。
# 对齐 Claude Code 的做法：它根本没有意图分类层，用户输入 + 全部工具 + 提示词
# 直接交给主模型编排。
#
# 保留 intent 模式不是为了兼容好看：两种调度的准确率/耗时/成本要能用同一套
# 评测**实测对比**，否则"模型自主编排更好"只是个没数据支撑的断言。
AGENT_ROUTING = os.getenv("AGENT_ROUTING", "model").strip().lower()
# 编排 Agent 的定义名（agents/defs/ 下的文件名）
ORCHESTRATOR_AGENT = os.getenv("ORCHESTRATOR_AGENT", "orchestrator").strip()


def routing_is_model() -> bool:
    return AGENT_ROUTING != "intent"


# --- Harness 开关：用于对照评测的「朴素基线」---
#
# full （默认）= 完整 Harness； naive = 朴素 LLM Agent 基线。
#
# 为何需要它：评测只能算出“我们准确率 100%”，却说不出“比什么提升了多少”——
# 而后者才是读者真正关心的。对照组选“同一个模型 + 同一批工具，但没有 Harness”，
# 因为那正是别人不用本项目时的替代方案（自己接个大模型 + 几个工具）。
#
# 【公平性边界】—— 这条线决定了对比结果可不可信，必须写死在这里：
#   保留（两臂一致，不是变量）：同一个模型、同一批业务工具、同样的步数上限、
#                              同样的集群环境描述（有哪些服务/库/缓存）。
#   剥离（即被测的自变量）：排障方法论（工作准则 + Skill）、事实核对、并行取数、
#                              子 Agent、任务清单与分段续跑、长期记忆。
#
# 为何环境描述要保留：任何人裸接工具都会告诉模型“你管的集群长什么样”，
# 不给就连工具参数都填不对，那是在造假对手而不是评测。而工作准则（如“空结果本身
# 就是结论”“规则只是起点”）是本项目踩坑积累的——那才是 Harness 的价值本体。
HARNESS_PROFILE = os.getenv("HARNESS_PROFILE", "full").strip().lower()


def harness_is_naive() -> bool:
    return HARNESS_PROFILE == "naive"

# --- 日志查询默认时间窗口 ---
# query_logs 默认只看最近这么久。为什么必须有默认窗口：
# 排障关心的是"现在怎么了"，而库里长期积累着历史数据。
# 没有窗口时工具会把两小时前演练的日志当成当前状况返回，
# Agent 据此给出的结论完全错误 —— 且引用的每条日志都真实存在，
# 事实核对查不出来（数据确实在库里，只是时间不对）。
# 显式传 minutes=0 可取消限制（导出/审计场景）。
LOG_QUERY_DEFAULT_MINUTES = int(os.getenv("LOG_QUERY_DEFAULT_MINUTES", "30"))

# --- 模型单价（仅用于展示估算花费，不参与任何熔断）---
# 元 / 1000 tokens。做成可配是因为单价会变，而之前硬编码在 usage_event() 里。
PRICE_IN_PER_1K = float(os.getenv("PRICE_IN_PER_1K", "0.02"))
PRICE_OUT_PER_1K = float(os.getenv("PRICE_OUT_PER_1K", "0.06"))

# --- 分段续跑（E-2）---
# MAX_AGENT_STEPS 是【单段】的步数上限；步数耗尽且任务清单里还有未完成步骤时，
# 允许生成交接摘要后再开一段。最多 1 + RUN_MAX_CONTINUATIONS 段。
#
# 原本为 2（最多 3 段）。预算停用后，这里反而成为“跑到任务完成”的真正卡点，
# 所以放宽到 50 段（配合 30 步/段，约 1500 步，远超实际任务所需）。
#
# 为何不索性设成无限：续跑的判据是“plan 里还有非 done 的步骤”，
# 而这份 plan 由模型自己维护 —— 模型若始终不把步骤标成 done，无限续跑就成了
# 真正的死循环，而此时预算熔断已经不在了。保留一个大但有限的值，
# 相当于把“不能让被约束的对象自己解除约束”这条原则留下最后一道兜底。
RUN_MAX_CONTINUATIONS = int(os.getenv("RUN_MAX_CONTINUATIONS", "50"))

# --- 结论事实核对（E-4）---
# 回答里的 traceID / 资源名必须能在"模型看到过的内容"里找到出处。
# 只对【没见过的 hex 标识符】与【与真实资源仅差 1~2 个字符的名字】报警，
# 数值对不上只作为附带信息 —— 单位换算与四则运算本来就该做，硬报警必然制造噪声。
VERIFY_ANSWER = os.getenv("VERIFY_ANSWER", "1").lower() in ("1", "true", "yes")


def verify_enabled() -> bool:
    """运行时判定：naive 基线臂强制关闭事实核对（它本身就是被测能力）。

    【为何必须是函数而不是模块级常量】评测脚本是在**同一进程内**改
    `config.HARNESS_PROFILE` 来跑双臂的（同 eval/run_l2.py 切 AGENT_ROUTING 的做法）。
    若写成模块级 `if harness_is_naive(): VERIFY_ANSWER = False`，它只在导入时求值一次，
    运行时切到 naive 后核对依旧开着 —— 基线臂偷偷带着 Harness 能力跑，
    测出的“提升”就是假的，而且不会报错、完全静默。
    """
    return VERIFY_ANSWER and not harness_is_naive()
# 命中后最多追加几次自纠正往返。设 0 则只报警不修正。
# 质量优先放宽到 2：带工具查回真实 ID 后可能仍有残留引用问题，
# 只给一次机会等于"查到一半就收工"。
VERIFY_MAX_CORRECTIONS = int(os.getenv("VERIFY_MAX_CORRECTIONS", "2"))

# A1（编造 traceID）自纠正时允许的取数步数。
# 为什么 A 类也要给工具：不给的话模型唯一出路是删掉那条证据 ——
# 结论还在但佐证没了，运维报告里这是实打实的质量损失。
VERIFY_CORRECT_MAX_STEPS = int(os.getenv("VERIFY_CORRECT_MAX_STEPS", "8"))

# --- C 类（数据时效）自动刷新复核 ---
#
# 【为什么要有它】原实现对 C 类只告警不处理，理由是"数据过期靠模型重写解决不了"。
# 这个判断对，但结论下错了：重写没用，**那就重新取数**。只挂一条
# "我的结论基于 31 分钟前的数据"然后等用户来问"要不要用最新数据再确认一次"，
# 等于把该系统自己做的事推给用户 —— 而用户很可能没注意到那条警示。
#
# 【与 A 类自纠正的本质区别】A 类（编造 ID/资源名）只需改写文字，所以
# _self_correct 刻意不给工具；C 类必须**真的再查一次**，因此单开一条允许
# 带工具的复核路径。两者的成本量级不同，上限也分开配。
VERIFY_REFRESH_STALE = os.getenv("VERIFY_REFRESH_STALE", "1").lower() in ("1", "true", "yes")
# 一次运行内最多触发几轮数据刷新复核。
# 为何必须有上限：若数据源本身停止更新（采集器挂了），重新取数拿回来的还是旧数据，
# 会再次命中 C 类 —— 没有上限就是死循环，且每轮都在烧钱。
VERIFY_MAX_REFRESH = int(os.getenv("VERIFY_MAX_REFRESH", "2"))
# 复核时允许模型最多再调几次工具。质量优先给到 15：复核往往要同时重取
# 多个过期数据源、再逐项比对新旧结论，步数太小会让它"查了一半就下结论"。
VERIFY_REFRESH_MAX_STEPS = int(os.getenv("VERIFY_REFRESH_MAX_STEPS", "15"))

# --- LLM 重试与自愈 ---
LLM_MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))     # 单次调用的重试上限
LLM_RETRY_BASE_S = float(os.getenv("LLM_RETRY_BASE_S", "1"))   # 指数退避基数
# 单次 LLM 请求的超时（秒）。必须显式设：SDK 默认 600s + 默认重试 2 次，
# 与外层 LLM_MAX_ATTEMPTS 叠加后一次调用最坏能阻塞近 1.5 小时，
# 而阻塞期间中断信号完全无法生效（实测卡过 21 分钟、日志零输出）。
# 180s 的依据：主模型生成上千 token 的长回答实测在数十秒量级，
# 留三分钟既不误杀正常长生成，又能把挂死的请求在可接受时间内拉回重试/中断检查点。
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "180"))
# 连续失败达到此数后降级到快模型（LLM_MODEL_FAST）
LLM_DOWNGRADE_AFTER = int(os.getenv("LLM_DOWNGRADE_AFTER", "3"))

# --- 权限门禁（P0-1）---
# 默认运行模式：readonly（只读巡检）/ confirm（默认，破坏性动作需确认）/ auto（无人值守）
DEFAULT_PERMISSION_MODE = os.getenv("DEFAULT_PERMISSION_MODE", "confirm")
# 等待用户确认的超时（秒）。超时按【拒绝】处理（fail-closed）。
APPROVAL_TIMEOUT_S = float(os.getenv("APPROVAL_TIMEOUT_S", "300"))
# 距超时还剩多少秒时推一次「即将超时」提醒
APPROVAL_WARN_BEFORE_S = float(os.getenv("APPROVAL_WARN_BEFORE_S", "60"))
# 注：曾有 PERMISSION_AUTO_APPROVE 逃生阀（所有 ask 自动批准），用于前端确认卡片
# 尚未实现期间跑通演示。前端已接入真实确认卡片后移除 —— 留着等于给"绕过人工确认"
# 开了一个只需改环境变量就能打开的后门。确认交互唯一入口现为 POST /api/chat/approve。

# --- 工具执行（P0-4 / P1-1）---
# 连续的只读且标记 concurrency_safe 的工具并发执行的最大线程数。
# 只读工具对 SQLite 是并发读（安全）；写上下文与写审计仍在串行阶段完成。
MAX_PARALLEL_TOOLS = int(os.getenv("MAX_PARALLEL_TOOLS", "4"))

# --- 动态 Mock 闭环（spec v1.1 契约 D）---
# static（默认）: 现有静态数据 demo 行为完全不变；live: 启用定时扫描，
# 数据由 data_collector 持续采集，规则/拓扑切换为滑动时间窗语义。
DATA_SOURCE = os.getenv("DATA_SOURCE", "static").lower()
MOCK_SERVER_URL = os.getenv("MOCK_SERVER_URL", "http://localhost:9001")
RISK_SCAN_INTERVAL_S = int(os.getenv("RISK_SCAN_INTERVAL_S", "60"))
RULE_WINDOW_MINUTES = int(os.getenv("RULE_WINDOW_MINUTES", "5"))

# --- 自主预诊断（P2-7）---
# 定时扫描发现【新增 P1】时自动派 diagnose 子 Agent 预分析根因。
# 默认开启（仅 live 模式下定时扫描才会运行）。关掉可省 token。
PREDIAGNOSIS_ENABLED = os.getenv("PREDIAGNOSIS_ENABLED", "1").lower() in ("1", "true", "yes")
# 单轮扫描最多发起几次预诊断 —— 每次是一个完整子 Agent（可达 40k token），
# 一次扫出多个新 P1 时必须限流，否则 token 会被烧穿。
PREDIAG_MAX_PER_SCAN = int(os.getenv("PREDIAG_MAX_PER_SCAN", "2"))

# --- 运行产物保留期（harness/retention.py）---
# 这三类产物只增不减，必须有保留期。审计留得最久：它是合规凭证不是缓存。
TOOL_RESULT_RETENTION_HOURS = float(os.getenv("TOOL_RESULT_RETENTION_HOURS", "24"))
AUDIT_RETENTION_DAYS = float(os.getenv("AUDIT_RETENTION_DAYS", "30"))
PREDIAG_RETENTION_DAYS = float(os.getenv("PREDIAG_RETENTION_DAYS", "7"))
# 清理间隔（秒）。0 表示只在启动时清一次。
RETENTION_INTERVAL_S = int(os.getenv("RETENTION_INTERVAL_S", "3600"))

# live 模式下 realtime_metrics 超过这个秒数没有新数据，就认为采集已断流。
# 默认 REALTIME_INTERVAL_S(10s) 的 6 倍，容忍偶发抖动。
FRESHNESS_STALE_AFTER_S = int(os.getenv("FRESHNESS_STALE_AFTER_S", "60"))


def is_live() -> bool:
    return DATA_SOURCE == "live"
