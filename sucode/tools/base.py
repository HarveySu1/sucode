"""工具层基础（Phase 2 tasks T12 → 设计决策对齐修订）：

设计要点（spec(docs/ch02) C16/C19/N6，对齐设计表）：
- 元信息：name / description（写给模型看，中文）/ category（工具分类）/ should_defer（延迟加载）
- 参数校验（C34）：每个工具声明 Pydantic 参数模型，JSON Schema 由 model_json_schema 自动生成
- 工具分类（C35）：category 为 Literal["read", "write", "command"]，静态类型检查
- 结果传递（C36）：ToolResult dataclass，is_error 让模型自行处理失败
- 注册机制（C37）：ToolRegistry dict 存储，支持 disable / enable 动态管理
- 文件缓存（C38）：FileCache 线程安全字典，ReadFile 读时填充、EditFile/WriteFile 写后失效
- 工具披露（ch03 C53）：should_defer 类属性声明延迟披露；披露状态与检索由注册中心负责
  （不再按提问打分自动暴露，改由模型显式调用 ToolSearch 检索后 mark_discovered）
- 并发判定（ch03 C45）：is_concurrency_safe 为静态类属性，声明式、不读入参
- execute 成功返回输出文本，失败抛 ToolError；任何失败都转结构化 ToolResult 回传模型，
  CancelledError 不吞——Esc 取消必须能穿透到会话层做配对收尾
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

# 单个工具结果输出上限（spec N6）；超出保留头尾各半并注明
MAX_OUTPUT_CHARS = 30_000

# 延迟工具检索最多返回的数量（C53 承接值）
DEFAULT_DEFERRED_TOP_K = 3

# 增量输出回调（spec(docs/ch02) C30）：工具执行中同步调用，不得阻塞；会话层收集后转事件
Emit = Callable[[str], None]

# 工具分类（spec C35；ch04 C62 扩展 task）：静态类型检查，权限确认/计划拦截按此分级——
# write 恒确认、command 白名单外确认、read 与 task 不确认（task = 智能体协作状态，
# 不改用户文件系统，风险等级同 read，计划模式亦放行）
Category = Literal["read", "write", "command", "task"]

# 延迟工具检索打分用：抽取英文/数字标识符（工具名与描述以英文标识符为主，中文按整词包含判定）
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class ToolError(Exception):
    """工具执行失败；message 会作为结构化错误文案回传给模型，须可读。"""


@dataclass
class ToolResult:
    """工具执行的结构化结果（spec C36）：is_error=True 时 output 即给模型看的错误说明。"""

    output: str
    is_error: bool = False


class FileCache:
    """线程安全的文件内容缓存（spec C38）。

    以「路径 + mtime_ns」为键：命中且 mtime 未变才复用，避免陈旧内容。
    ReadFile 读时填充；EditFile / WriteFile 写后失效。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[int, str]] = {}

    def get(self, path: Path, mtime_ns: int) -> str | None:
        with self._lock:
            hit = self._data.get(str(path))
        if hit is not None and hit[0] == mtime_ns:
            return hit[1]
        return None

    def put(self, path: Path, mtime_ns: int, text: str) -> None:
        with self._lock:
            self._data[str(path)] = (mtime_ns, text)

    def invalidate(self, path: Path) -> None:
        with self._lock:
            self._data.pop(str(path), None)


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """超限截断：保留头尾各一半，中间注明省略量。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return (
        text[:half]
        + f"\n…[输出超出 {limit} 字符上限，中间省略 {omitted} 字符]…\n"
        + text[-half:]
    )


def relevance_score(tool: "Tool", query: str) -> int:
    """延迟工具检索打分（spec(docs/ch03) C53）：名中 +10 / 描述中 +5 / 分词 +3/+1。

    检索词整体命中工具名 +10、命中描述 +5；分词（英文/数字标识符）命中名字 +3、
    命中描述 +1。中文无分词器，靠整体命中描述判定。
    """
    q = (query or "").strip().lower()
    if not q:
        return 0
    name = tool.name.lower()
    desc = tool.description.lower()
    score = 0
    if q in name:
        score += 10
    if q in desc:
        score += 5
    for token in {t for t in _TOKEN_RE.findall(q) if len(t) >= 2}:
        if token in name:
            score += 3
        elif token in desc:
            score += 1
    return score


class Tool(ABC):
    """统一工具接口：元信息 + Pydantic 参数模型 + 异步执行（spec C16）。"""

    name: str = ""
    description: str = ""  # 写给模型看：这个工具做什么、什么时候用
    category: Category = "read"  # 工具分类（spec C35）
    should_defer: bool = False  # 延迟披露标记：True 则须经 ToolSearch 披露后才出现在工具列表（ch03 C53）
    is_concurrency_safe: bool = False  # 能否并发执行（ch03 C45）：静态声明，不读入参；默认串行
    timeout: float = 60.0  # execute 硬超时（Bash 按自身 timeout_seconds 调大）
    Params: type[BaseModel] | None = None  # 参数模型；Schema 由其自动生成（spec C34）

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行并返回给模型的输出文本；失败抛 ToolError（由统一包装转结构化结果）。"""

    async def execute_streaming(self, emit: Emit | None, **kwargs) -> str:
        """可选流式执行入口（spec(docs/ch02) C30）。

        默认实现直接退化为 execute：非流式工具零改动即获得统一执行路径与心跳（N13）。
        需要增量输出的工具（Bash）覆写本方法，在读取到片段时调用 emit 回传。
        """
        return await self.execute(**kwargs)

    @classmethod
    def get_schema(cls) -> dict:
        """工具的 JSON Schema：由 Pydantic 参数模型自动生成（spec C34）。"""
        if cls.Params is None:
            return {"type": "object", "properties": {}}
        return cls.Params.model_json_schema()


def _format_validation(exc: ValidationError) -> str:
    """把 Pydantic 校验错误压成一行可读文案，便于模型自愈重试。"""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(根)"
        parts.append(f"{loc}: {err.get('msg', '')}")
    return "；".join(parts)


def parse_arguments(tool: Tool, arguments_json: str) -> dict:
    """参数解析 + Pydantic 校验（spec C34）。

    空串归一空对象（Anthropic 空参工具零 delta，spec N8）；JSON 不合法或校验不过
    都转 ToolError，由统一包装回传给模型。
    """
    raw = (arguments_json or "").strip()
    data: dict = {}
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolError(f"参数不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ToolError("参数应为 JSON 对象（键值映射）")
    if tool.Params is None:
        return data
    try:
        return tool.Params.model_validate(data).model_dump()
    except ValidationError as exc:
        raise ToolError(f"参数校验失败：{_format_validation(exc)}") from exc


async def safe_execute(tool: Tool, args: dict, emit: Emit | None = None) -> ToolResult:
    """单工具执行保护：超时与任何异常都转结构化结果；CancelledError 穿透。

    走 execute_streaming（默认退化到 execute），emit 供覆写工具回传增量输出（C30）。
    """
    try:
        output = await asyncio.wait_for(
            tool.execute_streaming(emit, **args), timeout=tool.timeout
        )
    except asyncio.TimeoutError:
        return ToolResult(f"错误：工具执行超时（{tool.timeout:.0f}s）已终止", is_error=True)
    except ToolError as exc:
        return ToolResult(f"错误：{exc}", is_error=True)
    except asyncio.CancelledError:
        raise  # Esc 取消：穿透到会话层做配对收尾
    except Exception as exc:  # noqa: BLE001 工具实现不可拖垮整个会话
        return ToolResult(f"错误：{type(exc).__name__}: {exc}", is_error=True)
    return ToolResult(truncate_output(str(output)))
