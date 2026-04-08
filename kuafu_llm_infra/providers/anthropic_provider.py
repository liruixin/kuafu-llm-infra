"""
Anthropic SDK provider adapter.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

from anthropic import AsyncAnthropic

from ..types import TokenUsage
from .base import BaseProvider, ChatResponse, StreamChunk
from .registry import register_provider


class AnthropicProvider(BaseProvider):
    """Provider adapter for the Anthropic Messages API."""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url is not None:
            kwargs["base_url"] = base_url
        if extra_headers is not None:
            kwargs["default_headers"] = extra_headers
        self._client = AsyncAnthropic(**kwargs)

    @property
    def provider_type(self) -> str:
        return "anthropic"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(
        messages: List[Dict[str, Any]],
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """Extract system prompt and convert to Anthropic message format."""
        system_parts: List[str] = []
        converted: List[Dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "system":
                system_parts.append(msg.get("content", ""))
            else:
                converted.append({"role": msg["role"], "content": msg.get("content", "")})
        system = "\n\n".join(system_parts) if system_parts else None
        return system, converted

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        system, converted = self._convert_messages(messages)

        params: Dict[str, Any] = dict(
            model=model,
            messages=converted,
            max_tokens=max_tokens,
            **kwargs,
        )
        if system is not None:
            params["system"] = system
        if temperature is not None:
            params["temperature"] = temperature

        coro = self._client.messages.create(**params)
        if timeout is not None:
            resp = await asyncio.wait_for(coro, timeout=timeout)
        else:
            resp = await coro

        content = ""
        for block in resp.content:
            if block.type == "text":
                content += block.text

        usage = TokenUsage()
        if resp.usage:
            usage = TokenUsage(
                prompt_tokens=resp.usage.input_tokens,
                completion_tokens=resp.usage.output_tokens,
                total_tokens=resp.usage.input_tokens + resp.usage.output_tokens,
            )

        return ChatResponse(
            content=content,
            model=resp.model,
            finish_reason=resp.stop_reason or "stop",
            usage=usage,
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
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        system, converted = self._convert_messages(messages)

        params: Dict[str, Any] = dict(
            model=model,
            messages=converted,
            max_tokens=max_tokens,
            **kwargs,
        )
        if system is not None:
            params["system"] = system
        if temperature is not None:
            params["temperature"] = temperature

        coro = self._client.messages.create(stream=True, **params)
        if timeout is not None:
            stream = await asyncio.wait_for(coro, timeout=timeout)
        else:
            stream = await coro

        input_tokens = 0
        output_tokens = 0

        async for event in stream:
            if event.type == "message_start":
                if hasattr(event.message, "usage") and event.message.usage:
                    input_tokens = event.message.usage.input_tokens
                continue

            if event.type == "content_block_delta":
                yield StreamChunk(
                    content=event.delta.text if hasattr(event.delta, "text") else "",
                    raw=event,
                )

            elif event.type == "message_delta":
                if hasattr(event, "usage") and event.usage:
                    output_tokens = event.usage.output_tokens
                usage = TokenUsage(
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                )
                yield StreamChunk(
                    content="",
                    finish_reason=event.delta.stop_reason,
                    usage=usage,
                    raw=event,
                )


@register_provider("anthropic")
def _create_anthropic(
    api_key: str, base_url: str, extra_headers: dict | None = None,
) -> AnthropicProvider:
    return AnthropicProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)
