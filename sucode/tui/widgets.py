"""TUI 渲染组件（tasks.md T8 → Phase 2 T18 → 修订 T25）：

Phase 2 修订（spec(docs/ch02) C30/C31/C32）：每个 LLM 轮次独立一个 AI 块
（TUI 收到 RoundStart 即新建），块内按时间展示：暗淡思考 → 工具行
（参数流式 + 输出片段 + 心跳计时，执行后 ✓/✗ 状态与摘要）→ Markdown 正文。
工具行复用正文同款 80ms 节流；块结束后整段重渲染保证 Markdown 完整。
"""

from __future__ import annotations

import time

from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, Static

THROTTLE_SECONDS = 0.08  # 节流间隔：流式期间最多每 80ms 刷新一次


class ToolLine(Static):
    """单个工具调用的状态行：
    ● 进行中（参数流式 + 输出片段 + 心跳计时）→ ✓ 完成 / ✗ 失败。
    """

    def __init__(self, tool_id: str, name: str) -> None:
        # 注意：Widget 自带只读 name 属性（DOMNode 节点名），工具名必须另取字段
        super().__init__("", classes="tool-line", markup=False)
        self.tool_id = tool_id
        self.tool_name = name
        self.args = ""
        self.output = ""  # 累积的增量输出片段（仅 Bash 产出，spec(docs/ch02) C30）
        self.elapsed = 0.0  # 已用秒数（心跳驱动，spec C31）；结束态即总耗时
        self._started_at = time.monotonic()  # 本地计时兜底（无心跳时用）
        self._got_heartbeat = False  # 是否收到过心跳（有则以其为准，精度更高）
        self.state = "running"  # running | done | error
        self.preview = ""
        self._dirty = True

    def add_args(self, fragment: str) -> None:
        self.args += fragment
        self._dirty = True

    def add_output(self, chunk: str) -> None:
        self.output += chunk
        self._dirty = True

    def heartbeat(self, elapsed: float) -> None:
        self.elapsed = elapsed
        self._got_heartbeat = True
        self._dirty = True

    def finish(self, ok: bool, preview: str) -> None:
        self.state = "done" if ok else "error"
        self.preview = preview
        # 总耗时：优先末次心跳值（会话层执行期计时，更精确）；从未收到心跳时用本地兜底
        if not self._got_heartbeat:
            self.elapsed = time.monotonic() - self._started_at
        self.set_classes(f"tool-line {'tool-ok' if ok else 'tool-err'}")
        self._dirty = True

    def flush(self) -> None:
        """把当前状态渲染成一行文本（由容器的节流机制驱动）。"""
        if not self._dirty:
            return
        self._dirty = False
        if self.state == "running":
            self.update(self._render_running())
        elif self.state == "done":
            self.update(f"✓ {self.tool_name} · {self.preview or '完成'}{self._elapsed_suffix()}")
        else:
            self.update(f"✗ {self.tool_name} · {self.preview or '失败'}{self._elapsed_suffix()}")

    def _elapsed_suffix(self) -> str:
        """耗时后缀（保留一位小数）：提示工具是否卡死（spec(docs/ch02) C31）。"""
        return f"（{self.elapsed:.1f}s）"

    def _render_running(self) -> str:
        """进行中一行：名称(参数) │ 最后一行输出 （已用 N.Ns）。"""
        shown = self.args if len(self.args) <= 80 else self.args[:77] + "…"
        line = f"● {self.tool_name}({shown})"
        out = self._last_output_line()
        if out:
            line += f" │ {out}"
        if self.elapsed > 0:  # 首次心跳前不显示，避免出现冻结的 0.0s
            line += f" {self._elapsed_suffix()}"
        return line

    def _last_output_line(self, limit: int = 60) -> str:
        """取输出中最后一行非空内容（超长截断），用于单行进度回显。"""
        for raw in reversed(self.output.splitlines()):
            text = raw.strip()
            if text:
                return text if len(text) <= limit else text[: limit - 1] + "…"
        return ""


class AIMessageView(Vertical):
    """一个 LLM 轮次的 AI 消息块：暗淡思考 + 工具行区 + Markdown 正文，支持流式增量更新。

    - add_thinking / add_text / tool_call_* / tool_output / tool_heartbeat：流式入口
    - finalize：本块结束（由 TUI 在下一轮 RoundStart 或流末尾调用）；错误时半截正文照常展示
    - 节流：缓冲分片，最多每 THROTTLE_SECONDS 刷一次，结束后整段重渲染一次保证 Markdown 完整
    """

    DEFAULT_CSS = """
    AIMessageView {
        height: auto;
        margin-top: 1;
    }
    AIMessageView .thinking {
        color: $text-muted;
        text-style: dim;
        margin-bottom: 1;
    }
    AIMessageView .tool-lines {
        height: auto;
        margin-bottom: 1;
    }
    AIMessageView .tool-line {
        color: $accent;
        text-style: dim;
        padding: 0 1;
    }
    AIMessageView .tool-ok {
        color: $success;
        text-style: none;
    }
    AIMessageView .tool-err {
        color: $error;
        text-style: bold;
    }
    AIMessageView .msg-notice {
        color: $text-muted;
        text-style: dim;
        margin-top: 0;
    }
    AIMessageView .msg-interrupted {
        color: $text-muted;
        text-style: dim;
        margin-top: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._thinking_parts: list[str] = []
        self._text_parts: list[str] = []
        self._pending_text = ""
        self._last_flush = 0.0
        self._finished = False
        self._final_error: str | None = None
        self._final_interrupted = False
        self._thinking_static: Static | None = None
        self._markdown: Markdown | None = None
        self._tool_lines_box: Vertical | None = None
        self._tool_lines: dict[str, ToolLine] = {}
        self._pending_notices: list[str] = []  # 未挂载期到达的提示行（兜底缓存）
        self.flush_count = 0  # 自测观察节流效果

    def compose(self):
        yield Static("", classes="thinking", markup=False)
        yield Vertical(classes="tool-lines")
        yield Markdown()

    def on_mount(self) -> None:
        self._thinking_static = self.query_one(".thinking", Static)
        self._thinking_static.display = False
        self._tool_lines_box = self.query_one(".tool-lines", Vertical)
        self._markdown = self.query_one(Markdown)
        # 兜底：流事件可能早于挂载周期到达（组件未就绪时被缓存）
        if self._finished:
            self._render_final()
        else:
            self._mount_pending()
            self._flush()

    # ---- 流式入口 ----
    def add_thinking(self, chunk: str) -> None:
        self._thinking_parts.append(chunk)
        self._maybe_flush()

    def add_text(self, chunk: str) -> None:
        self._text_parts.append(chunk)
        self._pending_text += chunk
        self._maybe_flush()

    def tool_call_started(self, tool_id: str, name: str) -> None:
        """工具调用开始：创建 ● 状态行（未挂载时缓存，on_mount 后补挂）。"""
        line = ToolLine(tool_id, name)
        self._tool_lines[tool_id] = line
        if self._tool_lines_box is not None:
            self._tool_lines_box.mount(line)
            self.scroll_visible(animate=False)

    def tool_call_delta(self, tool_id: str, fragment: str) -> None:
        line = self._tool_lines.get(tool_id)
        if line is not None:
            line.add_args(fragment)
            self._maybe_flush()

    def tool_output(self, tool_id: str, text: str) -> None:
        """工具增量输出片段（仅 Bash，spec(docs/ch02) C30）：追加到对应工具行并刷新。"""
        line = self._tool_lines.get(tool_id)
        if line is not None:
            line.add_output(text)
            self._maybe_flush()

    def tool_heartbeat(self, tool_id: str, elapsed: float) -> None:
        """工具执行心跳（spec C31）：更新对应工具行的已用秒数并刷新。"""
        line = self._tool_lines.get(tool_id)
        if line is not None:
            line.heartbeat(elapsed)
            self._maybe_flush()

    def tool_executed(self, tool_id: str, name: str, ok: bool, preview: str) -> None:
        line = self._tool_lines.get(tool_id)
        if line is not None:
            line.finish(ok, preview)
            self._maybe_flush()

    def notice(self, text: str) -> None:
        """块内暗淡提示行（如二轮再调用说明）；未挂载期缓存，挂载后补挂。"""
        self._pending_notices.append(text)
        self._mount_pending()
        self.scroll_visible(animate=False)

    def finalize(self, error: str | None = None) -> None:
        """流结束：最终整段重渲染；有错误则展示错误行（半截正文保留）。"""
        if self._finished:
            return
        self._finished = True
        self._final_error = error
        self._render_final()

    def mark_interrupted(self) -> None:
        """Esc 中断收尾：最终渲染 + 「已中断」标记行。"""
        if self._finished:
            return
        self._finished = True
        self._final_interrupted = True
        self._render_final()

    # ---- 内部 ----
    def _mount_pending(self) -> None:
        """把早于挂载到达的工具行/提示行补挂进容器（幂等，可重复调用）。"""
        if self._tool_lines_box is None:
            return
        for line in self._tool_lines.values():
            if line not in self._tool_lines_box.children:
                self._tool_lines_box.mount(line)
        while self._pending_notices:
            self._tool_lines_box.mount(
                Static(self._pending_notices.pop(0), classes="msg-notice", markup=False)
            )

    def _render_final(self) -> None:
        """最终渲染（finalize 与 on_mount 都可能触发；组件未就绪则延迟）。"""
        if self._markdown is None:
            return
        if self._thinking_static is not None and self._thinking_parts:
            self._thinking_static.display = True
            self._thinking_static.update("".join(self._thinking_parts))
        if self._tool_lines_box is not None:
            self._mount_pending()
            for line in self._tool_lines.values():
                line.flush()
        self._markdown.update("".join(self._text_parts))
        if self._final_interrupted:
            self.mount(Static("已中断", classes="msg-interrupted", markup=False))
        elif self._final_error:
            self.mount(Static(self._final_error, classes="msg-error", markup=False))
        self.scroll_visible(animate=False)

    def _maybe_flush(self) -> None:
        if self._finished:  # 防御：结束后不应再有流事件
            return
        now = time.monotonic()
        if now - self._last_flush >= THROTTLE_SECONDS:
            self._last_flush = now
            self._flush()

    def _flush(self) -> None:
        self.flush_count += 1
        if self._thinking_static is not None and self._thinking_parts:
            self._thinking_static.display = True
            self._thinking_static.update("".join(self._thinking_parts))
        if self._markdown is not None and self._pending_text:
            self._markdown.append(self._pending_text)
            self._pending_text = ""
        if self._tool_lines_box is not None:
            for line in self._tool_lines.values():
                line.flush()
        self.scroll_visible(animate=False)
