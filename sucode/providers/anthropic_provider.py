"""Anthropic 协议实现（tasks.md T4；Phase 2 T15 扩展）：AsyncAnthropic 流式 + extended thinking + tool_use。

多轮历史序列化经 serialize_messages 分叉：工具轮回传 thinking+signature（C25）、
tool_use/tool_result 内容块数组（N8）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import anthropic

from sucode.config import ProfileConfig
from sucode.providers.base import (
    BaseProvider,
    Message,
    ProviderAuthError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderRateLimitError,
    SignatureDelta,
    StreamDone,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
    UsageUpdate,
    describe_rate_limit,
    serialize_messages,
)

# 上下文超限的常见报错关键词（Anthropic 400 报文）
_CONTEXT_OVERFLOW_HINTS = ("context length", "maximum context", "prompt is too long", "too long")


def _translate(exc: Exception, profile: ProfileConfig) -> ProviderError:
    """把 anthropic SDK 异常转译为分类的 ProviderError 子类（异常链由调用方保留）。

    注意：APITimeoutError 是 APIConnectionError 的子类，必须先判超时。
    """
    endpoint = profile.base_url or "官方端点（api.anthropic.com）"
    if isinstance(exc, anthropic.AuthenticationError):
        return ProviderAuthError("鉴权失败：api_key 无效或已过期，请检查配置")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ProviderAuthError("无权限：该 api_key 无权访问此模型或端点")
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError(
            f"接口或模型不存在：请检查 base_url 与 model（{endpoint} / {profile.model}）"
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderRateLimitError(describe_rate_limit(exc))
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError("请求超时：模型响应过慢，稍后重试")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(f"网络错误：无法连接 {endpoint}")
    if isinstance(exc, anthropic.BadRequestError):
        msg = str(exc).lower()
        if any(hint in msg for hint in _CONTEXT_OVERFLOW_HINTS):
            return ProviderContextOverflowError(
                "上下文超限：对话已超出模型上下文窗口，可用 /new 清空后重试"
            )
        return ProviderError(f"请求被拒绝（400）：{exc}")
    return ProviderError(f"调用失败：{type(exc).__name__}: {exc}")


class AnthropicProvider(BaseProvider):
    """基于 anthropic 官方 SDK 的异步流式实现，透出 ThinkingDelta / TextDelta。"""

    def _build_client(self) -> anthropic.AsyncAnthropic:
        """base_url 留空时不传，由 SDK 走官方端点。

        timeout 120s：read 超时按「两个分片之间的间隔」计，正常流式远小于此。
        """
        kwargs: dict[str, object] = {"api_key": self.profile.api_key, "timeout": 120.0}
        if self.profile.base_url:
            kwargs["base_url"] = self.profile.base_url
        return anthropic.AsyncAnthropic(**kwargs)

    async def stream(
        self, history: list[Message], tools: list[dict] | None = None, system: str = ""
    ) -> AsyncIterator[StreamEvent]:
        """流式对话：思考/签名/正文/工具调用增量逐个产出，正常结束产出 StreamDone。

        - thinking 开启时不携带 temperature（API 硬性约束）
        - tools 每轮原样携带（历史含 tool_use 后必须带，且保持逐轮一致）
        - system 非空时走顶层 system= 参数（不进 messages，零历史污染，C50）
        - 工具参数经 input_json_delta 碎片到达；签名经块尾 signature_delta 捕获（C25）
        - async with 保证流在任何退出路径下自动关闭
        """
        client = self._build_client()
        params: dict = {
            "model": self.profile.model,
            "max_tokens": self._effective_max_tokens(),  # 运行期覆写优先（ch04 C57）
            "messages": serialize_messages("anthropic", history, has_tools=bool(tools)),
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools
        if self.profile.thinking_enabled:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.profile.thinking_budget,
            }
        try:
            finish_reason = ""
            tool_ids: dict[int, str] = {}  # 块 index → 工具 id（input_json_delta 不带 id）
            async with client.messages.stream(**params) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if getattr(block, "type", "") == "tool_use":
                            tool_ids[event.index] = block.id
                            yield ToolCallStart(id=block.id, name=block.name)
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        dtype = getattr(delta, "type", "")
                        if dtype == "thinking_delta":
                            yield ThinkingDelta(delta.thinking)
                        elif dtype == "signature_delta":
                            yield SignatureDelta(delta.signature)
                        elif dtype == "text_delta":
                            yield TextDelta(delta.text)
                        elif dtype == "input_json_delta" and delta.partial_json:
                            yield ToolCallDelta(
                                id=tool_ids.get(event.index, ""), arguments=delta.partial_json
                            )
                    elif event.type == "message_delta" and event.delta.stop_reason:
                        finish_reason = event.delta.stop_reason
            # 用量上报（ch04 C59）：最终消息携带 usage，StreamDone 之前产出一次
            final = await stream.get_final_message()
            usage = getattr(final, "usage", None)
            if usage is not None:
                yield UsageUpdate(
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                )
            yield StreamDone(finish_reason)
        except anthropic.AnthropicError as exc:
            raise _translate(exc, self.profile) from exc
