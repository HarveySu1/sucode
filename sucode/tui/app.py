"""TUI 层（tasks.md T7-T9 → Phase 2 T18 → Phase 3 T36 接入事件通道）：
三段布局 + 多行输入 + 流式渲染 + 命令与中断 + 工具行 + 计划模式。

T7：布局、输入绑定、退出；T8：AI 消息流式渲染；
T9：斜杠命令、Esc 中断、错误展示、profile 热切换顶栏更新；
Phase 2：工具调用过程可视化（ToolCallStart/Delta 流式参数 → ToolExecuted ✓/✗ 状态）、
每个 RoundStart 新建 AI 块、Bash 增量输出与全工具心跳计时。
Phase 3（spec(docs/ch03) C42/C43/C47/C48）：
- 消费 Agent 事件通道（asyncio.Queue）：RunFinished 是唯一结束信号，驱动收尾与
  _streaming 复位；UserMessage 事件跳过（本地已渲染）；RunError 走错误展示
- Esc = 取消信号（温和）+ 后台任务取消（即时）双触发，worker 不再抢先退出——
  沿通道吃到终止事件后按原因收尾（interrupted 标记 / cancelled 正常收尾）
- /plan [文件] /exec 切换计划模式，顶栏随模式更新
Phase 3 修订（spec(docs/ch03) C51，2026-10-21）：
- 权限确认应答：PermissionRequest 事件 → 醒目提示 + 待确认态；该态下输入框
  单行路由 y/n/a（无效输入提示用法，不进消息流）；同 id 工具行到达或 Esc 清理
主流程入口见 sucode/cli.py（T10/T36 装配 Agent）。
Phase 4（/save）：会话历史导出为 markdown 文件——只读 session.history 快照，
不经工具层（无拦截、无权限确认）；纯导出，不做恢复（ch01 spec O2 仍不做）。
ch04（spec(docs/ch04) C55/C59/C61，T59）：事件消费由队列改为
`async for event in agent.run(text)` 生成器直连；块生命周期改惰性创建
（首个内容事件建块）+ TurnComplete / LoopComplete 双收尾；新增 RetryEvent（↻ 恢复
提示）、UsageEvent（token 用量暗淡行）、HookEvent（hook 通知行）渲染；权限应答改传
PermissionResponse 枚举；Esc = request_cancel + worker.cancel（N29 双路径）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static, TextArea

from sucode.agent import (
    Agent,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    Notice,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolHeartbeat,
    ToolOutputDelta,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from sucode.chat import ChatSession
from sucode.config import AppConfig, ConfigError
from sucode.providers.base import (
    Message as SessionMessage,  # 会话历史消息（与 textual.message.Message 同名，取别名区分）
    ToolCallDelta,
    ToolCallStart,
)
from sucode.tui.widgets import AIMessageView

HELP_TEXT = """\
命令列表：
/exit          退出 sucode（同 Ctrl+Q）
/new           清空上下文，重新开始
/profile       查看当前与可用 profile
/profile <名>  切换 profile（对话历史保留）
/plan [文件]   进入计划模式：只读探索，计划写入计划文件（缺省 .sucode/plans/<可读名>.md）
/exec          退出计划模式，恢复执行
/save [文件]   保存当前对话为 markdown（缺省 sucode-session-<时间>.md）
/help          显示本帮助"""


# /save 导出（TUI 侧命令）：缺省文件名前缀 + 工具参数/结果单条字符上限
# （上限防单条巨量输出淹没整份导出文件）
SAVE_DEFAULT_PREFIX = "sucode-session-"
SAVE_ARG_LIMIT = 500
SAVE_RESULT_LIMIT = 2000


def _truncate(text: str, limit: int) -> str:
    """超长文本截断并标记（导出用：原文长度提示保留）。"""
    body = text.strip()
    if len(body) <= limit:
        return body
    return f"{body[:limit]}\n…（已截断，原文 {len(body)} 字符）"


def _fenced(text: str) -> str:
    """包成代码块；内容自带三反引号时升级为四反引号围栏，避免围栏被正文打断。"""
    fence = "````" if "```" in text else "```"
    return f"{fence}text\n{text}\n{fence}"


def _render_transcript(history: list[SessionMessage], profile_label: str) -> str:
    """会话历史快照 → markdown 全文（纯函数、无 I/O，便于脚本直接断言）。

    顺序沿用界面时间顺序：思考 → 工具调用 → 正文；user 消息上的 tool_results 单独成段，
    工具名按 tool_call_id 回查发起它的 assistant（回查不到则显示 id）。
    """
    out: list[str] = [
        "# sucode 会话导出",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- profile：{profile_label}",
        f"- 消息数：{len(history)}",
        "",
    ]
    tool_names: dict[str, str] = {}
    for m in history:
        if m.role == "assistant":
            out += ["## 助手", ""]
            if m.thinking:
                out += ["### 思考", "", _fenced(_truncate(m.thinking, SAVE_RESULT_LIMIT)), ""]
            if m.tool_calls:
                out += ["### 工具调用", ""]
                out += [
                    f"- `{tc.name}` `{_truncate(tc.arguments or '{}', SAVE_ARG_LIMIT)}`"
                    for tc in m.tool_calls
                ]
                out.append("")
                for tc in m.tool_calls:
                    tool_names[tc.id] = tc.name
            if m.content:
                out += [m.content, ""]
        else:
            if m.content:
                out += ["## 你", "", m.content, ""]
            if m.tool_results:
                # 纯工具结果消息（正文为空）不套「你」标题，直接用工具结果段承接上下文
                out += ["### 工具结果", ""] if m.content else ["## 工具结果", ""]
                for tr in m.tool_results:
                    name = tool_names.get(tr.tool_call_id, tr.tool_call_id)
                    out += [
                        f"- `{name}`（{'失败' if tr.is_error else '完成'}）",
                        "",
                        _fenced(_truncate(tr.content, SAVE_RESULT_LIMIT)),
                        "",
                    ]
    return "\n".join(out).rstrip() + "\n"


class InputArea(TextArea):
    """多行输入框：Enter 发送、Shift+Enter / Ctrl+J 换行。"""

    @dataclass
    class Submitted(Message):
        """输入提交事件。"""

        input_widget: InputArea
        text: str

        @property
        def control(self) -> InputArea:
            return self.input_widget

    BINDINGS = [
        Binding("enter", "submit", "发送", priority=True),
        Binding("shift+enter", "newline", "换行", priority=True),
        # 老式控制台（conhost）无法区分 Shift+Enter 与 Enter，Ctrl+J（LF）全终端可辨
        Binding("ctrl+j", "newline", "换行", priority=True, show=False),
    ]

    def action_submit(self) -> None:
        """回车发送：清空输入框并广播提交事件（空输入忽略）。"""
        text = self.text.strip()
        if not text:
            return
        self.clear()
        self.post_message(self.Submitted(self, text))

    def action_newline(self) -> None:
        """Shift+Enter / Ctrl+J：插入换行。"""
        self.insert("\n")


class SucodeApp(App[None]):
    """sucode 全屏对话应用。"""

    CSS = """
    #topbar {
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    #chat {
        height: 1fr;
        padding: 0 1;
    }
    .msg-user, .msg-ai {
        margin-top: 1;
    }
    .msg-system {
        color: $text-muted;
        margin-top: 1;
    }
    .msg-error {
        color: $error;
        text-style: bold;
        margin-top: 1;
    }
    .msg-permission {
        color: $warning;
        text-style: bold;
        margin-top: 1;
    }
    #input {
        height: auto;
        max-height: 40%;
        border: round $accent;
    }
    #input .text-area--cursor {
        background: $surface;
        color: $text;
        text-style: underline;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("escape", "interrupt", "中断生成", priority=True, show=False),
    ]

    def __init__(self, session: ChatSession, agent: Agent, config: AppConfig) -> None:
        super().__init__()
        self._session = session
        self._agent = agent  # 编排器（T36 装配）：TUI 只消费事件通道（C42）
        self._config = config
        self._current_view = None
        self._streaming = False
        # 权限确认（C51 延续）：待确认请求（None = 非待确认态）
        self._pending_permission: PermissionRequest | None = None
        # ch04：当前编排 worker 引用（Esc / on_unmount 取消用，替代 agent.task）
        self._worker = None

    def compose(self) -> ComposeResult:
        yield Static(self._topbar_text(), id="topbar")
        yield VerticalScroll(id="chat")
        yield InputArea(
            id="input",
            placeholder="输入问题 · Enter 发送 · Shift+Enter / Ctrl+J 换行 · Ctrl+Q 退出",
        )

    def on_mount(self) -> None:
        self.query_one("#input", InputArea).focus()

    def on_unmount(self) -> None:
        """退出兜底：取消仍在跑的编排 worker，防事件循环残留（N29 硬取消路径）。"""
        if self._worker is not None and not self._worker.is_finished:
            self._worker.cancel()

    # ---- 顶栏 ----
    def _topbar_text(self) -> str:
        p = self._session.profile
        mode = " · 计划模式" if self._session.plan_mode else ""
        return f"sucode · {p.name}/{p.model}{mode}"

    def _refresh_topbar(self) -> None:
        self.query_one("#topbar", Static).update(self._topbar_text())

    # ---- 消息区 ----
    def _append_message(self, css_class: str, text: str) -> None:
        """向对话区追加一条消息并滚动到底部。"""
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(Static(text, classes=css_class, markup=False))
        chat.scroll_end(animate=False)

    def _new_ai_message(self) -> AIMessageView:
        """创建一条 AI 消息的渲染容器。"""
        chat = self.query_one("#chat", VerticalScroll)
        view = AIMessageView()
        chat.mount(view)
        chat.scroll_end(animate=False)
        return view

    # ---- 输入处理 ----
    @on(InputArea.Submitted)
    def on_submitted(self, event: InputArea.Submitted) -> None:
        """命令解析在最前端；权限确认输入优先路由；生成期间拒绝新消息与非退出命令。"""
        text = event.text
        if self._pending_permission is not None:
            self._resolve_permission(text)  # 待确认态：单行应答路由（C51，不受生成中拦截影响）
            return
        if text.startswith("/"):
            if self._streaming and text.lower() != "/exit":
                self._append_message("msg-system", "（生成中：先按 Esc 中断，或用 /exit 退出）")
                return
            self._handle_command(text)
            return
        if self._streaming:
            self._append_message("msg-system", "（生成中：先按 Esc 中断再发送新消息）")
            return
        self._append_message("msg-user", f"你：{text}")
        self._current_view = None  # 由首个内容事件惰性建块（ch04 C55，替代 RoundStart）
        self._streaming = True
        self._worker = self._consume(text)

    def _handle_command(self, line: str) -> None:
        """执行斜杠命令。"""
        parts = line.split()
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "/exit":
            self.exit()  # 等价于 Ctrl+Q；action_quit 在 8.x 是协程不能同步调
        elif cmd == "/new":
            self._session.clear()
            self._append_message("msg-system", "已清空上下文，重新开始。")
        elif cmd == "/save":
            self._save_transcript(arg)
        elif cmd == "/help":
            self._append_message("msg-system", HELP_TEXT)
        elif cmd == "/plan":
            self._session.enter_plan_mode(arg)
            self._refresh_topbar()
            self._append_message(
                "msg-system",
                f"已进入计划模式：只读探索，唯一可写目标为 {self._session.plan_file}，"
                f"完成后用 /exec 退出并执行。",
            )
        elif cmd == "/exec":
            if not self._session.plan_mode:
                self._append_message("msg-system", "当前不在计划模式，无需退出。")
                return
            self._session.exit_plan_mode()
            self._refresh_topbar()
            self._append_message("msg-system", "已退出计划模式，写操作恢复可用。")
        elif cmd == "/profile":
            if not arg:
                cur = self._session.profile
                available = "、".join(sorted(self._config.profiles)) or "（无）"
                self._append_message(
                    "msg-system",
                    f"当前：{cur.name}（{cur.protocol} · {cur.model}）\n可用：{available}",
                )
                return
            try:
                profile = self._config.get(arg)
            except ConfigError as exc:
                self._append_message("msg-error", str(exc))
                return
            self._session.switch_profile(profile)
            self._refresh_topbar()
            self._append_message(
                "msg-system",
                f"已切换到 {profile.name}（{profile.protocol} · {profile.model}），对话历史保留。",
            )
        else:
            self._append_message("msg-error", f"未知命令 {cmd}，输入 /help 查看可用命令。")

    # ---- /save 会话导出 ----
    def _save_transcript(self, arg: str) -> None:
        """执行 /save：把当前会话历史导出为 markdown 文件。

        - 空历史不建文件；无参用时间戳名（天然不覆盖），带参按用户路径（已存在则覆盖）
        - 父目录不存在 / 目标是目录 / 写失败 → 错误提示，不抛异常
        - 只读 session.history 快照：导出不影响模型上下文（会话层无写入）
        """
        history = self._session.history
        if not history:
            self._append_message("msg-system", "当前没有可保存的对话。")
            return
        p = self._session.profile
        text = _render_transcript(history, f"{p.name}（{p.protocol} · {p.model}）")
        if arg.strip():
            path = Path(arg.strip()).expanduser()
            if path.is_dir():
                self._append_message("msg-error", f"{path} 是目录，请给出文件名。")
                return
            if not path.parent.exists():
                self._append_message("msg-error", f"目录不存在：{path.parent}")
                return
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = Path(f"{SAVE_DEFAULT_PREFIX}{stamp}.md")
        try:
            path.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            self._append_message("msg-error", f"保存失败：{exc}")
            return
        self._append_message("msg-system", f"已保存 {len(history)} 条消息到 {path.resolve()}")

    # ---- 权限确认应答（spec(docs/ch03) C51 修订）----
    def _resolve_permission(self, text: str) -> None:
        """待确认态输入路由：y/yes → 允许、n/no → 拒绝、a/always → 本会话总是允许。

        无效应答提示用法且不进消息流；应答已失效（对方超时按拒绝 / 已取消）时
        resolve_permission 静默忽略（N23）。
        """
        req = self._pending_permission
        assert req is not None
        key = text.strip().lower()
        verdict = {
            "y": PermissionResponse.ALLOW, "yes": PermissionResponse.ALLOW,
            "n": PermissionResponse.DENY, "no": PermissionResponse.DENY,
            "a": PermissionResponse.ALLOW_ALWAYS, "always": PermissionResponse.ALLOW_ALWAYS,
        }.get(key)
        if verdict is None:
            self._append_message(
                "msg-system", "请输入 y（允许）/ n（拒绝）/ a（本会话内总是允许）"
            )
            return
        self._pending_permission = None
        self._agent.resolve_permission(req.request_id, verdict)
        if verdict is PermissionResponse.ALLOW:
            self._append_message("msg-system", "已允许，继续执行。")
        elif verdict is PermissionResponse.ALLOW_ALWAYS:
            self._append_message("msg-system", f"已允许（本会话内 {req.tool_name} 不再询问）。")
        else:
            self._append_message("msg-system", "已拒绝本次执行。")

    # ---- Esc 中断（C48 双路径延续 / N29）----
    def action_interrupt(self) -> None:
        """Esc：取消信号（温和，轮间检查点）+ worker 取消（即时）双触发。

        worker 即生成器消费者：取消后生成器沿 CancelledError 传播做配对收尾（N18），
        本协程 except 分支把界面标记「已中断」（LoopComplete 不产出，N29）。
        待确认的权限请求一并取消（C51：Future 悬空无害，N23）。
        """
        if not self._streaming:
            return
        if self._pending_permission is not None:
            self._pending_permission = None
            self._append_message("msg-system", "已取消待确认的权限请求。")
        self._agent.request_cancel()
        if self._worker is not None and not self._worker.is_finished:
            self._worker.cancel()

    # ---- 事件消费 worker（async for 直连编排生成器，ch04 C55）----
    @work(group="stream")
    async def _consume(self, text: str) -> None:
        """消费编排事件流：async for 直接驱动 agent.run；LoopComplete 后生成器自然终止。

        块生命周期（ch04 修订，替代 RoundStart 驱动）：首个内容事件惰性建块；
        TurnComplete 收尾本轮块、view 置空；LoopComplete 按原因统一收尾
        （error 配 ErrorEvent 暂存文案）。硬取消（worker.cancel）→ 生成器沿
        CancelledError 传播做配对收尾（N18/N29），本协程 except 把界面标记中断。
        """
        view: AIMessageView | None = None
        error_text = ""

        def ensure_view() -> AIMessageView:
            nonlocal view
            if view is None:
                view = self._new_ai_message()
                self._current_view = view
            return view

        try:
            async for ev in self._agent.run(text):
                if isinstance(ev, PermissionRequest):
                    # 待确认态（C51 延续）：醒目提示 + 挂起等输入框应答
                    self._pending_permission = ev
                    self._append_message(
                        "msg-permission",
                        f"⚠ 请求执行 {ev.tool_name}\n{ev.summary}\n"
                        "允许吗？输入 y 允许 / n 拒绝 / a 本会话内总是允许（60 秒无应答自动拒绝）",
                    )
                    continue
                if isinstance(ev, RetryEvent):
                    self._append_message("msg-system", f"↻ {ev.reason}")
                    continue
                if isinstance(ev, UsageEvent):
                    self._append_message(
                        "msg-system",
                        f"tokens 用量：↑{ev.input_tokens} ↓{ev.output_tokens}"
                        f"（累计 ↑{ev.total_input} ↓{ev.total_output}）",
                    )
                    continue
                if isinstance(ev, HookEvent):
                    self._append_message("msg-system", f"[hook:{ev.event}] {ev.message}")
                    continue
                if isinstance(ev, ErrorEvent):
                    error_text = ev.message
                    continue
                if isinstance(ev, LoopComplete):
                    # 自然终止信号（C55）：按原因收尾（硬取消不走这里，N29）
                    if view is not None:
                        if ev.reason == "error" and error_text:
                            view.finalize(error_text)
                        else:
                            view.finalize()
                    elif error_text:
                        self._append_message("msg-error", error_text)
                    self._pending_permission = None  # 悬空请求清理（Future 悬空无害，N23）
                    break  # 生成器已自然终止
                if isinstance(ev, TurnComplete):
                    if view is not None:
                        view.finalize()  # 本轮块收尾
                    view = None
                    continue
                # 内容类事件：惰性建块后分流渲染
                if isinstance(ev, ThinkingText):
                    ensure_view().add_thinking(ev.text)
                elif isinstance(ev, StreamText):
                    ensure_view().add_text(ev.text)
                elif isinstance(ev, ToolCallStart):
                    ensure_view().tool_call_started(ev.id, ev.name)
                elif isinstance(ev, ToolCallDelta):
                    ensure_view().tool_call_delta(ev.id, ev.arguments)
                elif isinstance(ev, ToolUseEvent):
                    pass  # 参数已由 Start/Delta 流式呈现；本事件服务 headless 消费者
                elif isinstance(ev, ToolOutputDelta):
                    ensure_view().tool_output(ev.id, ev.text)
                elif isinstance(ev, ToolHeartbeat):
                    ensure_view().tool_heartbeat(ev.id, ev.elapsed)
                elif isinstance(ev, ToolResultEvent):
                    if (
                        self._pending_permission is not None
                        and ev.id == self._pending_permission.request_id
                    ):
                        # 请求已定局（允许后执行完 / 拒绝 / 超时），清理待确认态
                        self._pending_permission = None
                    ensure_view().tool_executed(ev.id, ev.name, ev.ok, ev.preview)
                elif isinstance(ev, Notice):
                    ensure_view().notice(ev.text)
        except asyncio.CancelledError:
            # 应用退出 / Esc：生成器硬取消路径（N29）——界面标记中断后继续传播
            if view is not None:
                try:
                    view.mark_interrupted()
                except Exception:  # noqa: BLE001 应用退出中 UI 可能已拆除
                    pass
            raise
        finally:
            self._streaming = False
