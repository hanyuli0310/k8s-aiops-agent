"""子 Agent 定义：从 `agents/defs/*.md` 加载（P2-2）。

## 为什么外移

原来 `AGENT_SPECS` 是 Python dict，加一个专家 Agent 必须改代码。外移成 markdown 后
非程序员也能加，且与 Skill 用同一套 frontmatter 约定（对齐 Claude Code 的字段命名，
见 CC/src/skills/loadSkillsDir.ts:239-256）。

## 定义格式

    ---
    name: RiskAgent                  # 展示名
    description: 风险治理专家……        # 职责，会进系统提示词
    when_to_use: 需要执行治理动作时……   # 供路由/派发判断
    model: primary | fast            # 语义档位，不写死模型名
    skill: risk_governance           # 主 Skill（全文注入）
    allowed-tools: run_risk_scan, …  # 工具白名单
    dispatchable: true               # 是否可被 dispatch_agent 派发
    ---

    （正文＝该 Agent 的补充行为约束，会拼在 role 之后）

`model` 用 `primary`/`fast` 语义值而非具体模型名：换模型只改 config，
不用动 7 个定义文件。

## 提示词组装顺序（volatile 收尾，P2-3）

    role + 集群概况 + 工作准则 + Agent 正文 + 主 Skill 全文 + Skill 目录 + live_note
    └────────────────── 同一 Agent 内固定不变 ──────────────────┘   └ volatile ┘
    memory                                                          ← 每次都变，必须最后
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .. import config
from ..harness import memory, skills

logger = logging.getLogger(__name__)

DEFS_DIR = Path(__file__).resolve().parent / "defs"

_BASE_PROMPT = """你是「全链路智能运维 Agent」的 {role}，负责阿里云上的生产 K8s 集群 prod-cluster-01（cn-hangzhou）。

集群概况：6 节点跨 3 可用区；8 个服务，调用链
nginx-ingress → web-frontend → api-gateway → 后端五个服务
（user-service、product-service、order-service、payment-service、inventory-service）；
数据库 rds-mysql-order 与 rds-mysql-core（均 MySQL 8.0）、缓存 kvstore-redis-01（Redis 7.0）。
可观测数据（CMS 指标 60 分钟、SLS 日志/Trace 30 分钟）已采集入库，通过工具查询。

工作准则：
1. 一切结论必须来自工具返回的真实数据，禁止编造数值、资源名、traceID；
2. 【输出要短】回答用中文 Markdown、关键数值加粗，但**结论优先、越短越好**：
   · 思考文字一句话说清"接下来查什么、为什么"，不要复述已经看到的数据；
   · 最终回答先给结论与根因，证据只列**支撑结论的那几个关键数值**；
   · 不要把工具返回的表格原样搬进回答，也不要为了"完整"罗列全部指标。
   这不是排版偏好：生成 token 是本系统最主要的耗时来源（实测每轮耗时与输出长度
   相关系数 +0.86，与输入长度只有 +0.16）—— 多写一屏字，用户就多等十几秒。
3. 【并行取数】若本步需要多份互不依赖的只读数据（如指标 + 日志 + 拓扑 + 风险报告），
   请在同一次回复里一次性发起这多个查询，它们会被并发执行，比逐个来快得多；
   只有当后一步的参数依赖前一步的结果时，才分步调用；
4. 治理/变更类动作必须单独发起、逐个确认，不要和其他调用混在同一次回复里；
5. 执行治理/变更类动作前必须得到用户明确确认（本轮用户输入里明确同意才算）；
6. 【超长结果】若工具结果过大，你会收到「前几条样本 + 各字段总数」的预览和一个
   <persisted-output> 落盘路径。此时不要以为数据只有样本这几条：先看 _total 判断规模，
   再用 sql_query 做聚合/过滤精查（数据都在库里）；只有确需原始文本时才用
   read_tool_result 按 offset 分页取回。
7. 【任务清单】若手里有 update_plan 工具且任务需要三步以上，**先列清单再动手**，
   每完成一步立刻更新状态。这不是形式：工具调用步数是有上限的，
   系统靠这份清单判断任务是否还没做完，从而自动开新一段接着执行；
   没有清单时步数一用尽就只能中断并把问题交回用户。
8. 【空结果本身就是结论】查询返回 0 行是**有效信息**，不是失败。
   确认某类数据在时间窗内不存在后，**如实汇报"窗口内无此类数据"并结束**，
   不要反复换写法试探同一件事 —— 那样只会烧完预算却什么也没查到。
   典型例子：稳态下 slow_logs 本来就是空的（慢查询只在故障态产生）。
9. 【规则只是起点，不是边界】内置风险规则是人预先写好的、数量有限，
   只能覆盖已经被想到过的故障模式。因此：
   · **规则没报不等于正常。**只要用户反映有问题，就直接看数据：
     指标是否突变、日志是否出现新错误、调用链哪一段耗时最多；
   · 排查范围不要被风险报告里出现过的组件限住 —— 缓存、消息队列、
     外部依赖、客户端重试这些可能根本没有对应规则，但照样会出故障；
   · 对无规则覆盖的异常，用通用三步自己判：哪些指标偏离平时水位、
     异常开始时间与哪些事件对得上、在调用链上谁在上游（上游更可能是根因）；
   · 结论落在规则覆盖范围之外时，**明确说明这一点**（“此项无内置规则覆盖，
     以下是从指标/日志直接得出的判断”）；若这类异常值得长期监控，
     可建议用 create_risk_rule 把它沉淀成新规则（阈值靠 sql_query 探数据分布定）。
{extra}{skill}{catalog}{live}{memory}"""

# model 语义档位 → 实际模型。换模型只改 config，不动定义文件。
_MODEL_TIERS = {"fast": "LLM_MODEL_FAST", "primary": "LLM_MODEL"}

# --- 朴素基线的提示词（HARNESS_PROFILE=naive）---
#
# 这是对照评测的基线臂，代表「别人不用本项目时的替代方案」：拿一个大模型、
# 接上同一批运维工具，直接问。所以它只有两样东西：角色 + 集群环境描述。
#
# 与 _BASE_PROMPT 的差集就是被测的自变量：那 9 条工作准则（防幻觉、并行取数、
# 输出要短、超长结果分页、任务清单、空结果是结论、规则只是起点……）全部拿掉，
# Skill 方法论、Skill 目录、长期记忆也全部不注入。
#
# 集群概况刻意与 _BASE_PROMPT 逐字一致：它是环境事实而非方法论，
# 两臂之间任何环境信息的差异都会污染对比（模型连库名都不知道就没法公平比）。
_NAIVE_PROMPT = """你是一个运维助手，负责阿里云上的生产 K8s 集群 prod-cluster-01（cn-hangzhou）。

集群概况：6 节点跨 3 可用区；8 个服务，调用链
nginx-ingress → web-frontend → api-gateway → 后端五个服务
（user-service、product-service、order-service、payment-service、inventory-service）；
数据库 rds-mysql-order 与 rds-mysql-core（均 MySQL 8.0）、缓存 kvstore-redis-01（Redis 7.0）。
可观测数据（CMS 指标 60 分钟、SLS 日志/Trace 30 分钟）已采集入库，通过工具查询。

请用中文回答。"""

# naive 臂要摘掉的工具：它们本身就是 Harness 能力，不是业务工具。
#   dispatch_agent —— 子 Agent 与上下文隔离
#   load_skill     —— 渐进式披露的方法论检索
#   update_plan    —— 任务清单，且分段续跑靠它判定
#   read_tool_result —— 超长结果分页回取（与落盘机制配套）
_HARNESS_ONLY_TOOLS = {"dispatch_agent", "load_skill", "update_plan", "read_tool_result"}


def _split_tools(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).replace("，", ",").split(",") if x.strip()]


def _resolve_model(tier: str) -> str:
    """把 primary/fast 解析成真实模型名；也允许直接写模型名（兼容用）。"""
    attr = _MODEL_TIERS.get(str(tier or "").strip().lower())
    if attr:
        return getattr(config, attr)
    if tier:
        return str(tier)                        # 直接写了模型名
    return config.LLM_MODEL


def _load_defs() -> dict:
    """扫描 defs/*.md，组装成与旧 AGENT_SPECS 同结构的 dict。"""
    specs = {}
    if not DEFS_DIR.is_dir():
        logger.error("Agent 定义目录不存在: %s", DEFS_DIR)
        return specs

    for path in sorted(DEFS_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("读取 Agent 定义失败 %s: %s", path, e)
            continue

        meta, body = {}, text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                try:
                    meta = yaml.safe_load(text[3:end]) or {}
                    body = text[end + 4:]
                except yaml.YAMLError as e:
                    logger.warning("Agent 定义 frontmatter 解析失败 %s: %s", path, e)
        if not isinstance(meta, dict):
            meta = {}

        key = path.stem
        specs[key] = {
            "name": str(meta.get("name") or key),
            "role": str(meta.get("description") or "").strip(),
            "when_to_use": str(meta.get("when_to_use") or "").strip(),
            "skill": (str(meta.get("skill")).strip() if meta.get("skill") else None),
            "tools": _split_tools(meta.get("allowed-tools") or meta.get("allowed_tools")),
            "model_tier": str(meta.get("model") or "primary"),
            "model": _resolve_model(meta.get("model")),
            # 能否被 dispatch_agent 派发。默认 False（fail-closed）：新增一个定义文件
            # 不该自动出现在 LLM 可派发的枚举里 —— 适不适合当子 Agent 要显式声明。
            "dispatchable": bool(meta.get("dispatchable", False)),
            "extra": body.strip(),
        }
    logger.info("加载 %d 个 Agent 定义: %s", len(specs), sorted(specs))
    return specs


# 模块级加载。保留 AGENT_SPECS 这个名字与结构，既有调用方（dispatch_agent /
# scheduler / 测试）不需要任何改动。
AGENT_SPECS = _load_defs()


def reload_defs() -> dict:
    """重新扫描定义文件（改完 md 不必重启进程）。"""
    global AGENT_SPECS
    AGENT_SPECS = _load_defs()
    skills.discover(force=True)
    # dispatch_agent 的枚举与描述由定义文件生成，热加载后必须一起刷新，
    # 否则新增的专家 Agent 存在却派不出去（曾经真的漏过 capacity / dbops）。
    try:
        from ..tools import agent_tools
        agent_tools.refresh_subagent_types()
    except Exception:                              # noqa: BLE001
        logger.debug("刷新 dispatch_agent 枚举失败", exc_info=True)
    return AGENT_SPECS


def dispatchable_keys() -> list:
    """可被 dispatch_agent 派发的 Agent key —— 唯一权威来源。"""
    return sorted(k for k, v in AGENT_SPECS.items() if v.get("dispatchable"))


def effective_tools(key: str) -> list:
    """该 Agent 运行时【真正拿到】的工具集。

    与 spec 里声明的 allowed-tools 有两处差异，所以必须单独算：
      · live 模式摘掉 ingest_data（它内部 DELETE 七张观测表，会清掉 collector 的数据）
      · 补上 load_skill（按需加载 Skill 的前提）
    build_agent 与 describe（前端资产面板）都走这里，避免两处各算一遍而说法不一致。
    """
    tools = list(AGENT_SPECS[key]["tools"])
    if config.is_live() and "ingest_data" in tools:
        tools.remove("ingest_data")
    if "load_skill" not in tools:
        tools.append("load_skill")
    return tools


def build_agent(key: str, user_query: str = "") -> dict:
    """按需组装 Agent（每次组装时注入最新长期记忆）。

    提示词拼接顺序是刻意的（P2-3）：稳定内容全部在前，每次都变的 memory 放最后。
    prompt cache 命中的是最长公共前缀，volatile 内容插在中间会让它后面的全部失效。

    Skill 分两层注入（P2-1）：
      · 主 Skill 全文 —— 它是这个 Agent 每轮都要用的核心方法论，若要求先调
        load_skill 才能拿到，等于给每个请求多加一轮 LLM 往返；
      · 其余 Skill 只给目录（name/description/when_to_use），模型按需 load。
    """
    spec = AGENT_SPECS[key]
    tools = effective_tools(key)

    # 朴素基线臂：只给角色 + 环境描述，摘掉全部方法论与 Harness 专有工具。
    # 业务工具一个不减 —— 否则测到的是“工具少了所以差”而不是“没有 Harness 所以差”。
    if config.harness_is_naive():
        return {
            "name": spec["name"],
            "system_prompt": _NAIVE_PROMPT,
            "tools": [t for t in tools if t not in _HARNESS_ONLY_TOOLS],
            "model": spec.get("model") or config.LLM_MODEL,
        }

    live_note = ("\n\n[当前为 live 模式：集群数据由采集器每 10~60s 持续更新，"
                 "结论请标注数据时间窗；无需也无法手动重新采集]") if config.is_live() else ""
    main_skill = spec.get("skill")
    extra = f"\n\n[本 Agent 的补充约束]\n{spec['extra']}" if spec.get("extra") else ""

    return {
        "name": spec["name"],
        "system_prompt": _BASE_PROMPT.format(
            role=spec["role"],
            extra=extra,
            skill=skills.body_prompt(main_skill) if main_skill else "",
            catalog=skills.catalog_prompt(exclude=main_skill),
            live=live_note,
            memory=memory.memory_prompt(user_query)),   # ← volatile，必须最后
        "tools": tools,
        # 分模型路由（P1-3）：轻结构化任务走快模型，决策/推理走主模型
        "model": spec.get("model") or config.LLM_MODEL,
    }


def describe() -> list:
    """供 /api/status 暴露：有哪些 Agent、各自用什么模型与 Skill。

    tools 给的是【运行时真正生效】的集合（effective_tools），不是 md 里的声明 ——
    前端资产面板显示的必须是模型手里实际有的工具，否则会误导排查。
    """
    return [
        {"key": k, "name": v["name"], "description": v["role"],
         "when_to_use": v["when_to_use"], "model": v["model"],
         "model_tier": v["model_tier"], "skill": v["skill"],
         "dispatchable": v["dispatchable"], "tools": sorted(effective_tools(k))}
        for k, v in sorted(AGENT_SPECS.items())
    ]
