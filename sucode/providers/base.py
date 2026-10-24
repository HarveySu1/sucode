"""Provider 层抽象：统一流式对话接口与事件集（Phase 1 T3 → Phase 2 T14 扩展）。

接口契约：
- 输入：完整对话历史（Message 列表）+ 工具列表（可空，双协议格式由注册中心转换）
- 输出：异步流事件迭代器（见 StreamEvent）
- 失败：网络/鉴权/超上下文等一律转译为 ProviderError 子类抛出

Phase 2 扩展（spec(docs/ch02) C20/C25/N8）：
- Message：tool_calls（assistant 发起）/ tool_results（挂 user 消息）/
  thinking_signature（Anthropic 工具轮签名回传）
- serialize_messages 双协议分叉：anthropic 内容块数组（thinking 仅最后工具轮、
  空正文省略 text 块、tool_result 先于正文、空参数归一空对象）；openai tool_calls
  展开 + 带 has_tools 时回传 reasoning_content（DeepSeek 硬性要求）
- 新事件：SignatureDelta / ToolExecuted / Notice

Phase 2 修订扩展（spec(docs/ch02) C27/C30/C31/C32）：
- 新事件：RoundStart（轮次开始，TUI 开新块）/ ToolOutputDelta（工具增量输出）/
  ToolHeartbeat（工具执行心跳，含已用秒数）

Phase 3 修订扩展（spec(docs/ch03) C51，2026-10-21）：
- 新事件：PermissionRequest（风险操作权限确认请求，应答经同进程 Future 传回）
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

from sucode.config import ProfileConfig


# ---- 异常分类（4 种自定义异常，SDK 异常映射 + 异常链保留在各实现中） ----


class ProviderError(Exception):
    """Provider 执行失败基类；message 面向用户可读。"""


class ProviderAuthError(ProviderError):
    """鉴权/权限失败（401/403）。"""


class ProviderRateLimitError(ProviderError):
    """触发限流或账户额度不足（429）。"""


class ProviderContextOverflowError(ProviderError):
    """对话超出模型上下文窗口（400 家族）。"""


# 429 报文关键词：先判「额度不足」（更可操作），再判「频次限制」，都不命中则并列提示
_RATE_LIMIT_QUOTA_HINTS = ("quota", "insufficient", "balance", "billing", "arrears", "额度不足", "欠费")
_RATE_LIMIT_PACE_HINTS = ("rate limit", "requests per", "rpm", "tpm", "too many requests", "frequency", "频繁")


def describe_rate_limit(exc: Exception) -> str:
    """429 异常 → 可区分文案：额度不足 / 请求过于频繁 / 无法判定（两者并列）。

    固定前缀「HTTP 429」，便于用户区分**服务端限流**与本地手工上限（如工具轮数上限）。
    """
    raw = f"{getattr(exc, 'message', '') or exc}".lower()
    if any(hint in raw for hint in _RATE_LIMIT_QUOTA_HINTS):
        reason = "账户额度不足（请检查余额或套餐）"
    elif any(hint in raw for hint in _RATE_LIMIT_PACE_HINTS):
        reason = "请求过于频繁"
    else:
        reason = "请求过于频繁或账户额度不足"
    return f"触发限流（HTTP 429）：{reason}，稍后重试"


# ---- 工具调用结构（Phase 2 启用） ----


@dataclass
class ToolCall:
    """assistant 发起的工具调用；arguments 为原始 JSON 字符串（空串=无参）。"""

    id: str
    name: str
    arguments: str = ""


@dataclass
class ToolResultBlock:
    """工具结果块；挂在 role="user" 的 Message 上（保持内部 user/assistant 交替）。"""

    tool_call_id: str
    content: str
    is_error: bool = False


# ---- 消息模型（Phase 2 全面启用预留字段） ----


@dataclass
class Message:
    """会话消息：会话层与 Provider 层共享的结构。"""

    role: str  # "user" | "assistant"（tool_results 挂 user 消息上，序列化时按协议展开）
    content: str = ""  # 正文（assistant 被中断时为半截内容）
    thinking: str = ""  # 思考文本（anthropic thinking / openai 兼容端 reasoning_content）
    thinking_signature: str = ""  # Anthropic 签名：工具轮回传 thinking 时必须携带（C25）
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant 发起的工具调用
    tool_results: list[ToolResultBlock] = field(default_factory=list)  # 工具结果（仅 user 消息）


# ---- 事件类型 ----


@dataclass
class ThinkingDelta:
    """思考片段（Anthropic extended thinking；OpenAI 兼容端 reasoning_content，见 spec C6）。"""

    text: str


@dataclass
class SignatureDelta:
    """思考块签名增量（Anthropic 工具场景回传 thinking 必需，spec(docs/ch02) C25）。"""

    signature: str


@dataclass
class TextDelta:
    """正文片段。"""

    text: str


@dataclass
class StreamDone:
    """流正常结束（finish_reason：stop / length / tool_use / tool_calls 等）。"""

    finish_reason: str = ""


@dataclass
class MessageStart:
    """消息开始（保留，当前无生产者）。"""

    role: str = "assistant"


@dataclass
class UsageUpdate:
    """用量更新（保留；spec O4 不展示）。"""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ToolCallStart:
    """工具调用开始：参数经 ToolCallDelta 流式聚合。"""

    id: str = ""
    name: str = ""


@dataclass
class ToolCallDelta:
    """工具调用参数增量（JSON 碎片，会话层拼接）。"""

    id: str = ""
    arguments: str = ""


@dataclass
class ToolExecuted:
    """单个工具执行完成（会话层产出，供 TUI 更新 ✓/✗ 状态行）。"""

    id: str = ""
    name: str = ""
    ok: bool = False
    preview: str = ""


@dataclass
class Notice:
    """会话层提示（如已达工具轮数上限），TUI 以暗淡行展示。"""

    text: str


@dataclass
class RoundStart:
    """一轮 LLM 流开始（spec(docs/ch02) C32）：TUI 据此新建一个 AI 消息块。"""

    round: int = 0  # 从 1 起的轮次序号


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


# ---- 编排层事件（Phase 3 T30，spec(docs/ch03) C43/N19） ----

# 终止原因枚举（checklist 承接值）：正常完成 / 用户中断 / 取消信号 / 错误 / 达轮数上限
FinishReason = Literal["completed", "interrupted", "cancelled", "error", "max_rounds"]


@dataclass
class UserMessage:
    """用户消息事件：本轮提问文本（供未来非 TUI 消费者，TUI 已自行渲染）。"""

    text: str


@dataclass
class RunError:
    """编排错误事件：可读错误文案；后随 RunFinished(reason="error")。"""

    message: str


@dataclass
class RunFinished:
    """编排终止事件：通道内唯一结束信号（后台任务内一切异常转事件，不跨任务抛）。"""

    reason: FinishReason = "completed"


@dataclass
class PermissionRequest:
    """权限确认请求（spec(docs/ch03) C51，2026-10-21 修订新增）。

    风险操作（写类 / 白名单外命令）执行前由编排器发出；future 为同进程应答载体——
    消费者调 Agent.resolve_permission(request_id, verdict) 置结果（N23）。
    """

    request_id: str
    tool_name: str
    summary: str = ""
    future: asyncio.Future[str] | None = None  # 应答值：allow / deny / always


StreamEvent = (
    ThinkingDelta
    | SignatureDelta
    | TextDelta
    | StreamDone
    | MessageStart
    | UsageUpdate
    | ToolCallStart
    | ToolCallDelta
    | ToolExecuted
    | Notice
    | RoundStart
    | ToolOutputDelta
    | ToolHeartbeat
    | UserMessage
    | RunError
    | RunFinished
    | PermissionRequest
)


# ---- 序列化：双协议分叉（Phase 2 分叉点落地，spec(docs/ch02) N8） ----


def _parse_tool_input(arguments: str) -> dict:
    """工具参数 JSON → 对象；空串/解析失败归一空对象（不让坏参数拖垮请求）。"""
    raw = (arguments or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _serialize_anthropic(history: list[Message]) -> list[dict]:
    """anthropic：内容块数组。三条硬规则（缺一即 400）：
    - 空 text 块必须省略（content_block text 非空约束）
    - thinking+signature 仅回传最后一条含 tool_calls 的 assistant（更早剥离省 token）
    - user 消息中 tool_result 块先于正文 text 块
    """
    last_tool_assistant = max(
        (i for i, m in enumerate(history) if m.role == "assistant" and m.tool_calls),
        default=-1,
    )
    out: list[dict] = []
    for i, m in enumerate(history):
        if m.role == "assistant":
            blocks: list[dict] = []
            if m.thinking and i == last_tool_assistant and m.thinking_signature:
                blocks.append(
                    {"type": "thinking", "thinking": m.thinking, "signature": m.thinking_signature}
                )
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": _parse_tool_input(tc.arguments),
                    }
                )
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(空)"}]})
        else:
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": tr.tool_call_id,
                    "content": tr.content,
                    "is_error": tr.is_error,
                }
                for tr in m.tool_results
            ]
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            out.append({"role": "user", "content": blocks or [{"type": "text", "text": "(空)"}]})
    return out


def _serialize_openai(history: list[Message], has_tools: bool) -> list[dict]:
    """openai：user(tool_results) 展开为 N 条 role=tool 消息；
    带 has_tools 时 assistant 回传 reasoning_content（DeepSeek 硬性要求，C25）。
    """
    out: list[dict] = []
    for m in history:
        if m.role == "assistant":
            content = m.content or None
            if not content and not m.tool_calls:
                # 空正文且无工具调用（如二轮被剥离 tool_calls 后模型未输出正文）：
                # DeepSeek 等端点要求 assistant 必须有 content 或 tool_calls，兜底占位
                content = "(空)"
            msg: dict = {"role": "assistant", "content": content}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
                    }
                    for tc in m.tool_calls
                ]
            if has_tools and m.thinking:
                msg["reasoning_content"] = m.thinking
            out.append(msg)
        else:
            for tr in m.tool_results:
                out.append({"role": "tool", "tool_call_id": tr.tool_call_id, "content": tr.content})
            # 有正文 → 追加 user 文本；纯 tool_results 消息不追加（历史交替保持给 anthropic 用）
            if m.content or not m.tool_results:
                out.append({"role": "user", "content": m.content})
    return out


def serialize_messages(
    protocol: str, history: list[Message], has_tools: bool = False
) -> list[dict]:
    """会话历史 → 协议消息数组（双协议内部分发）。

    has_tools：本次请求是否携带工具定义（决定 openai 端 reasoning_content 回传）。
    """
    if protocol == "anthropic":
        return _serialize_anthropic(history)
    if protocol == "openai":
        return _serialize_openai(history, has_tools)
    raise ProviderError(f"未知协议：{protocol}")


class BaseProvider(ABC):
    """流式对话 Provider 统一接口；新增后端只需子类化并在工厂注册（spec N3）。"""

    def __init__(self, profile: ProfileConfig) -> None:
        self.profile = profile

    @abstractmethod
    def stream(
        self, history: list[Message], tools: list[dict] | None = None, system: str = ""
    ) -> AsyncIterator[StreamEvent]:
        """输入完整对话历史与工具列表（可空），异步逐个产出流事件；失败抛 ProviderError 子类。

        约定：不修改传入的 history；末尾消息为最新一条用户输入；
        tools 由注册中心按协议转换，每轮原样携带（历史含 tool_use 后必须带）。
        system 为会话级系统提示词（spec(docs/ch03) C50）：空串不携带；
        只进请求体、不进 Message 历史（零污染，N22）。
        """
