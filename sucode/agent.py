"""Agent 编排器（Phase 3 tasks T31/T32 → T33/T35 扩展）：

Phase 3（spec(docs/ch03) C42-C45）把 ReAct 循环从 ChatSession 迁出为独立编排器：
- start(text)：创建后台任务跑循环，返回 asyncio.Queue 事件通道（广播给上层消费者）
- 通道事件序列：UserMessage → 每轮 RoundStart → 流事件 / 工具反馈 → RunFinished
- RunFinished 是通道内唯一结束信号；后台任务内一切异常转事件（N19）：
  ProviderError / 未预期异常 → RunError + RunFinished(error)，任务内消化不外抛；
  CancelledError → 中断收尾 → RunFinished(interrupted) 后 re-raise（任务取消契约）
- 历史读写全部经 ChatSession（会话层退化为历史 / profile / 模式状态容器，C42）

工具分批执行（C44/C45，N16/N18/N21）：
- 切批算法：遍历同轮调用序列，经注册中心查工具做按入参的并发安全判定；
  安全且上一批也是安全批则并入，否则新开批 → 相邻安全调用合并、不安全各自成批、
  未知工具判不安全（走串行路径在执行时返回结构化错误）
- 安全批 asyncio.gather + 信号量限并发（CONCURRENCY_LIMIT）；批间按原顺序串行
- 每批全部完成后统一配对入史；中断时已完成保留真实结果、被取消与未开始的
  全部配对「[已中断]」结果（零孤儿）
- 并发批内多工具的增量输出 / 心跳按调用 id 区分各自产出（N21）

延续 ch02 硬约束（spec(docs/ch02) C27-C33）：
- 安全上限：工具轮数达 MAX_TOOL_ROUNDS 后强制收尾（C28）
- 思考留存：仅带工具的会话把 thinking/signature 入史（C25 修订）
- 每轮流各自创建、各自 aclose（C33）

Phase 3 修订（spec(docs/ch03) C51/C52，2026-10-21）：
- 权限确认：普通模式下写类与白名单外命令执行前发 PermissionRequest（应答经
  Future 传回）；deny → 结构化拒绝配对入史（工具不执行）；always → 会话级
  授权集放行后续同工具调用；超时按拒绝；计划模式不走确认（自动拦截照旧）
- 记忆提取：终止事件统一出口做运行计数，每 5 次触发后台提取任务（历史快照 →
  LLM 总结 → 追加写入 cwd 的 project_memory.md；fire-and-forget，失败静默）
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path

from sucode.chat import ChatSession
from sucode.memory import EXTRACT_EVERY_N_RUNS, extract_memory
from sucode.providers.base import (
    FinishReason,
    Message,
    Notice,
    PermissionRequest,
    ProviderError,
    RoundStart,
    RunError,
    RunFinished,
    SignatureDelta,
    StreamDone,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallStart,
    ToolExecuted,
    ToolHeartbeat,
    ToolOutputDelta,
    ToolResultBlock,
    UserMessage,
)
from sucode.tools import ToolRegistry
from sucode.tools.bash_tool import is_readonly_command
from sucode.tools.base import Tool, ToolError, ToolResult, parse_arguments

INTERRUPTED_MARKER = "\n\n[已中断]"
INTERRUPTED_TOOL_RESULT = "[已中断] 用户取消了本次工具执行"
MAX_TOOL_ROUNDS = 10  # spec(docs/ch02) C28 硬上限：工具执行轮数上限，到顶强制收尾
HEARTBEAT_SECONDS = 2.0  # spec(docs/ch02) C31/N12 心跳间隔
CONCURRENCY_LIMIT = 4  # spec(docs/ch03) N16：安全批内并发上限（checklist 承接值）

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

# 事件产出回调：同步、不得阻塞（Agent 内统一为 queue.put_nowait）
EventSink = Callable[[StreamEvent], None]

# 拦截位钩子（spec(docs/ch03) C49，T33）：
# 前置：返回 ToolResult 即否决（该结果直接配对入史，工具不执行）；返回 None 放行
PreToolHook = Callable[[ToolCall], Awaitable[ToolResult | None]]
# 后置：只观察（入参为工具调用 + 执行结果，含前置否决产生的结果）
PostToolHook = Callable[[ToolCall, ToolResult], Awaitable[None]]


class _RoundAggregator:
    """单轮流事件的聚合器：正文/思考/签名/工具调用碎片 → 完整结构。"""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._signature: str = ""
        self.tool_calls: list[ToolCall] = []
        self._args: dict[str, list[str]] = {}  # 工具 id → 参数碎片
        self.finish_reason: str = ""

    def feed(self, ev: StreamEvent) -> None:
        if isinstance(ev, TextDelta):
            self._text.append(ev.text)
        elif isinstance(ev, ThinkingDelta):
            self._thinking.append(ev.text)
        elif isinstance(ev, SignatureDelta):
            self._signature += ev.signature
        elif isinstance(ev, ToolCallStart):
            self.tool_calls.append(ToolCall(id=ev.id, name=ev.name))
            self._args[ev.id] = []
        elif isinstance(ev, ToolCallDelta):
            if ev.id in self._args:
                self._args[ev.id].append(ev.arguments)
        elif isinstance(ev, StreamDone):
            self.finish_reason = ev.finish_reason

    @property
    def text(self) -> str:
        return "".join(self._text)

    @property
    def thinking(self) -> str:
        return "".join(self._thinking)

    @property
    def signature(self) -> str:
        return self._signature

    def finalize_arguments(self) -> None:
        """流结束后把参数碎片拼接进 ToolCall（空参工具自然归一空串）。"""
        for tc in self.tool_calls:
            tc.arguments = "".join(self._args.get(tc.id, []))


def _same_file_path(a: str, b: str) -> bool:
    """两个路径是否指向同一文件（resolve 归一后精确匹配；计划模式放行判定，L9）。"""
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


class Agent:
    """ReAct 编排器：后台任务跑循环，事件经 asyncio.Queue 通道广播（C42/C43）。"""

    def __init__(
        self,
        session: ChatSession,
        pre_hooks: Sequence[PreToolHook] = (),
        post_hooks: Sequence[PostToolHook] = (),
    ) -> None:
        self._session = session
        # 拦截位（C49）：计划模式拦截是第一个内置前置钩子（C47），其余默认空直通
        self._pre_hooks: list[PreToolHook] = [self._plan_intercept, *pre_hooks]
        self._post_hooks = list(post_hooks)
        self._task: asyncio.Task | None = None
        self._cancel_event = asyncio.Event()  # 取消信号（C48，T35）
        # 权限确认（C51 修订）：待应答表（请求 id → Future）+ 会话级授权工具集（always 语义）
        self._pending_permissions: dict[str, asyncio.Future[str]] = {}
        self._permission_granted: set[str] = set()
        # 记忆提取（C52 修订）：运行计数（每 EXTRACT_EVERY_N_RUNS 次触发）+ 后台任务集
        self._run_count = 0
        self._memory_tasks: set[asyncio.Task] = set()

    # ---- 取消信号（spec(docs/ch03) C48）----

    def request_cancel(self) -> None:
        """触发取消信号：轮间检查点检测到后干净退出（温和，当前轮跑完才停，L8）。

        与任务取消（Esc 的即时路径）互补：本信号不抛异常、不打断进行中的流与工具，
        退出时历史天然配对完整；终止事件原因 = cancelled。
        """
        self._cancel_event.set()

    # ---- 权限确认（spec(docs/ch03) C51 修订，T38/T39）----

    def resolve_permission(self, request_id: str, verdict: str) -> None:
        """应答权限请求（TUI 调用）：置 Future 结果；已失效请求静默忽略（N23）。"""
        future = self._pending_permissions.get(request_id)
        if future is not None and not future.done():
            future.set_result(verdict)

    @staticmethod
    def _needs_permission(tool: Tool, args: dict) -> bool:
        """风险操作判定（C51）：写类恒确认；命令类白名单外确认；读类不确认。"""
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

    async def _request_permission(self, tc: ToolCall, args: dict, out: EventSink) -> str:
        """发出权限请求并等待应答（C51/N23）：超时按拒绝；finally 清待应答表。"""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_permissions[tc.id] = future
        out(
            PermissionRequest(
                request_id=tc.id,
                tool_name=tc.name,
                summary=self._permission_summary(tc, args),
                future=future,
            )
        )
        try:
            return await asyncio.wait_for(future, timeout=PERMISSION_TIMEOUT)
        except asyncio.TimeoutError:
            return "deny"  # 超时按拒绝（checklist 承接值）
        finally:
            self._pending_permissions.pop(tc.id, None)

    # ---- 系统提示词装配（C50）与计划模式（C47）----

    def _system_prompt(self) -> str:
        """按会话模式装配系统提示词：普通最小文案 / 计划模式专用文案。"""
        session = self._session
        if not session.plan_mode:
            return NORMAL_SYSTEM_PROMPT
        return PLAN_SYSTEM_PROMPT_TEMPLATE.format(plan_file_path=session.plan_file)

    async def _plan_intercept(self, tc: ToolCall) -> ToolResult | None:
        """计划模式内置前置钩子（C47/N20）：非计划文件的写类与非白名单命令一律拦截。

        放行规则（N20 例外唯一性）：写类目标 = 计划文件（按文件身份精确匹配，L9）；
        命令类命中只读白名单（与并发判定共享，C46）；读类放行。
        非计划模式直接放行（钩子不生效）。
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
        return None  # 读类与未知工具放行（未知工具由串行执行路径返回结构化错误）

    def _notify_plan_finished(self, out: EventSink) -> None:
        """计划模式收尾提示（C47）：产出最终回复时提醒用户手动 /exec 切换。"""
        session = self._session
        if session.plan_mode:
            out(Notice(PLAN_DONE_NOTICE.format(plan_file_path=session.plan_file)))

    # ---- 通道与任务管理 ----

    def start(self, text: str) -> asyncio.Queue[StreamEvent]:
        """启动一次编排：用户消息入史 → 后台任务跑循环 → 返回事件通道。

        通道事件以 RunFinished 收尾（唯一结束信号）；重复启动要求上一次已终止
        （防止两次运行的历史写入交错）。
        """
        if self._task is not None and not self._task.done():
            raise RuntimeError("上一次编排仍在进行中，须等待 RunFinished 后再启动")
        if self._session.take_plan_exit_pending():
            # /exec 后首次请求：拼接一次模式切换提示（C47 修订），随该轮 user 消息入史
            text = f"{PLAN_EXIT_NOTICE}\n{text}"
        self._cancel_event.clear()  # 取消信号跨运行不复用（每次运行信号全新）
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._task = asyncio.create_task(self._run(text, queue))
        return queue

    @property
    def task(self) -> asyncio.Task | None:
        """当前后台任务（TUI 中断路径取消用；未启动过为 None）。"""
        return self._task

    async def _run(self, text: str, queue: asyncio.Queue[StreamEvent]) -> None:
        """循环主体（原 ChatSession.send 迁移）：异常全部事件化，不跨任务抛。"""
        out = queue.put_nowait  # 单生产者按序压入（N17：先进先出不丢不乱）

        session = self._session
        session.append(Message(role="user", content=text))
        out(UserMessage(text=text))

        round_index = 0  # 已开始的 LLM 流轮次（从 1 起）
        tools_used = 0  # 已执行的工具轮数（上限 MAX_TOOL_ROUNDS）
        flags = {"tools_interrupted": False}  # 工具阶段是否已自行配对收尾
        current: _RoundAggregator | None = None
        # 思考留存规则（spec O6 修订 + C25）：仅带工具的会话才把 thinking/signature 入史
        keep_thinking = session.registry is not None
        try:
            while True:
                if self._cancel_event.is_set():
                    # 取消信号轮间检查点（C48）：当前轮已完成且配对入史，干净退出
                    self._finish(out, "cancelled")
                    return
                round_index += 1
                offer_tools = session.registry is not None and tools_used < MAX_TOOL_ROUNDS
                out(RoundStart(round=round_index))

                current = _RoundAggregator()
                async for ev in self._consume_round(current, offer_tools, text):
                    out(ev)
                current.finalize_arguments()

                if not current.tool_calls or session.registry is None:
                    # 最终回复（含 registry 缺失时的纯对话路径）
                    session.append(self._assistant_message(current, keep_thinking=keep_thinking))
                    self._notify_plan_finished(out)  # 计划模式收尾提示（C47）
                    self._finish(out, "completed")
                    return

                if tools_used >= MAX_TOOL_ROUNDS:
                    # 已达上限且模型仍请求工具：不执行，剥离 tool_calls 收尾（防孤儿，C28）
                    out(Notice(f"已达最大工具轮数（{MAX_TOOL_ROUNDS}），不再执行工具调用。"))
                    session.append(self._assistant_message(current, keep_thinking=keep_thinking))
                    self._notify_plan_finished(out)  # 计划模式收尾提示（C47）
                    self._finish(out, "max_rounds")
                    return

                # ---- 执行工具轮（spec C27/C29 + Phase 3 分批 C44）----
                session.append(
                    self._assistant_message(
                        current, tool_calls=current.tool_calls, keep_thinking=keep_thinking
                    )
                )
                await self._execute_tools(current.tool_calls, flags, out)
                tools_used += 1
        except (asyncio.CancelledError, GeneratorExit):
            # 中断边界（spec C29）：收尾后发终止事件，再让取消沿任务契约继续传播
            if not flags["tools_interrupted"]:
                self._finalize_interrupted(current, round_index)
            self._finish(out, "interrupted")
            raise
        except ProviderError as exc:
            # 出错边界与中断一致：有半截保留标中断；一字未出按轮次回滚（N19 事件化收尾）
            if not flags["tools_interrupted"]:
                self._finalize_interrupted(current, round_index)
            out(RunError(str(exc)))
            self._finish(out, "error")
        except Exception as exc:  # noqa: BLE001 未预期异常同样事件化，任务不崩（N19）
            # 工具阶段异常（如钩子内部出错）已自行配对入史（flags 置位）——不可再回滚，
            # 否则会弹掉配对消息制造孤儿 assistant(tool_calls)（N18）
            if not flags["tools_interrupted"]:
                self._finalize_interrupted(current, round_index)
            out(RunError(f"未预期错误：{type(exc).__name__}: {exc}"))
            self._finish(out, "error")

    def _finish(self, out: EventSink, reason: FinishReason) -> None:
        """终止事件统一出口（C43）：运行计数 + 周期性记忆提取触发（C52 修订）。

        全部终止原因（completed / interrupted / cancelled / error / max_rounds）均计数。
        """
        self._run_count += 1
        if self._run_count % EXTRACT_EVERY_N_RUNS == 0:
            self._schedule_memory_extraction()
        out(RunFinished(reason))

    def _schedule_memory_extraction(self) -> None:
        """fire-and-forget 触发记忆提取（C52）：历史快照传参，任务集跟踪防 GC。"""
        history = list(self._session.history)
        if not history:
            return
        task = asyncio.create_task(extract_memory(self._session.provider, history))
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    # ---- 工具轮执行（分批，spec(docs/ch03) C44/C45） ----

    def _is_safe_call(self, tc: ToolCall) -> bool:
        """单次调用是否并发安全：未知工具 / 参数解析失败 / 判定方法说不安全 → False。"""
        registry = self._session.registry
        assert registry is not None
        tool = registry.find(tc.name)
        if tool is None:
            return False  # 未知工具：串行路径，执行时返回结构化「未知工具」错误
        try:
            args = parse_arguments(tool, tc.arguments)
        except ToolError:
            return False  # 坏参数：串行路径，执行时返回结构化校验错误
        return tool.is_concurrency_safe(args)

    def _plan_batches(self, tool_calls: list[ToolCall]) -> list[tuple[bool, list[int]]]:
        """切批（需求方伪代码）：安全且上一批也是安全批则并入，否则新开批。

        产物 [(is_safe, [原始下标...])]：相邻安全调用合并为一批，
        不安全调用各自成批；批与批保持原始顺序。
        """
        batches: list[tuple[bool, list[int]]] = []
        for idx, tc in enumerate(tool_calls):
            safe = self._is_safe_call(tc)
            if safe and batches and batches[-1][0]:
                batches[-1][1].append(idx)
            else:
                batches.append((safe, [idx]))
        return batches

    async def _execute_tools(
        self, tool_calls: list[ToolCall], flags: dict, out: EventSink
    ) -> None:
        """分批执行本轮工具：安全批并发（信号量限流）、不安全各自串行、批间按原顺序。

        每批等全部完成后统一配对入史（N18）；中断时已完成保留真实结果、
        被取消与未开始的全部配对「[已中断]」结果（零孤儿），随后 re-raise。
        """
        results: list[ToolResultBlock | None] = [None] * len(tool_calls)
        try:
            for is_safe, indices in self._plan_batches(tool_calls):
                if is_safe:
                    await self._run_parallel_batch(tool_calls, indices, results, out)
                else:
                    # 不安全各自成批：indices 必为单元素，串行执行
                    await self._run_tool(tool_calls[indices[0]], results, indices[0], out)
        except (asyncio.CancelledError, GeneratorExit):
            # Esc / 取消：当前与剩余工具全部配对中断结果后收尾（不变量保持）
            for i, r in enumerate(results):
                if r is None:
                    results[i] = ToolResultBlock(
                        tool_call_id=tool_calls[i].id,
                        content=INTERRUPTED_TOOL_RESULT,
                        is_error=True,
                    )
            self._session.append(Message(role="user", content="", tool_results=results))
            flags["tools_interrupted"] = True
            raise
        except Exception:
            # 未预期异常（如钩子内部出错）：同样补齐配对再上抛，任何路径零孤儿（N18）
            for i, r in enumerate(results):
                if r is None:
                    results[i] = ToolResultBlock(
                        tool_call_id=tool_calls[i].id,
                        content=INTERRUPTED_TOOL_RESULT,
                        is_error=True,
                    )
            self._session.append(Message(role="user", content="", tool_results=results))
            flags["tools_interrupted"] = True
            raise
        self._session.append(Message(role="user", content="", tool_results=results))

    async def _run_parallel_batch(
        self,
        tool_calls: list[ToolCall],
        indices: list[int],
        results: list[ToolResultBlock | None],
        out: EventSink,
    ) -> None:
        """安全批并发执行：信号量限并发（N16），多工具增量输出 / 心跳按 id 交错产出（N21）。"""
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def runner(idx: int) -> None:
            async with semaphore:
                await self._run_tool(tool_calls[idx], results, idx, out)

        await asyncio.gather(*(runner(i) for i in indices))

    async def _run_tool(
        self,
        tc: ToolCall,
        results: list[ToolResultBlock | None],
        idx: int,
        out: EventSink,
    ) -> None:
        """执行单个工具并边等边反馈：输出分片（ToolOutputDelta）+ 心跳（ToolHeartbeat）。

        前置钩子（C49）在执行前依次询问：任一返回结果即否决——该结果直接配对入史、
        工具不执行（ToolExecuted 照常产出供 UI 收尾）；全部放行才真正执行。
        工具在后台任务里跑，emit 同步把分片塞进队列；本协程同时 race 队列与任务：
        队列有货即回传分片、任务未完成即按间隔发心跳、任务完成即排空残余并产出 ToolExecuted。
        后置钩子（C49）在结果确定后观察（含否决结果）。取消沿 CancelledError 上抛。
        结果写入 results[idx]（批完成统一配对入史）。
        """
        # ---- 前置钩子：可否决（C49）----
        for hook in self._pre_hooks:
            verdict = await hook(tc)
            if verdict is not None:
                results[idx] = ToolResultBlock(
                    tool_call_id=tc.id, content=verdict.output, is_error=verdict.is_error
                )
                out(ToolExecuted(id=tc.id, name=tc.name, ok=not verdict.is_error,
                                 preview=self._preview(verdict)))
                for post in self._post_hooks:
                    await post(tc, verdict)
                return

        # ---- 权限确认（C51 修订）：普通模式下的写类与白名单外命令 ----
        registry = self._session.registry
        assert registry is not None
        tool = registry.find(tc.name)
        if (
            tool is not None
            and tc.name not in self._permission_granted
            and not self._session.plan_mode
        ):
            try:
                args = parse_arguments(tool, tc.arguments)
            except ToolError:
                args = None  # 坏参数：交由执行路径返回结构化校验错误，不走确认
            if args is not None and self._needs_permission(tool, args):
                verdict = await self._request_permission(tc, args, out)
                if verdict == "always":
                    self._permission_granted.add(tc.name)  # 本会话内不再询问
                if verdict == "deny":
                    denied = ToolResult(PERMISSION_DENIED, is_error=True)
                    results[idx] = ToolResultBlock(
                        tool_call_id=tc.id, content=denied.output, is_error=True
                    )
                    out(ToolExecuted(id=tc.id, name=tc.name, ok=False,
                                     preview=self._preview(denied)))
                    for post in self._post_hooks:
                        await post(tc, denied)
                    return

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
                    out(ToolOutputDelta(id=tc.id, text=pending_get.result()))
                    pending_get = None
                    continue
                if task in done:
                    break
                out(ToolHeartbeat(id=tc.id, name=tc.name, elapsed=time.monotonic() - start))
        finally:
            if pending_get is not None:
                pending_get.cancel()
                await asyncio.gather(pending_get, return_exceptions=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        # 任务已结束：排空队列残余分片并取结果（工具被取消时 task.result() 照常抛 CancelledError）
        while not queue.empty():
            out(ToolOutputDelta(id=tc.id, text=queue.get_nowait()))
        result = task.result()
        results[idx] = ToolResultBlock(
            tool_call_id=tc.id, content=result.output, is_error=result.is_error
        )
        # 收尾心跳：给出精确总耗时，供 TUI 结束态展示「（N.Ns）」（spec C31）
        out(ToolHeartbeat(id=tc.id, name=tc.name, elapsed=time.monotonic() - start))
        out(ToolExecuted(id=tc.id, name=tc.name, ok=not result.is_error, preview=self._preview(result)))
        # ---- 后置钩子：只观察（C49）----
        for post in self._post_hooks:
            await post(tc, result)

    # ---- 轮次消费与收尾 ----

    async def _consume_round(
        self, agg: _RoundAggregator, offer_tools: bool, query: str = ""
    ) -> AsyncIterator[StreamEvent]:
        """消费一轮 provider 流：透传事件给通道，同时喂给聚合器；确保 aclose。

        offer_tools=False 时不携带工具定义（达上限后强制模型只能产出正文）；
        query 供延迟工具打分（spec C40）；system 按会话模式装配（C50，不进历史）。
        """
        tools = self._tool_schemas(query) if offer_tools else None
        stream = self._session.provider.stream(
            list(self._session.history), tools, self._system_prompt()
        )
        try:
            async for ev in stream:
                agg.feed(ev)
                yield ev
        finally:
            await stream.aclose()  # 确定性关闭底层流（幂等）

    def _tool_schemas(self, query: str = "") -> list[dict] | None:
        """当前协议的工具列表；无注册中心（纯对话回归）时为 None。

        query 传入当前提问，供注册中心对延迟工具打分择优暴露（spec C40）。
        """
        registry = self._session.registry
        if registry is None:
            return None
        return registry.get_all_schemas(self._session.provider.profile.protocol, query=query)

    def _finalize_interrupted(self, agg: _RoundAggregator | None, round_index: int) -> None:
        """流中断 / 出错收尾（spec(docs/ch02) C29）：
        有半截 → 带标记入史；一字未出 → 首轮回滚用户消息（Phase 1 规则），
        非首轮回滚本轮流前的一对 assistant(tool_calls)+user(tool_results)（副作用不撤销）。
        """
        partial = agg.text if agg is not None else ""
        if partial:
            self._session.append(Message(role="assistant", content=partial + INTERRUPTED_MARKER))
            return
        if round_index <= 1:
            self._session.pop()  # 本轮作废，回滚用户消息
        else:
            self._session.pop()  # user(tool_results)
            self._session.pop()  # assistant(tool_calls)

    @staticmethod
    def _assistant_message(
        agg: _RoundAggregator,
        tool_calls: list[ToolCall] | None = None,
        keep_thinking: bool = False,
    ) -> Message:
        """轮次聚合结果 → assistant 消息（正文 / 思考 / 签名 / 工具调用）。

        keep_thinking=False（纯对话路径）时不带 thinking/signature 入史（spec O6 修订）。
        """
        return Message(
            role="assistant",
            content=agg.text,
            thinking=agg.thinking if keep_thinking else "",
            thinking_signature=agg.signature if keep_thinking else "",
            tool_calls=tool_calls or [],
        )

    @staticmethod
    def _preview(result) -> str:
        """给 TUI 工具行的单行摘要（首行前 60 字符）。"""
        first = result.output.strip().splitlines()[0] if result.output.strip() else ""
        return first[:60]
