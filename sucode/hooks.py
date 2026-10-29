"""Hook 引擎（spec(docs/ch04) C61，T56）。

把 ch03 的 pre/post 工具两挂点（C49）扩展为八事件点全链路钩子：
session_start / turn_start / pre_send / post_receive / pre_tool_use / post_tool_use /
turn_end / session_end。

- 阻断语义：仅 pre_tool_use 可阻断——钩子返回 HookRejection 即否决该次工具调用
  （首个否决生效，工具不执行、按错误结果配对回灌）；其余挂点只观察，返回值一律忽略。
- 通知排出：钩子可经 engine.notify(message) 排队界面消息，Agent 在每个挂点执行后
  drain 出来转成 HookEvent 事件上浮（默认空引擎零噪音）。
- 代表性路径：infer_file_path 仅取 file_path → path 两个字段（对齐 mewcode N4 精简版，
  本章记 N27），只供 hook 匹配，不做全参数扫描。
- 本章只交付机制与内置接线；hook 配置文件加载不做（O 系留后续章节）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from sucode.tools.base import ToolResult

HookEventName = Literal[
    "session_start",
    "turn_start",
    "pre_send",
    "post_receive",
    "pre_tool_use",
    "post_tool_use",
    "turn_end",
    "session_end",
]

# 八挂点全部合法取值（运行期注册校验 + 测试断言用）
HOOK_EVENT_NAMES: tuple[HookEventName, ...] = (
    "session_start",
    "turn_start",
    "pre_send",
    "post_receive",
    "pre_tool_use",
    "post_tool_use",
    "turn_end",
    "session_end",
)


@dataclass
class HookRejection:
    """pre_tool_use 阻断结论：reason 直接拼进给模型的错误结果（"Hook rejected: ..."）。"""

    reason: str


@dataclass
class HookContext:
    """钩子执行上下文：挂点名 + 挂点相关字段（无关字段保持默认）。"""

    event: HookEventName
    tool_name: str = ""  # 工具挂点：工具名
    args: dict = field(default_factory=dict)  # 工具挂点：解析后的参数
    file_path: str = ""  # args 中推断出的代表性路径（N27：file_path → path）
    result: ToolResult | None = None  # post_tool_use：刚产生的结果（含否决/拒绝结果）
    round: int = 0  # turn_* 挂点：当前轮次（从 1 起）
    text: str = ""  # pre_send：本轮发出的用户/续写文本（恢复轮为续写指令）


Hook = Callable[[HookContext], Awaitable[HookRejection | None]]


def infer_file_path(args: dict) -> str:
    """从工具参数推断代表性路径（N27 精简）：仅 file_path / path 两字段，按序取首个非空。"""
    for key in ("file_path", "path"):
        value = args.get(key)
        if value:
            return str(value)
    return ""


class HookEngine:
    """八挂点钩子容器：默认空直通（零行为变化），通知队列由 Agent 逐挂点排出。"""

    def __init__(self) -> None:
        self._hooks: dict[str, list[Hook]] = {name: [] for name in HOOK_EVENT_NAMES}
        self._notifications: list[str] = []

    def register(self, event: HookEventName, hook: Hook) -> None:
        """注册一个挂点钩子（同挂点按注册顺序执行）。"""
        if event not in self._hooks:
            raise ValueError(f"未知挂点：{event}")
        self._hooks[event].append(hook)

    async def run_hooks(self, ctx: HookContext) -> HookRejection | None:
        """执行一个挂点的全部钩子；仅 pre_tool_use 消费 HookRejection（首个否决即返回）。"""
        for hook in self._hooks.get(ctx.event, ()):
            outcome = await hook(ctx)
            if ctx.event == "pre_tool_use" and outcome is not None:
                return outcome
        return None

    def notify(self, message: str) -> None:
        """钩子侧排队一条界面消息（Agent drain 后转成 HookEvent 事件）。"""
        if message:
            self._notifications.append(message)

    def drain_notifications(self) -> list[str]:
        """取出并清空通知队列（Agent 在每个挂点执行后调用）。"""
        pending = self._notifications
        self._notifications = []
        return pending
