"""Provider 层入口：统一接口导出 + 工厂（tasks.md T3）。

新增后端步骤（spec N3：其余层不动）：
1. 新建 xxx_provider.py 实现 BaseProvider
2. 在 _PROVIDERS 注册表登记 protocol 名
"""

from __future__ import annotations

from sucode.config import ProfileConfig
from sucode.providers.anthropic_provider import AnthropicProvider
from sucode.providers.base import (
    BaseProvider,
    Message,
    MessageStart,
    ProviderAuthError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderRateLimitError,
    StreamDone,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
    UsageUpdate,
    serialize_messages,
)
from sucode.providers.openai_provider import OpenAIProvider

# protocol → 实现类注册表
_PROVIDERS: dict[str, type[BaseProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def get_provider(profile: ProfileConfig) -> BaseProvider:
    """按 profile.protocol 返回 Provider 实例；未知 protocol 抛 ProviderError。"""
    cls = _PROVIDERS.get(profile.protocol)
    if cls is None:
        registered = "、".join(sorted(_PROVIDERS)) or "（无）"
        raise ProviderError(f"未注册的 protocol「{profile.protocol}」，已注册：{registered}")
    return cls(profile)


__all__ = [
    "get_provider",
    "serialize_messages",
    "BaseProvider",
    "Message",
    "MessageStart",
    "ProviderError",
    "ProviderAuthError",
    "ProviderRateLimitError",
    "ProviderContextOverflowError",
    "StreamDone",
    "StreamEvent",
    "TextDelta",
    "ThinkingDelta",
    "ToolCallStart",
    "ToolCallDelta",
    "UsageUpdate",
    "AnthropicProvider",
    "OpenAIProvider",
]
