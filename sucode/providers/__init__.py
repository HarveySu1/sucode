"""Provider 层入口：统一接口导出 + 工厂（tasks.md T3 → ch03 T48 惰性导入）。

新增后端步骤（spec N3：其余层不动）：
1. 新建 xxx_provider.py 实现 BaseProvider
2. 在 _PROVIDERS 注册表登记 protocol → (模块名, 类名)

惰性加载（spec(docs/ch03) C54）：实现模块只在 get_provider 按 protocol 首次使用时
导入，未使用的协议 SDK（anthropic / openai）不进入启动路径。
"""

from __future__ import annotations

import importlib

from sucode.config import ProfileConfig
from sucode.providers.base import (
    BaseProvider,
    Message,
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
# protocol → (实现模块, 类名)；实现模块含对应 SDK，首次使用时才导入（C54）
_PROVIDERS: dict[str, tuple[str, str]] = {
    "anthropic": ("sucode.providers.anthropic_provider", "AnthropicProvider"),
    "openai": ("sucode.providers.openai_provider", "OpenAIProvider"),
}


def get_provider(profile: ProfileConfig) -> BaseProvider:
    """按 profile.protocol 返回 Provider 实例；未知 protocol 抛 ProviderError。"""
    entry = _PROVIDERS.get(profile.protocol)
    if entry is None:
        registered = "、".join(sorted(_PROVIDERS)) or "（无）"
        raise ProviderError(f"未注册的 protocol「{profile.protocol}」，已注册：{registered}")
    module_name, class_name = entry
    module = importlib.import_module(module_name)
    cls: type[BaseProvider] = getattr(module, class_name)
    return cls(profile)


__all__ = [
    "get_provider",
    "serialize_messages",
    "BaseProvider",
    "Message",
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
]
