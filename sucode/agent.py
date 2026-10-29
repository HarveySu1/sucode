"""Agent 编排器（ch04: Agent Loop——对齐 mewcode 生成器架构重构）。

对外接口（spec(docs/ch04) C55）：`async for event in agent.run(text)` 异步生成器，
事件为 AgentEvent 家族（16 个事件类，其中 ToolCallStart/ToolCallDelta 复用 providers
定义）；ch03 的 asyncio.Queue 通道退役（C42/C43 修订），LoopComplete 为自然终止信号。

主循环（ReAct）：
- 一轮 = StreamCollector 边收边转发 provider 流（C56）→ 无工具调用则 assistant 入史
  并 LoopComplete(completed) 收束；有工具调用则 partition_tool_calls 切批执行、结果
  统一配对入史后 TurnComplete，进入下一轮
- 循环健壮性（C57/C58/C59）：
  * finish_reason=max_tokens/length → 首次升档 max_tokens 至 64000 + assistant 半截
    入史 + 注入续写指令 + RetryEvent，最多 3 次恢复轮，超出走主循环
  * 连续 3 轮仅请求未知工具 → ErrorEvent + LoopComplete(error) 熔断（幻觉死循环）
  * 每轮 UsageEvent 上报 token 用量（本轮 + 会话累计；provider 未报则不产出）
- 取消双路径（C48 修订/N29）：取消信号在轮间检查点命中 → LoopComplete(cancelled)；
  消费者取消任务（Esc / worker.cancel）→ CancelledError 沿生成器传播，配对收尾在
  except 内完成——except 内不可 yield，硬取消不产出终止事件，消费者以异常感知
- 配对不变量延续（N18）：任何路径零孤儿；工具阶段被中断时未完成调用统一配对
  「[已中断]」结果后上抛；半截正文带标记入史（C29）；升档恢复轮被中断时撤回
  续写指令、半截并回上一条 assistant（保持 user/assistant 交替）
- 记忆提取延续（C52）：全部终止点计数（含硬取消），每 5 次 fire-and-forget 提取

工具执行（C44 延续 + C60/C61）：
- 单工具 _execute_tool 为异步生成器：plan 拦截（内置第一道，C47）→ pre_tool_use
  hook（可阻断，C61）→ 权限确认（PermissionRequest + PermissionResponse 三值，C51
  延续）→ registry.run 统一执行（增量输出/心跳转事件）→ _maybe_persist_or_truncate
  （超 8000 字符持久化到 session 目录回灌预览，C60）→ post_tool_use hook
- HookEngine 八挂点（C61）：session_start/turn_start/pre_send/post_receive/
  pre_tool_use/post_tool_use/turn_end/session_end；默认空引擎零行为变化
- 并发批经合流队列转发事件（N25）：多工具增量输出/心跳按 id 交错、信号量限流（N16）

延续 ch02/ch03 既有约束：MAX_TOOL_ROUNDS=10（C28）、thinking/signature 仅带工具的
会话入史（C25）、每轮流各自 aclose（C33）、系统提示词不进历史（N22）、deferred
名单拼装（C53）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from sucode.chat import ChatSession
from sucode.hooks import HookContext, HookEngine, HookEventName, infer_file_path
from sucode.memory import EXTRACT_EVERY_N_RUNS, extract_memory
from sucode.providers.base import (
    Message,
    ProviderError,
    SignatureDelta,
    StreamDone,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallStart,
    ToolResultBlock,
    UsageUpdate,
)
from sucode.tools import ToolRegistry
from sucode.tools.bash_tool import is_readonly_command
from sucode.tools.base import (
    MAX_OUTPUT_CHARS,
    Tool,
    ToolError,
    ToolResult,
    parse_arguments,
    truncate_output,
)

INTERRUPTED_MARKER = "\n\n[已中断]"
INTERRUPTED_TOOL_RESULT = "[已中断] 用户取消了本次工具执行"
MAX_TOOL_ROUNDS = 10  # spec(docs/ch02) C28 硬上限：工具执行轮数上限，到顶强制收尾
HEARTBEAT_SECONDS = 2.0  # spec(docs/ch02) C31/N12 心跳间隔
CONCURRENCY_LIMIT = 4  # spec(docs/ch03) N16：安全批内并发上限（checklist 承接值）

# 循环健壮性（spec(docs/ch04) C57/C58/C60，checklist 承接值）
MAX_TOKENS_CEILING = 64000  # max_tokens 截断时的一次性升档上限
MAX_OUTPUT_TOKENS_RECOVERIES = 3  # 升档后的恢复轮次数上限
RESUME_INSTRUCTION = "继续完成刚才未完成的输出，从中断处接着写，不要重复已有内容。"
UNKNOWN_TOOL_STREAK_LIMIT = 3  # 连续「整轮全未知工具」熔断阈值
SINGLE_RESULT_CHAR_LIMIT = 8000  # 工具结果超此长持久化落盘、回灌预览
PERSIST_PREVIEW_EDGE = 500  # 持久化预览的首尾保留字符数

# 系统提示词（spec(docs/ch03) C50，文案为 checklist 承接值；不进对话历史，N22 零污染）
NORMAL_SYSTEM_PROMPT = (
    "你是 sucode，一个运行在终端里的 AI 编程助手。优先借助工具获取真实信息再回答；回答用中文，保持简洁。"
    "不要在回复正文里输出思考或推理过程（如自我权衡、计划独白），正文只写面向用户的结论与必要说明。"
)
PLAN_SYSTEM_PROMPT_TEMPLATE = """\
Plan mode is active. 你不能执行任何修改操作，不能编辑文件、不能提交代码、不能修改配置。
唯一可以写入的文件是下面指定的 plan file：{plan_file_path}

你的工作流程：
1. 用 ReadFile、Grep、Glob、Bash（只读命令）探索代码
2. 分析用户需求，设计实现方案
3. 把计划写入 plan file
4. 等待用户确认后再执行
"""
# 拦截提示文案（is_error 结构化结果回传模型，checklist 承接值）
PLAN_WRITE_INTERCEPTED = (
    "计划模式：写操作已拦截（仅允许写入计划文件 {plan_file_path}）。"
    "请继续用只读工具探索，或把计划写入计划文件。"
)
PLAN_COMMAND_INTERCEPTED = (
    "计划模式：该命令不在只读白名单内，已拦截。请改用只读命令（如 ls、cat、git status/diff/log）。"
)
PLAN_DONE_NOTICE = "计划已写入 {plan_file_path}，确认后可用 /exec 退出计划模式并执行"
# /exec 退出后的首次请求拼接提示（C47 修订，checklist 承接值）：/exec 是 UI 命令不进历史，
# 模型凭旧上下文会误判仍在计划模式而拒绝写操作；一次生效后自动消失（不入独立历史消息）
PLAN_EXIT_NOTICE = "[模式切换] 已退出计划模式，写操作已恢复，可直接执行修改类任务。"

# 权限确认（spec(docs/ch03) C51 修订，checklist 承接值）
PERMISSION_TIMEOUT = 60.0  # 等待应答超时，超时按拒绝
PERMISSION_DENIED = "用户拒绝了本次工具执行，未运行。"  # deny 的结构化回传文案

# ---- 事件家族（spec(docs/ch04) C55，对齐 mewcode T1） ----

# 终止原因枚举（ch03 延续）：正常完成 / 用户中断 / 取消信号 / 错误 / 达轮数上限。
# interrupted 仅保留枚举值：生成器契约下硬取消经 CancelledError 传播、不产出事件（N29）
FinishReason = Literal["completed", "interrupted", "cancelled", "error", "max_rounds"]


class PermissionResponse(Enum):
    """权限应答三值（spec(docs/ch03) C51 延续，ch04 枚举化，对齐 mewcode T1）。"""

    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALWAYS = "always"


@dataclass
class ThinkingText:
    """思考文本增量（源 ThinkingDelta，经 StreamCollector 映射）。"""

    text: str


@dataclass
class StreamText:
    """正文增量（源 TextDelta，经 StreamCollector 映射）。"""

    text: str


# ToolCallStart / ToolCallDelta 复用 providers/base.py 的定义（参数流式透传，
# spec(docs/ch02) C32 的 UX 保留），并同时并入下方 AgentEvent 联合


@dataclass
class ToolUseEvent:
    """一次完整工具调用（流尾参数聚合后产出；供 headless 消费者拿全量调用，TUI 不渲染）。"""

    id: str
    name: str
    arguments: str = ""


@dataclass
class ToolOutputDelta:
    """工具执行中的增量输出片段（spec(docs/ch02) C30，当前仅 Bash 产出）。"""

    id: str = ""
    text: str = ""


@dataclass
class ToolHeartbeat:
    """工具执行心跳（spec(docs/ch02) C31）：所有工具执行期间周期性产出，含已用秒数。"""

    id: str = ""
    name: str = ""
    elapsed: float = 0.0


@dataclass
class ToolResultEvent:
    """单个工具执行完成（原 ToolExecuted 改名，对齐 mewcode T1）：TUI 更新 ✓/✗ 状态行。"""

    id: str = ""
    name: str = ""
    ok: bool = False
    preview: str = ""


@dataclass
class Notice:
    """会话层提示（如已达工具轮数上限、计划收尾），TUI 以暗淡行展示。"""

    text: str


@dataclass
class PermissionRequest:
    """权限确认请求（spec(docs/ch03) C51 延续，ch04 迁入 agent.py）。

    风险操作（写类 / 白名单外命令）执行前由编排器发出；future 为同进程应答载体——
    消费者调 Agent.resolve_permission(request_id, PermissionResponse) 置结果（N23）。
    """

    request_id: str
    tool_name: str
    summary: str = ""
    future: asyncio.Future[PermissionResponse] | None = None


@dataclass
class RetryEvent:
    """恢复类事件（spec(docs/ch04) C57）：max_tokens 升档/恢复轮提示。"""

    reason: str


@dataclass
class UsageEvent:
    """token 用量上报（spec(docs/ch04) C59）：本轮值 + 会话累计。"""

    input_tokens: int
    output_tokens: int
    total_input: int
    total_output: int


@dataclass
class HookEvent:
    """hook 通知上浮（spec(docs/ch04) C61）：钩子经 engine.notify 排队的界面消息。"""

    event: HookEventName
    message: str


@dataclass
class TurnComplete:
    """一次迭代（含工具批执行与结果回灌）完成，对齐 mewcode T1。"""

    round: int


@dataclass
class LoopComplete:
    """编排自然终止事件（生成器内唯一自然结束信号，C55）；硬取消不产出（N29）。"""

    reason: FinishReason = "completed"


@dataclass
class ErrorEvent:
    """编排错误事件（原 RunError 改名，对齐 mewcode T1）；后随 LoopComplete(reason="error")。"""

    message: str


AgentEvent = (
    ThinkingText
    | StreamText
    | ToolCallStart
    | ToolCallDelta
    | ToolUseEvent
    | ToolOutputDelta
    | ToolHeartbeat
    | ToolResultEvent
    | Notice
    | PermissionRequest
    | RetryEvent
    | UsageEvent
    | HookEvent
    | TurnComplete
    | LoopComplete
    | ErrorEvent
)

# ---- 轮次折叠（spec(docs/ch04) C56，对齐 mewcode T4） ----


@dataclass
class LLMResponse:
    """一轮 LLM 流的折叠结果。"""

    text: str = ""
    thinking: str = ""
    signature: str = ""  # Anthropic 思考签名：工具轮回传必需（spec(docs/ch02) C25 延续）
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class StreamCollector:
    """provider 流 → AgentEvent 流的折叠器（C56）。

    边收边转发：TextDelta/ThinkingDelta 映射为 StreamText/ThinkingText；
    ToolCallStart/ToolCallDelta 原样透传（参数流式 UX，C32 保留）；
    SignatureDelta 只累积不外发；流尾把参数碎片拼进 ToolCall 并逐个产出 ToolUseEvent。
    流的确定性关闭（aclose，C33）由本类负责。
    """

    def __init__(self) -> None:
        self.response = LLMResponse()
        self._args: dict[str, list[str]] = {}  # 工具 id → 参数碎片

    async def consume(self, stream: AsyncIterator[StreamEvent]) -> AsyncIterator[AgentEvent]:
        """消费 provider 流，折叠为 AgentEvent 产出；返回前确定性 aclose。"""
        try:
            async for ev in stream:
                if isinstance(ev, TextDelta):
                    self.response.text += ev.text
                    yield StreamText(text=ev.text)
                elif isinstance(ev, ThinkingDelta):
                    self.response.thinking += ev.text
                    yield ThinkingText(text=ev.text)
                elif isinstance(ev, SignatureDelta):
                    self.response.signature += ev.signature
                elif isinstance(ev, ToolCallStart):
                    self.response.tool_calls.append(ToolCall(id=ev.id, name=ev.name))
                    self._args[ev.id] = []
                    yield ev  # 参数流式透传（C32 保留）
                elif isinstance(ev, ToolCallDelta):
                    if ev.id in self._args:
                        self._args[ev.id].append(ev.arguments)
                    yield ev
                elif isinstance(ev, UsageUpdate):
                    self.response.input_tokens = ev.input_tokens
                    self.response.output_tokens = ev.output_tokens
                elif isinstance(ev, StreamDone):
                    self.response.finish_reason = ev.finish_reason
        finally:
            await stream.aclose()  # 确定性关闭底层流（幂等，C33）
        # 流尾：参数碎片拼接 + 完整调用事件（对齐 mewcode ToolCallComplete → ToolUseEvent）
        for tc in self.response.tool_calls:
            tc.arguments = "".join(self._args.get(tc.id, []))
        for tc in self.response.tool_calls:
            yield ToolUseEvent(id=tc.id, name=tc.name, arguments=tc.arguments)


# ---- 工具批次切分（spec(docs/ch04) 对齐 mewcode T5；ch03 C44/C45 语义延续） ----


@dataclass
class ToolBatch:
    """一次批次：concurrent=True 为安全批（可并发执行），否则串行。"""

    concurrent: bool
    calls: list[ToolCall] = field(default_factory=list)


def partition_tool_calls(
    tool_calls: list[ToolCall], registry: ToolRegistry
) -> list[ToolBatch]:
    """切批：安全（is_concurrency_safe 且已启用）且上一批也是安全批则并入同批，
    否则新开批——相邻安全调用合并、不安全各自成批、批间保持原序。

    未知/禁用工具判不安全（registry.find 为 None 或 is_enabled 为假），
    走串行路径在执行时返回结构化错误（C45 延续）。
    """
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        tool = registry.find(tc.name)
        safe = tool is not None and tool.is_concurrency_safe and registry.is_enabled(tc.name)
        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(tc)
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches


# ---- 并发收集器（spec(docs/ch04) 对齐 mewcode T6/N26） ----


@dataclass
class ExecOutcome:
    """StreamingExecutor 的收集结果：正常输出 / 结构化结果 / 异常包装。"""

    output: str = ""
    is_error: bool = False
    exception: BaseException | None = None

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ExecOutcome":
        return cls(output=f"错误：{type(exc).__name__}: {exc}", is_error=True, exception=exc)


class StreamingExecutor:
    """并发任务收集器：submit 保序、collect_results 按提交顺序收集（N26）。

    主循环不消费本类；供后续 SubAgent / Teams（ch13）在流式阶段启动工具后统一收割：
    任何 Exception 包装为错误结果不中断主流程；CancelledError 穿透（配对交上层）。
    """

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    def submit(self, coro) -> asyncio.Task:
        """提交一个协程（记序 + 后台运行）。"""
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def collect_results(self) -> list[ExecOutcome]:
        """按提交顺序收集全部结果：ToolResult → 结构化形态；str → 原样；异常 → 错误包装。"""
        outcomes: list[ExecOutcome] = []
        for task in self._tasks:
            try:
                value = await task
            except asyncio.CancelledError:
                raise  # 取消语义穿透（N18 由上层统一配对）
            except Exception as exc:  # noqa: BLE001 单任务失败不拖垮整批
                outcomes.append(ExecOutcome.from_exception(exc))
                continue
            if isinstance(value, ToolResult):
                outcomes.append(ExecOutcome(output=value.output, is_error=value.is_error))
            else:
                outcomes.append(ExecOutcome(output="" if value is None else str(value)))
        return outcomes


def _same_file_path(a: str, b: str) -> bool:
    """两个路径是否指向同一文件（resolve 归一后精确匹配；计划模式放行判定，L9）。"""
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


class Agent:
    """ReAct 编排器（ch04 生成器形态）：`async for event in agent.run(text)` 消费全程事件。"""

    def __init__(
        self,
        session: ChatSession,
        work_dir: Path | str | None = None,
        hook_engine: HookEngine | None = None,
    ) -> None:
        self._session = session
        self._work_dir = Path(work_dir) if work_dir is not None else Path.cwd()
        # HookEngine 八挂点（C61）：默认空引擎零行为变化；外部可注入自建引擎
        self._hooks = hook_engine if hook_engine is not None else HookEngine()
        # 会话目录（C60 超长工具结果持久化）：首次持久化时创建，agent_id 区分每次运行
        self._agent_id = uuid.uuid4().hex[:12]
        self._session_dir = self._work_dir / ".sucode" / "sessions" / self._agent_id
        self._session_dir_ready = False
        self._active = False  # run 重入防护（替代 ch03 的后台任务存活检查）
        self._cancel_event = asyncio.Event()  # 取消信号（C48 延续，跨运行不复用）
        # 权限确认（C51 延续）：待应答表（请求 id → Future）+ 会话级授权工具集（always 语义）
        self._pending_permissions: dict[str, asyncio.Future[PermissionResponse]] = {}
        self._permission_granted: set[str] = set()
        # 记忆提取（C52 延续）：运行计数（每 EXTRACT_EVERY_N_RUNS 次触发）+ 后台任务集
        self._run_count = 0
        self._memory_tasks: set[asyncio.Task] = set()
        # token 用量累计（C59）
        self._total_input = 0
        self._total_output = 0

    # ---- 取消信号（spec(docs/ch03) C48 延续）----

    def request_cancel(self) -> None:
        """触发取消信号：轮间检查点检测到后干净退出（温和，当前轮跑完才停）。

        与消费者取消任务（Esc 的即时路径）互补：本信号不抛异常、不打断进行中的
        流与工具，退出时历史天然配对完整，终止事件 = LoopComplete(cancelled)。
        """
        self._cancel_event.set()

    # ---- 权限确认（spec(docs/ch03) C51 延续）----

    def resolve_permission(self, request_id: str, response: PermissionResponse) -> None:
        """应答权限请求（TUI 调用）：置 Future 结果；已失效请求静默忽略（N23）。"""
        future = self._pending_permissions.get(request_id)
        if future is not None and not future.done():
            future.set_result(response)

    @staticmethod
    def _needs_permission(tool: Tool, args: dict) -> bool:
        """风险操作判定（C51 延续）：写类恒确认；命令类白名单外确认；读/task 不确认。"""
        if tool.category == "write":
            return True
        if tool.category == "command":
            return not is_readonly_command(str(args.get("command", "")))
        return False

    @staticmethod
    def _permission_summary(tc: ToolCall, args: dict) -> str:
        """确认提示用的单行参数摘要：命令类给命令、写类给路径、其余给原始参数。"""
        if "command" in args:
            return f"{tc.name}: {str(args['command'])[:100]}"
        if "path" in args:
            return f"{tc.name}: {args['path']}"
        return f"{tc.name}: {tc.arguments[:100]}"

    # ---- 系统提示词装配（C50/C53 延续）与计划拦截（C47 延续）----

    def _system_prompt(self) -> str:
        """按会话模式装配系统提示词：普通最小文案 / 计划模式专用文案 + deferred 名单。"""
        session = self._session
        if not session.plan_mode:
            prompt = NORMAL_SYSTEM_PROMPT
        else:
            prompt = PLAN_SYSTEM_PROMPT_TEMPLATE.format(plan_file_path=session.plan_file)
        return prompt + self._deferred_hint()

    def _deferred_hint(self) -> str:
        """未披露的延迟工具名单（C53）；无延迟工具时返回空串（零噪音）。"""
        registry = self._session.registry
        if registry is None:
            return ""
        names = registry.get_deferred_tool_names()
        if not names:
            return ""
        return (
            "\n\n可用 ToolSearch 检索的延迟工具："
            + "、".join(names)
            + "。需要时调用 ToolSearch 获取定义（query 传 select:名称1,名称2 或关键词检索），"
            "命中后下一轮即可直接调用。"
        )

    def _plan_intercept(self, tc: ToolCall) -> ToolResult | None:
        """计划模式内置拦截（C47/N20，ch04 仍为内置第一道、不经 HookEngine）：
        非计划文件的写类与非白名单命令一律拦截。

        放行规则（N20 例外唯一性）：写类目标 = 计划文件（按文件身份精确匹配，L9）；
        命令类命中只读白名单（C46）；读类与 task 类放行。非计划模式直接放行。
        """
        session = self._session
        if not session.plan_mode:
            return None
        registry = session.registry
        tool = registry.find(tc.name) if registry is not None else None
        try:
            args = parse_arguments(tool, tc.arguments) if tool is not None else {}
        except ToolError:
            args = {}  # 坏参数：写类按无目标拦截，命令类按非白名单拦截
        if tool is not None and tool.category == "write":
            if _same_file_path(str(args.get("path", "")), session.plan_file):
                return None  # 唯一放行的写目标（N20）
            return ToolResult(
                PLAN_WRITE_INTERCEPTED.format(plan_file_path=session.plan_file), is_error=True
            )
        if tool is not None and tool.category == "command":
            if is_readonly_command(str(args.get("command", ""))):
                return None  # 唯一放行的命令集（N20）
            return ToolResult(PLAN_COMMAND_INTERCEPTED, is_error=True)
        return None  # 读类/task 类与未知工具放行（未知工具由执行路径返回结构化错误）

    # ---- Hook 接线（C61）----

    def _hook_events(self, event: HookEventName) -> list[HookEvent]:
        """把引擎通知队列转成 HookEvent 列表（空队列零产出）。"""
        return [HookEvent(event=event, message=m) for m in self._hooks.drain_notifications()]

    async def _post_tool_hooks(
        self, tc: ToolCall, result: ToolResult
    ) -> AsyncIterator[HookEvent]:
        """post_tool_use 挂点（只观察，含否决/拒绝产生的结果）+ 通知排出。"""
        registry = self._session.registry
        tool = registry.find(tc.name) if registry is not None else None
        args: dict = {}
        if tool is not None:
            try:
                args = parse_arguments(tool, tc.arguments)
            except ToolError:
                args = {}
        await self._hooks.run_hooks(
            HookContext(
                event="post_tool_use",
                tool_name=tc.name,
                args=args,
                file_path=infer_file_path(args),
                result=result,
            )
        )
        for he in self._hook_events("post_tool_use"):
            yield he

    # ---- 主循环（spec(docs/ch04) C55-C59）----

    async def run(self, text: str) -> AsyncIterator[AgentEvent]:
        """一次编排：async for 消费全程事件；LoopComplete 为自然终止信号。

        运行中重复调用抛 RuntimeError（防两次运行的历史写入交错）；
        硬取消（消费者取消任务）经 CancelledError 传播、不产出终止事件（N29）。
        """
        if self._active:
            raise RuntimeError("上一次编排仍在进行中，须等待终止后再启动")
        self._active = True
        self._cancel_event.clear()
        session = self._session
        try:
            if session.take_plan_exit_pending():
                # /exec 后首次请求：拼接一次模式切换提示（C47 修订），随该轮 user 消息入史
                text = f"{PLAN_EXIT_NOTICE}\n{text}"
            session.append(Message(role="user", content=text))
            session.provider.reset_max_output_tokens()  # 每次运行回到 profile 配置（C57）

            await self._hooks.run_hooks(HookContext(event="session_start"))
            for he in self._hook_events("session_start"):
                yield he

            round_index = 0  # 已开始的 LLM 流轮次（从 1 起）
            tools_used = 0  # 已执行的工具轮数（上限 MAX_TOOL_ROUNDS，C28）
            unknown_streak = 0  # 连续「整轮全未知工具」轮数（C58 熔断计数）
            escalated = False  # 本次运行是否已升档过 max_tokens（C57）
            recoveries = 0  # 已消耗的恢复轮次数（C57）
            flags = {"tools_interrupted": False}  # 工具阶段是否已自行配对收尾（N18）
            current: StreamCollector | None = None
            # 思考留存规则（spec O6 修订 + C25）：仅带工具的会话才把 thinking/signature 入史
            keep_thinking = session.registry is not None

            try:
                while True:
                    if self._cancel_event.is_set():
                        # 取消信号轮间检查点（C48）：当前轮已完成且配对入史，干净退出
                        await self._hooks.run_hooks(HookContext(event="session_end"))
                        for he in self._hook_events("session_end"):
                            yield he
                        self._record_run_and_maybe_extract()
                        yield LoopComplete(reason="cancelled")
                        return
                    round_index += 1
                    await self._hooks.run_hooks(
                        HookContext(event="turn_start", round=round_index)
                    )
                    for he in self._hook_events("turn_start"):
                        yield he
                    await self._hooks.run_hooks(
                        HookContext(event="pre_send", text=text if round_index == 1 else "")
                    )
                    for he in self._hook_events("pre_send"):
                        yield he

                    # ---- 消费一轮 provider 流（C56），边收边转发 ----
                    offer_tools = session.registry is not None and tools_used < MAX_TOOL_ROUNDS
                    tools = self._tool_schemas() if offer_tools else None
                    current = StreamCollector()
                    async for ev in current.consume(
                        session.provider.stream(
                            list(session.history), tools, self._system_prompt()
                        )
                    ):
                        yield ev
                    resp = current.response

                    await self._hooks.run_hooks(
                        HookContext(event="post_receive", round=round_index)
                    )
                    for he in self._hook_events("post_receive"):
                        yield he

                    # ---- 用量上报（C59）：本轮值 + 会话累计；provider 未报则不产出 ----
                    if resp.input_tokens or resp.output_tokens:
                        self._total_input += resp.input_tokens
                        self._total_output += resp.output_tokens
                        yield UsageEvent(
                            input_tokens=resp.input_tokens,
                            output_tokens=resp.output_tokens,
                            total_input=self._total_input,
                            total_output=self._total_output,
                        )

                    # ---- max_tokens 升档恢复（C57）----
                    if (
                        resp.finish_reason in ("max_tokens", "length")
                        and recoveries < MAX_OUTPUT_TOKENS_RECOVERIES
                    ):
                        if not escalated:
                            escalated = True
                            session.provider.set_max_output_tokens(MAX_TOKENS_CEILING)
                        recoveries += 1
                        session.append(
                            self._assistant_message_from(resp, keep_thinking=keep_thinking)
                        )
                        session.append(Message(role="user", content=RESUME_INSTRUCTION))
                        yield RetryEvent(
                            reason=f"max_tokens 截断：已升档至 {MAX_TOKENS_CEILING}，"
                            f"第 {recoveries}/{MAX_OUTPUT_TOKENS_RECOVERIES} 次恢复轮"
                        )
                        continue

                    if not resp.tool_calls or session.registry is None:
                        # 最终回复（含 registry 缺失时的纯对话路径）
                        session.append(
                            self._assistant_message_from(resp, keep_thinking=keep_thinking)
                        )
                        if session.plan_mode:
                            yield Notice(
                                text=PLAN_DONE_NOTICE.format(plan_file_path=session.plan_file)
                            )
                        await self._hooks.run_hooks(HookContext(event="session_end"))
                        for he in self._hook_events("session_end"):
                            yield he
                        self._record_run_and_maybe_extract()
                        yield LoopComplete(reason="completed")
                        return

                    if tools_used >= MAX_TOOL_ROUNDS:
                        # 已达上限且模型仍请求工具：不执行，剥离 tool_calls 收尾（防孤儿，C28）
                        yield Notice(
                            text=f"已达最大工具轮数（{MAX_TOOL_ROUNDS}），不再执行工具调用。"
                        )
                        session.append(
                            self._assistant_message_from(resp, keep_thinking=keep_thinking)
                        )
                        if session.plan_mode:
                            yield Notice(
                                text=PLAN_DONE_NOTICE.format(plan_file_path=session.plan_file)
                            )
                        await self._hooks.run_hooks(HookContext(event="session_end"))
                        for he in self._hook_events("session_end"):
                            yield he
                        self._record_run_and_maybe_extract()
                        yield LoopComplete(reason="max_rounds")
                        return

                    # ---- 执行工具轮（C44 分批 + C60/C61，N18 统一配对）----
                    session.append(
                        self._assistant_message_from(
                            resp, tool_calls=resp.tool_calls, keep_thinking=keep_thinking
                        )
                    )
                    results: list[ToolResultBlock | None] = [None] * len(resp.tool_calls)
                    async for ev in self._execute_tools(resp.tool_calls, results, flags):
                        yield ev

                    # ---- 未知工具熔断（C58）：整轮全未知 streak+1，任一已知清零 ----
                    registry = session.registry
                    assert registry is not None
                    if all(registry.find(tc.name) is None for tc in resp.tool_calls):
                        unknown_streak += 1
                    else:
                        unknown_streak = 0
                    if unknown_streak >= UNKNOWN_TOOL_STREAK_LIMIT:
                        await self._hooks.run_hooks(HookContext(event="session_end"))
                        for he in self._hook_events("session_end"):
                            yield he
                        self._record_run_and_maybe_extract()
                        yield ErrorEvent(
                            message=(
                                f"连续 {UNKNOWN_TOOL_STREAK_LIMIT} 轮仅请求未知工具，"
                                "已终止本次编排（请核对工具清单或系统提示词）"
                            )
                        )
                        yield LoopComplete(reason="error")
                        return

                    await self._hooks.run_hooks(
                        HookContext(event="turn_end", round=round_index)
                    )
                    for he in self._hook_events("turn_end"):
                        yield he
                    yield TurnComplete(round=round_index)
                    tools_used += 1
            except (asyncio.CancelledError, GeneratorExit):
                # 中断边界（spec C29 / ch04 N29）：配对收尾后让取消沿生成器契约继续传播；
                # except 内不可 yield，硬取消不产出终止事件（消费者以异常感知中断）
                if not flags["tools_interrupted"]:
                    self._finalize_interrupted(current, round_index)
                self._record_run_and_maybe_extract()
                raise
            except ProviderError as exc:
                # 出错边界与中断一致：有半截保留标中断；一字未出按轮次回滚（N19 修订）
                if not flags["tools_interrupted"]:
                    self._finalize_interrupted(current, round_index)
                await self._hooks.run_hooks(HookContext(event="session_end"))
                for he in self._hook_events("session_end"):
                    yield he
                self._record_run_and_maybe_extract()
                yield ErrorEvent(message=str(exc))
                yield LoopComplete(reason="error")
            except Exception as exc:  # noqa: BLE001 未预期异常同样事件化（N19 修订）
                # 工具阶段异常已自行配对入史（flags 置位）——不可再回滚，
                # 否则会弹掉配对消息制造孤儿 assistant(tool_calls)（N18）
                if not flags["tools_interrupted"]:
                    self._finalize_interrupted(current, round_index)
                await self._hooks.run_hooks(HookContext(event="session_end"))
                for he in self._hook_events("session_end"):
                    yield he
                self._record_run_and_maybe_extract()
                yield ErrorEvent(message=f"未预期错误：{type(exc).__name__}: {exc}")
                yield LoopComplete(reason="error")
        finally:
            self._active = False

    # ---- 记忆提取触发（spec(docs/ch03) C52 延续）----

    def _record_run_and_maybe_extract(self) -> None:
        """终止统一出口计数（全部终止原因均计，含硬取消）+ 周期性记忆提取触发。"""
        self._run_count += 1
        if self._run_count % EXTRACT_EVERY_N_RUNS == 0:
            self._schedule_memory_extraction()

    def _schedule_memory_extraction(self) -> None:
        """fire-and-forget 触发记忆提取：历史快照传参，任务集跟踪防 GC。"""
        history = list(self._session.history)
        if not history:
            return
        task = asyncio.create_task(extract_memory(self._session.provider, history))
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    # ---- 工具轮执行（分批，spec(docs/ch03) C44 延续；ch04 生成器化 + C60/C61）----

    async def _execute_tools(
        self,
        tool_calls: list[ToolCall],
        results: list[ToolResultBlock | None],
        flags: dict,
    ) -> AsyncIterator[AgentEvent]:
        """分批执行本轮工具：安全批并发（合流转发事件）、不安全各自串行。

        本轮统一配对入史（N18）；中断/未预期异常时已完成保留真实结果、
        被取消与未开始的全部配对「[已中断]」（零孤儿）后 re-raise。
        """
        registry = self._session.registry
        assert registry is not None
        idx_of = {tc.id: i for i, tc in enumerate(tool_calls)}
        try:
            for batch in partition_tool_calls(tool_calls, registry):
                if batch.concurrent:
                    async for ev in self._execute_batch_parallel(batch.calls, results, idx_of):
                        yield ev
                else:
                    # 不安全各自成批：calls 必为单元素，串行执行
                    tc = batch.calls[0]
                    async for ev in self._execute_tool(tc, results, idx_of[tc.id]):
                        yield ev
        except (asyncio.CancelledError, GeneratorExit):
            # Esc / 取消：当前与剩余工具全部配对中断结果后收尾（不变量保持）
            self._fill_interrupted(tool_calls, results)
            self._session.append(Message(role="user", content="", tool_results=results))
            flags["tools_interrupted"] = True
            raise
        except Exception:
            # 未预期异常（如钩子内部出错）：同样补齐配对再上抛，任何路径零孤儿（N18）
            self._fill_interrupted(tool_calls, results)
            self._session.append(Message(role="user", content="", tool_results=results))
            flags["tools_interrupted"] = True
            raise
        self._session.append(Message(role="user", content="", tool_results=results))

    @staticmethod
    def _fill_interrupted(
        tool_calls: list[ToolCall], results: list[ToolResultBlock | None]
    ) -> None:
        """未完成的调用统一配对「[已中断]」错误结果（零孤儿，N18）。"""
        for i, r in enumerate(results):
            if r is None:
                results[i] = ToolResultBlock(
                    tool_call_id=tool_calls[i].id,
                    content=INTERRUPTED_TOOL_RESULT,
                    is_error=True,
                )

    async def _execute_batch_parallel(
        self,
        calls: list[ToolCall],
        results: list[ToolResultBlock | None],
        idx_of: dict[str, int],
    ) -> AsyncIterator[AgentEvent]:
        """安全批并发执行（N25）：每个工具一个 pump 任务（信号量限流，N16），
        事件经合流队列按到达序转发（多工具增量输出/心跳按 id 交错，N21 延续）。
        """
        merged: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def pump(tc: ToolCall) -> None:
            try:
                async with semaphore:
                    async for ev in self._execute_tool(tc, results, idx_of[tc.id]):
                        merged.put_nowait(ev)
            finally:
                merged.put_nowait(None)  # 正常/异常/取消都计完成，防主循环悬等

        pumps = [asyncio.create_task(pump(tc)) for tc in calls]
        remaining = len(calls)
        try:
            while remaining:
                ev = await merged.get()
                if ev is None:
                    remaining -= 1
                    continue
                yield ev
            # 全部 pump 结束：若有异常在此上抛（保持 ch03「异常中断整轮」语义）
            await asyncio.gather(*pumps)
        finally:
            for p in pumps:
                p.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    async def _execute_tool(
        self,
        tc: ToolCall,
        results: list[ToolResultBlock | None],
        idx: int,
    ) -> AsyncIterator[AgentEvent]:
        """串行执行单个工具并转发过程事件：plan 拦截（内置第一道）→ pre_tool_use
        hook（可阻断）→ 权限确认（C51）→ 执行（增量输出/心跳）→ 结果整形持久化
        （C60）→ post_tool_use hook。

        结果写入 results[idx]（本轮统一配对入史，N18）；取消沿 CancelledError 上抛。
        """
        registry = self._session.registry
        assert registry is not None

        # ---- 内置第一道：计划模式拦截（C47/N20，不经 HookEngine）----
        verdict = self._plan_intercept(tc)
        if verdict is not None:
            results[idx] = ToolResultBlock(
                tool_call_id=tc.id, content=verdict.output, is_error=verdict.is_error
            )
            yield ToolResultEvent(
                id=tc.id, name=tc.name, ok=not verdict.is_error, preview=self._preview(verdict)
            )
            async for he in self._post_tool_hooks(tc, verdict):
                yield he
            return

        tool = registry.find(tc.name)

        # ---- pre_tool_use hook（C61，可阻断）----
        hook_args: dict = {}
        if tool is not None:
            try:
                hook_args = parse_arguments(tool, tc.arguments)
            except ToolError:
                hook_args = {}  # 坏参数：交由执行路径返回结构化校验错误，hook 拿空参
        rejection = await self._hooks.run_hooks(
            HookContext(
                event="pre_tool_use",
                tool_name=tc.name,
                args=hook_args,
                file_path=infer_file_path(hook_args),
            )
        )
        for he in self._hook_events("pre_tool_use"):
            yield he
        if rejection is not None:
            rejected = ToolResult(f"Hook rejected: {rejection.reason}", is_error=True)
            results[idx] = ToolResultBlock(
                tool_call_id=tc.id, content=rejected.output, is_error=True
            )
            yield ToolResultEvent(
                id=tc.id, name=tc.name, ok=False, preview=self._preview(rejected)
            )
            async for he in self._post_tool_hooks(tc, rejected):
                yield he
            return

        # ---- 权限确认（C51 延续）：普通模式下的写类与白名单外命令 ----
        if (
            tool is not None
            and tc.name not in self._permission_granted
            and not self._session.plan_mode
            and self._needs_permission(tool, hook_args)
        ):
            future: asyncio.Future[PermissionResponse] = asyncio.get_running_loop().create_future()
            self._pending_permissions[tc.id] = future
            yield PermissionRequest(
                request_id=tc.id,
                tool_name=tc.name,
                summary=self._permission_summary(tc, hook_args),
                future=future,
            )
            try:
                response = await asyncio.wait_for(future, timeout=PERMISSION_TIMEOUT)
            except asyncio.TimeoutError:
                response = PermissionResponse.DENY  # 超时按拒绝（checklist 承接值）
            finally:
                self._pending_permissions.pop(tc.id, None)
            if response is PermissionResponse.ALLOW_ALWAYS:
                self._permission_granted.add(tc.name)  # 本会话内不再询问
            if response is PermissionResponse.DENY:
                denied = ToolResult(PERMISSION_DENIED, is_error=True)
                results[idx] = ToolResultBlock(
                    tool_call_id=tc.id, content=denied.output, is_error=True
                )
                yield ToolResultEvent(
                    id=tc.id, name=tc.name, ok=False, preview=self._preview(denied)
                )
                async for he in self._post_tool_hooks(tc, denied):
                    yield he
                return

        # ---- 执行：emit 内部队列 + 心跳轮转（C30/C31 延续），事件改 yield ----
        queue: asyncio.Queue[str] = asyncio.Queue()

        def emit(chunk: str) -> None:
            queue.put_nowait(chunk)

        task = asyncio.create_task(registry.run(tc.name, tc.arguments, emit))
        start = time.monotonic()
        pending_get: asyncio.Task | None = None
        try:
            while True:
                if pending_get is None:
                    pending_get = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {task, pending_get},
                    timeout=HEARTBEAT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending_get in done:
                    yield ToolOutputDelta(id=tc.id, text=pending_get.result())
                    pending_get = None
                    continue
                if task in done:
                    break
                yield ToolHeartbeat(id=tc.id, name=tc.name, elapsed=time.monotonic() - start)
        finally:
            if pending_get is not None:
                pending_get.cancel()
                await asyncio.gather(pending_get, return_exceptions=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        # 任务已结束：排空队列残余分片并取结果（工具被取消时 task.result() 照常抛 CancelledError）
        while not queue.empty():
            yield ToolOutputDelta(id=tc.id, text=queue.get_nowait())
        result = task.result()
        shaped = self._maybe_persist_or_truncate(result.output)
        results[idx] = ToolResultBlock(
            tool_call_id=tc.id, content=shaped, is_error=result.is_error
        )
        # 收尾心跳：给出精确总耗时，供 TUI 结束态展示「（N.Ns）」（spec C31）
        yield ToolHeartbeat(id=tc.id, name=tc.name, elapsed=time.monotonic() - start)
        yield ToolResultEvent(
            id=tc.id, name=tc.name, ok=not result.is_error, preview=self._preview(result)
        )
        # ---- post_tool_use hook（C61，只观察；含否决/拒绝产生的结果）----
        async for he in self._post_tool_hooks(tc, result):
            yield he

    # ---- 结果整形与持久化（spec(docs/ch04) C60）----

    def _maybe_persist_or_truncate(self, output: str) -> str:
        """工具结果整形：超 SINGLE_RESULT_CHAR_LIMIT 持久化落盘、回灌预览；
        超 MAX_OUTPUT_CHARS 兜底截断（safe_execute 已先行截断，此为双保险）。"""
        if len(output) > SINGLE_RESULT_CHAR_LIMIT:
            return self._persist_tool_result(output)
        if len(output) > MAX_OUTPUT_CHARS:
            return truncate_output(output)
        return output

    def _persist_tool_result(self, output: str) -> str:
        """超长结果持久化到 session 目录，返回首尾预览 + 保存路径指引。

        落盘失败退化为截断（不阻塞主流程）；session 目录首次持久化时创建。
        """
        try:
            if not self._session_dir_ready:
                self._session_dir.mkdir(parents=True, exist_ok=True)
                self._session_dir_ready = True
            name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.md"
            path = self._session_dir / name
            path.write_text(output, encoding="utf-8")
            edge = PERSIST_PREVIEW_EDGE
            return (
                f"{output[:edge]}\n…\n{output[-edge:]}\n"
                f"…（完整输出 {len(output)} 字符已保存：{path.resolve()}）"
            )
        except OSError:
            return truncate_output(output)

    # ---- 轮次收尾与消息装配 ----

    def _finalize_interrupted(self, current: StreamCollector | None, round_index: int) -> None:
        """流中断 / 出错收尾（spec(docs/ch02) C29 延续 + ch04 恢复轮修订）：
        有半截 → 带标记入史；一字未出 → 首轮回滚用户消息，
        非首轮回滚本轮流前的一对 assistant(tool_calls)+user(tool_results)（副作用不撤销）。
        升档恢复轮（末条为续写指令）被中断：撤回续写指令、半截并回上一条 assistant，
        保持 user/assistant 交替与截断标记。
        """
        partial = current.response.text if current is not None else ""
        history = self._session.history
        if history and history[-1].role == "user" and history[-1].content == RESUME_INSTRUCTION:
            self._session.pop()  # 撤回续写指令（恢复轮被中断）
            remaining = self._session.history
            if remaining and remaining[-1].role == "assistant" and remaining[-1].content:
                prev = self._session.pop()
                self._session.append(
                    Message(
                        role="assistant",
                        content=prev.content + partial + INTERRUPTED_MARKER,
                        thinking=prev.thinking,
                        thinking_signature=prev.thinking_signature,
                    )
                )
            elif partial:
                self._session.append(
                    Message(role="assistant", content=partial + INTERRUPTED_MARKER)
                )
            return
        if partial:
            self._session.append(Message(role="assistant", content=partial + INTERRUPTED_MARKER))
            return
        if round_index <= 1:
            self._session.pop()  # 本轮作废，回滚用户消息
        else:
            self._session.pop()  # user(tool_results)
            self._session.pop()  # assistant(tool_calls)

    def _tool_schemas(self) -> list[dict] | None:
        """当前协议的工具列表；无注册中心（纯对话回归）时为 None。

        延迟工具由注册中心按披露状态过滤（ch03 C53），本层不再传提问。
        """
        registry = self._session.registry
        if registry is None:
            return None
        return registry.get_all_schemas(self._session.provider.profile.protocol)

    @staticmethod
    def _assistant_message_from(
        resp: LLMResponse,
        tool_calls: list[ToolCall] | None = None,
        keep_thinking: bool = False,
    ) -> Message:
        """轮次折叠结果 → assistant 消息（正文 / 思考 / 签名 / 工具调用）。

        keep_thinking=False（纯对话路径）时不带 thinking/signature 入史（spec O6 修订）。
        """
        return Message(
            role="assistant",
            content=resp.text,
            thinking=resp.thinking if keep_thinking else "",
            thinking_signature=resp.signature if keep_thinking else "",
            tool_calls=tool_calls or [],
        )

    @staticmethod
    def _preview(result: ToolResult) -> str:
        """给 TUI 工具行的单行摘要（首行前 60 字符）。"""
        first = result.output.strip().splitlines()[0] if result.output.strip() else ""
        return first[:60]
