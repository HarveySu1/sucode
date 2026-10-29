"""工具注册中心（Phase 2 tasks T12 → 设计决策对齐修订）：

设计要点：
- 注册机制（C37）：dict 存储，支持 disable / enable 动态管理（禁用工具既不出现在
  给模型的工具列表里，也不可执行）
- 协议适配（C41）：get_all_schemas(protocol) 按参数区分 Anthropic / OpenAI 格式
- 工具披露（ch03 C53）：should_defer 的工具默认不出现在工具列表，须经 ToolSearch
  检索后 mark_discovered 才披露；披露状态仅运行期（O23）
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
        self._discovered: set[str] = set()  # 已披露的延迟工具（ch03 C53，运行期状态）

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

    # ---- 工具披露（spec(docs/ch03) C53）----

    def mark_discovered(self, name: str) -> None:
        """标记延迟工具已披露：下一轮 get_all_schemas 起进入工具列表（仅运行期）。"""
        if name not in self._tools:
            raise ToolError(f"未知工具「{name}」，无法披露")
        self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered

    def get_deferred_tool_names(self, include_discovered: bool = False) -> list[str]:
        """可披露的延迟工具名（默认只返回尚未披露的），供系统提示词告知可检索范围。"""
        return sorted(
            n
            for n, t in self._tools.items()
            if t.should_defer
            and n not in self._disabled
            and (include_discovered or n not in self._discovered)
        )

    def find_deferred_by_names(self, names: list[str]) -> list[Tool]:
        """按名精确选取延迟工具（对应 ToolSearch 的 select:Name1,Name2 形态）。"""
        found: list[Tool] = []
        for name in names:
            tool = self.find(name)
            if tool is not None and tool.should_defer:
                found.append(tool)
        return found

    def search_deferred(
        self, query: str, max_results: int = DEFAULT_DEFERRED_TOP_K
    ) -> list[Tool]:
        """按打分规则检索延迟工具，取前 max_results 个命中项（C53 承接值）。"""
        pool = [
            t for t in self._tools.values() if t.should_defer and t.name not in self._disabled
        ]
        scored = sorted(
            ((relevance_score(t, query), t) for t in pool),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [t for score, t in scored if score > 0][: max(0, max_results)]

    # ---- 双协议 API 工具列表格式适配（spec C41）----

    def get_all_schemas(self, protocol: str) -> list[dict]:
        """按当前 profile 协议给出工具列表（会话层每轮原样携带）。

        被禁用的工具不出现；延迟工具在披露前不出现（ch03 C53，不按提问打分自动暴露）。
        """
        if protocol not in ("anthropic", "openai"):
            raise ToolError(f"未知协议：{protocol}")
        tools = [
            t
            for t in self._tools.values()
            if t.name not in self._disabled and (not t.should_defer or t.name in self._discovered)
        ]
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
    """六件套默认注册（spec C17）；ToolSearch 由 cli 装配时按协议追加（C53/T46）。"""
    from sucode.tools.bash_tool import Bash
    from sucode.tools.file_tools import EditFile, ReadFile, WriteFile
    from sucode.tools.search_tools import Glob, Grep

    registry = ToolRegistry()
    for tool in (ReadFile(), WriteFile(), EditFile(), Glob(), Grep(), Bash()):
        registry.register(tool)
    return registry
