"""
OpenAI SDK provider adapter.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from ..types import TokenUsage
from .base import BaseProvider, ChatResponse, StreamChunk, ToolCall
from .registry import register_provider


class OpenAIProvider(BaseProvider):
    """Provider adapter for the OpenAI API (and compatible endpoints)."""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
        )

    @property
    def provider_type(self) -> str:
        return "openai"

    async def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        params: Dict[str, Any] = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stream=False,
            **kwargs,
        )
        if temperature is not None:
            params["temperature"] = temperature
        if tools is not None:
            params["tools"] = tools
        if tool_choice is not None:
            params["tool_choice"] = tool_choice

        coro = self._client.chat.completions.create(**params)
        if timeout is not None:
            resp = await asyncio.wait_for(coro, timeout=timeout)
        else:
            resp = await coro

        choice = resp.choices[0]
        usage = TokenUsage()
        if resp.usage:
            usage = TokenUsage(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
                total_tokens=resp.usage.total_tokens,
            )

        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    type=tc.type or "function",
                    function_name=tc.function.name or "",
                    function_arguments=tc.function.arguments or "",
                )
                for tc in choice.message.tool_calls
            ]

        return ChatResponse(
            content=choice.message.content or "",
            model=resp.model,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            tool_calls=tool_calls,
            raw=resp,
        )

    async def chat_stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        params: Dict[str, Any] = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        if temperature is not None:
            params["temperature"] = temperature
        if tools is not None:
            params["tools"] = tools
        if tool_choice is not None:
            params["tool_choice"] = tool_choice

        coro = self._client.chat.completions.create(**params)
        if timeout is not None:
            stream = await asyncio.wait_for(coro, timeout=timeout)
        else:
            stream = await coro

        async for chunk in stream:
            usage = None
            if hasattr(chunk, "usage") and chunk.usage is not None:
                usage = TokenUsage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    total_tokens=chunk.usage.total_tokens,
                )

            if not chunk.choices:
                if usage:
                    yield StreamChunk(content="", usage=usage, raw=chunk)
                continue

            delta = chunk.choices[0].delta
            delta_tool_calls = None
            if hasattr(delta, "tool_calls") and delta.tool_calls:
                delta_tool_calls = [
                    ToolCall(
                        id=tc.id or "",
                        type=tc.type or "function",
                        function_name=tc.function.name if tc.function and tc.function.name else "",
                        function_arguments=tc.function.arguments if tc.function and tc.function.arguments else "",
                    )
                    for tc in delta.tool_calls
                ]
            yield StreamChunk(
                content=delta.content or "",
                finish_reason=chunk.choices[0].finish_reason,
                usage=usage,
                tool_calls=delta_tool_calls,
                raw=chunk,
            )


@register_provider("openai")
def _create_openai(
    api_key: str, base_url: str, extra_headers: dict | None = None,
) -> OpenAIProvider:
    return OpenAIProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)
