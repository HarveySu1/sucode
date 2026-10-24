"""工具注册中心（Phase 2 tasks T12 → 设计决策对齐修订）：

设计要点：
- 注册机制（C37）：dict 存储，支持 disable / enable 动态管理（禁用工具既不出现在
  给模型的工具列表里，也不可执行）
- 协议适配（C41）：get_all_schemas(protocol) 按参数区分 Anthropic / OpenAI 格式
- 延迟加载（C40）：should_defer 的工具按当前提问打分后择优暴露（默认六工具均不延迟）
- 统一执行入口 run()：任何失败路径都转结构化 ToolResult，永不抛出除取消外的异常
"""

from __future__ import annotations

from sucode.tools.base import (
    DEFAULT_DEFERRED_TOP_K,
    Emit,
    Tool,
    ToolError,
    ToolResult,
    parse_arguments,
    relevance_score,
    safe_execute,
)


class ToolRegistry:
    """集中登记工具；会话层按名查找并经 run() 统一执行（spec C18/C19/C37）。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ToolError("工具缺少 name，无法注册")
        if tool.name in self._tools:
            raise ToolError(f"工具名重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            available = "、".join(sorted(self._tools)) or "（无）"
            raise ToolError(f"未知工具「{name}」，可用工具：{available}")
        if name in self._disabled:
            raise ToolError(f"工具「{name}」已被禁用，当前不可执行")
        return tool

    def find(self, name: str) -> Tool | None:
        """按名查找但不抛错（spec(docs/ch03) C45）：不存在 / 已禁用返回 None（判不安全）。"""
        tool = self._tools.get(name)
        if tool is None or name in self._disabled:
            return None
        return tool

    # ---- 动态管理（spec C37）----

    def disable(self, name: str) -> None:
        """禁用工具：不再暴露给模型，也不可执行（不影响已登记状态）。"""
        if name not in self._tools:
            raise ToolError(f"未知工具「{name}」，无法禁用")
        self._disabled.add(name)

    def enable(self, name: str) -> None:
        """恢复被禁用的工具。"""
        if name not in self._tools:
            raise ToolError(f"未知工具「{name}」，无法启用")
        self._disabled.discard(name)

    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def names(self, include_disabled: bool = False) -> list[str]:
        return sorted(n for n in self._tools if include_disabled or n not in self._disabled)

    # ---- 双协议 API 工具列表格式适配（spec C41）----

    def get_all_schemas(
        self,
        protocol: str,
        query: str = "",
        top_k: int = DEFAULT_DEFERRED_TOP_K,
    ) -> list[dict]:
        """按当前 profile 协议给出工具列表（会话层每轮原样携带）。

        - query：当前提问，用于延迟工具打分（spec C40）
        - 被禁用的工具不出现；should_defer 的工具按打分择优保留
        """
        if protocol not in ("anthropic", "openai"):
            raise ToolError(f"未知协议：{protocol}")
        tools = self._select_tools(query, top_k)
        if protocol == "anthropic":
            return [
                {"name": t.name, "description": t.description, "input_schema": t.get_schema()}
                for t in tools
            ]
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.get_schema()},
            }
            for t in tools
        ]

    def _select_tools(self, query: str, top_k: int) -> list[Tool]:
        """可用工具选择：非延迟工具全给；延迟工具按提问打分取前 top_k（spec C40）。"""
        enabled = [t for t in self._tools.values() if t.name not in self._disabled]
        eager = [t for t in enabled if not t.should_defer]
        deferred = [t for t in enabled if t.should_defer]
        if not deferred:
            return eager
        scored = sorted(
            ((relevance_score(t, query), t) for t in deferred),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return eager + [t for score, t in scored[:top_k] if score > 0]

    # ---- 统一执行入口 ----

    async def run(
        self, name: str, arguments_json: str, emit: Emit | None = None
    ) -> ToolResult:
        """执行保护总入口：未知工具/禁用/坏 JSON/校验失败/异常/超时 → 结构化 ToolResult。

        emit（spec(docs/ch02) C30）：增量输出回调用，透传给覆写 execute_streaming 的工具。
        CancelledError 唯一例外：穿透给会话层做中断配对收尾（spec C24）。
        """
        try:
            tool = self.get(name)
            args = parse_arguments(tool, arguments_json)
        except ToolError as exc:
            return ToolResult(f"错误：{exc}", is_error=True)
        return await safe_execute(tool, args, emit)


def create_default_registry() -> ToolRegistry:
    """六件套默认注册（spec C17）。"""
    from sucode.tools.file_tools import EditFile, ReadFile, WriteFile
    from sucode.tools.search_tools import Glob, Grep
    from sucode.tools.bash_tool import Bash

    registry = ToolRegistry()
    for tool in (ReadFile(), WriteFile(), EditFile(), Glob(), Grep(), Bash()):
        registry.register(tool)
    return registry
