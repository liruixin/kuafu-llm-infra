"""OpenAI Chat Completions 协议适配器（兼容 DeepSeek、MiniMax 等同协议端点）。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from ..types import TokenUsage
from .base import BaseProvider, StreamChunk, ToolCall, ToolCallFunction


def _usage_from(usage: Any) -> Optional[TokenUsage]:
    """从 SDK usage 对象提取 TokenUsage，没有则返回 None。"""
    if not usage:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
    return TokenUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        cached_tokens=cached,
    )


class OpenAIProvider(BaseProvider):

    @staticmethod
    def _convert_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """tool_calls 只留 OpenAI 标准字段，扔掉别家的 thought_signature（bytes，序列化会报错）。"""
        converted = []
        for msg in messages:
            if not msg.get("tool_calls"):
                converted.append(msg)
                continue
            converted.append({**msg, "tool_calls": [
                {"id": tc.get("id", ""), "type": tc.get("type", "function"), "function": tc.get("function", {})}
                for tc in msg["tool_calls"]
            ]})
        return converted

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 extra_headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(api_key, base_url, extra_headers)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, default_headers=extra_headers)

    async def chat_stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        params: Dict[str, Any] = dict(
            model=model, messages=self._convert_messages(messages), max_tokens=max_tokens,
            stream=True, stream_options={"include_usage": True}, **kwargs,
        )
        if temperature is not None:
            params["temperature"] = temperature
        if tools is not None:
            params["tools"] = tools
        if tool_choice is not None:
            params["tool_choice"] = tool_choice

        stream = await self._client.chat.completions.create(**params)

        in_think = False      # 是否处于 <think>...</think> 标签内
        reasoning = ""        # 累积的完整思考内容

        async for chunk in stream:
            usage = _usage_from(getattr(chunk, "usage", None))
            if not chunk.choices:
                if usage:
                    yield StreamChunk(usage=usage, raw=chunk)
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            text = delta.content or ""

            # 思考内容来源一：独立字段 reasoning_content（DeepSeek 风格）
            reasoning_delta = getattr(delta, "reasoning_content", None) or ""
            # 思考内容来源二：<think> 标签嵌在 content 里（MiniMax、开源模型风格）
            if not reasoning_delta and text:
                text, reasoning_delta, in_think = _split_think_tag(text, in_think)

            if reasoning_delta:
                reasoning += reasoning_delta

            tool_calls = [
                ToolCall(
                    id=tc.id or "", type=tc.type or "function", index=tc.index,
                    function=ToolCallFunction(
                        name=(tc.function and tc.function.name) or "",
                        arguments=(tc.function and tc.function.arguments) or "",
                    ),
                )
                for tc in (delta.tool_calls or [])
            ] or None

            # 有正文 / 工具调用 / usage / 结束原因时按普通帧输出，只有思考时按思考帧输出
            if text or tool_calls or usage or choice.finish_reason:
                yield StreamChunk(
                    content=text, finish_reason=choice.finish_reason, usage=usage,
                    tool_calls=tool_calls, raw=chunk, reasoning_content=reasoning,
                )
            elif reasoning_delta:
                yield StreamChunk(content=reasoning_delta, thinking=True, raw=chunk, reasoning_content=reasoning)


def _split_think_tag(text: str, in_think: bool) -> tuple[str, str, bool]:
    """把一帧文本拆成 (正文, 思考, 是否仍在思考中)。标签可能跨帧。"""
    if in_think:
        if "</think>" not in text:
            return "", text, True
        thought, rest = text.split("</think>", 1)
        return rest, thought, False
    if "<think>" not in text:
        return text, "", False
    before, after = text.split("<think>", 1)
    if "</think>" in after:
        thought, rest = after.split("</think>", 1)
        return before + rest, thought, False
    return before, after, True
