"""OpenAI 协议实现（tasks.md T5；Phase 2 T16 扩展）：AsyncOpenAI 流式 + tool_calls 聚合。

思考展示：读取兼容端点的 reasoning_content 字段（如 deepseek-reasoner），
映射为 ThinkingDelta 事件；普通模型无该字段，自然不受影响。
带工具时序列化回传 reasoning_content（DeepSeek 硬性要求，spec(docs/ch02) C25）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import openai

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
    describe_rate_limit,
    serialize_messages,
)

# 上下文超限的常见报错关键词（OpenAI/DeepSeek 等 400 报文）
_CONTEXT_OVERFLOW_HINTS = ("context length", "maximum context", "too long", "reduce the length")


def _translate(exc: Exception, profile: ProfileConfig) -> ProviderError:
    """把 openai SDK 异常转译为分类的 ProviderError 子类（异常链由调用方保留）。

    注意：APITimeoutError 是 APIConnectionError 的子类，必须先判超时。
    """
    endpoint = profile.base_url or "官方端点（api.openai.com）"
    if isinstance(exc, openai.AuthenticationError):
        return ProviderAuthError("鉴权失败：api_key 无效或已过期，请检查配置")
    if isinstance(exc, openai.PermissionDeniedError):
        return ProviderAuthError("无权限：该 api_key 无权访问此模型或端点")
    if isinstance(exc, openai.NotFoundError):
        return ProviderError(
            f"接口或模型不存在：请检查 base_url 与 model（{endpoint} / {profile.model}）"
        )
    if isinstance(exc, openai.RateLimitError):
        return ProviderRateLimitError(describe_rate_limit(exc))
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError("请求超时：模型响应过慢，稍后重试")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(f"网络错误：无法连接 {endpoint}")
    if isinstance(exc, openai.BadRequestError):
        msg = str(exc).lower()
        if any(hint in msg for hint in _CONTEXT_OVERFLOW_HINTS):
            return ProviderContextOverflowError(
                "上下文超限：对话已超出模型上下文窗口，可用 /new 清空后重试"
            )
        return ProviderError(f"请求被拒绝（400）：{exc}")
    return ProviderError(f"调用失败：{type(exc).__name__}: {exc}")


class OpenAIProvider(BaseProvider):
    """基于 openai 官方 SDK 的异步流式实现；base_url 可指向任意兼容端点（spec C8）。"""

    def _build_client(self) -> openai.AsyncOpenAI:
        """base_url 留空时不传，由 SDK 走官方端点。

        timeout 120s：read 超时按「两个分片之间的间隔」计，正常流式远小于此。
        """
        kwargs: dict[str, object] = {"api_key": self.profile.api_key, "timeout": 120.0}
        if self.profile.base_url:
            kwargs["base_url"] = self.profile.base_url
        return openai.AsyncOpenAI(**kwargs)

    async def stream(
        self, history: list[Message], tools: list[dict] | None = None, system: str = ""
    ) -> AsyncIterator[StreamEvent]:
        """流式对话：正文/思考/工具调用增量逐个产出，正常结束产出 StreamDone。

        - 约定 max_tokens 而非 max_completion_tokens：兼容端点（spec C8）普遍只认前者
        - system 非空时在 messages 首位插入 {"role": "system"}（不进 Message 历史，C50）
        - tools 每轮原样携带；delta.tool_calls 按 index 聚合：首片带 id+name 发
          ToolCallStart，参数碎片按 id 发 ToolCallDelta（C20）
        - finish_reason=="tool_calls" 透传给会话层做编排判断
        """
        client = self._build_client()
        messages = serialize_messages("openai", history, has_tools=bool(tools))
        if system:
            messages = [{"role": "system", "content": system}, *messages]
        try:
            stream = await client.chat.completions.create(
                model=self.profile.model,
                messages=messages,
                max_tokens=self._effective_max_tokens(),  # 运行期覆写优先（ch04 C57）
                stream=True,
                stream_options={"include_usage": True},  # 末尾 chunk 带 usage（ch04 C59）
                **({"tools": tools} if tools else {}),
            )
            finish_reason = ""
            usage_input = 0
            usage_output = 0
            tool_states: dict[int, dict] = {}  # index → {id, name, started}
            async with stream:  # 任何退出路径下自动关闭连接
                async for chunk in stream:
                    # 用量捕获（ch04 C59，2026-10-30 修订）：usage 落点因端点而异——
                    # OpenAI 规范为末尾空 choices 的独立 chunk；DeepSeek 实测
                    # （deepseek-flash）骑在最后一个内容 chunk 上、无独立空 chunk。
                    # 逐 chunk 探测、非空即记（后到覆盖先到），两种形态通吃。
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        usage_input = int(getattr(usage, "prompt_tokens", 0) or 0)
                        usage_output = int(getattr(usage, "completion_tokens", 0) or 0)
                    if not chunk.choices:  # 末尾 chunk 可能只带 usage，无 choices
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    if not delta:
                        continue
                    # 首个 chunk 的 delta 只带 role，content 为 None，跳过
                    if delta.content:
                        yield TextDelta(delta.content)
                    # 兼容端点的思考字段（deepseek-reasoner 等）；普通模型无此字段
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield ThinkingDelta(reasoning)
                    # 工具调用：按 index 聚合（多工具交错时各自独立拼接）
                    for tc in delta.tool_calls or []:
                        st = tool_states.setdefault(
                            tc.index, {"id": "", "name": "", "started": False}
                        )
                        if tc.id:
                            st["id"] = tc.id
                        frag = ""
                        if tc.function:
                            if tc.function.name:
                                st["name"] = tc.function.name
                            frag = tc.function.arguments or ""
                        if not st["started"] and st["id"] and st["name"]:
                            st["started"] = True
                            yield ToolCallStart(id=st["id"], name=st["name"])
                        if frag:
                            yield ToolCallDelta(id=st["id"], arguments=frag)
            # 用量上报（ch04 C59）：末尾 usage chunk 汇总，StreamDone 之前产出一次
            if usage_input or usage_output:
                yield UsageUpdate(input_tokens=usage_input, output_tokens=usage_output)
            yield StreamDone(finish_reason)
        except openai.OpenAIError as exc:
            raise _translate(exc, self.profile) from exc
