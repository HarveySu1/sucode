"""延迟工具披露检索（spec(docs/ch03) C53 / tasks T45）。

模型在系统提示词的「可用 ToolSearch 检索的延迟工具」名单里看到候选后调用本工具：
- `select:Name1,Name2`：按名精确选取
- 其他文本：按打分规则检索（名中 +10 / 描述中 +5 / 分词 +3/+1，取前 max_results 个）
命中即逐个 mark_discovered（运行期状态），从下一轮起该工具进入 get_all_schemas 列表；
未命中回退列出全部可披露工具名，供模型改选。

本工具自身不延迟披露（should_defer=False）、属只读类（category="read"），
执行无副作用（仅改内存披露集合），因此既不需要权限确认，也不被计划模式拦截。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from sucode.tools.base import DEFAULT_DEFERRED_TOP_K, Tool, ToolError

if TYPE_CHECKING:  # 仅类型引用，避免与注册中心循环导入
    from sucode.tools import ToolRegistry

SELECT_PREFIX = "select:"


class _ToolSearchParams(BaseModel):
    query: str = Field(
        description=(
            "检索词。精确选取用 select:Name1,Name2（逗号分隔工具名）；"
            "否则按关键词检索工具名与描述。"
        )
    )
    max_results: int = Field(
        default=DEFAULT_DEFERRED_TOP_K,
        ge=1,
        le=10,
        description="关键词检索最多返回的候选数（默认 3）。",
    )


class ToolSearchTool(Tool):
    name = "ToolSearch"
    description = (
        "检索并披露延迟加载的工具定义。工具列表只展示已披露的工具；"
        "当系统提示词列出「可用 ToolSearch 检索的延迟工具」时，用本工具获取其完整参数定义。"
        "query 传 select:Name1,Name2 可精确选取，或直接给关键词检索名称与描述；"
        "命中后该工具从下一轮起即可直接调用，未命中会返回全部可披露工具名。"
    )
    category = "read"
    should_defer = False
    Params = _ToolSearchParams

    def __init__(self, registry: ToolRegistry, protocol: str) -> None:
        self._registry = registry
        self._protocol = protocol

    async def execute(self, query: str = "", max_results: int = DEFAULT_DEFERRED_TOP_K) -> str:
        text = (query or "").strip()
        if not text:
            raise ToolError("缺少参数 query（要检索的工具名或关键词）")
        if text.lower().startswith(SELECT_PREFIX):
            names = [n.strip() for n in text[len(SELECT_PREFIX):].split(",") if n.strip()]
            tools = self._registry.find_deferred_by_names(names)
        else:
            tools = self._registry.search_deferred(text, max_results=max_results)
        if not tools:
            return self._fallback(text)
        for tool in tools:
            self._registry.mark_discovered(tool.name)
        payload = [self._schema_payload(tool) for tool in tools]
        return f"Found {len(tools)} tool(s):\n" + json.dumps(payload, ensure_ascii=False, indent=2)

    def _schema_payload(self, tool: Tool) -> dict:
        """按会话协议给出与工具列表同构的 schema，方便模型直接照参调用。"""
        if self._protocol == "openai":
            return {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.get_schema(),
                },
            }
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.get_schema(),
        }

    def _fallback(self, query: str) -> str:
        """未命中回退：列出全部可披露工具名（含已披露）。"""
        names = self._registry.get_deferred_tool_names(include_discovered=True)
        if not names:
            return f'No matching deferred tools for "{query}". Available: (none)'
        return f'No matching deferred tools for "{query}". Available: {", ".join(names)}'
