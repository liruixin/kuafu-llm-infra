"""
OpenAI SDK provider adapter.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from .base import BaseProvider, ChatResponse, StreamChunk


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

        coro = self._client.chat.completions.create(**params)
        if timeout is not None:
            resp = await asyncio.wait_for(coro, timeout=timeout)
        else:
            resp = await coro

        choice = resp.choices[0]
        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }

        return ChatResponse(
            content=choice.message.content or "",
            model=resp.model,
            finish_reason=choice.finish_reason or "stop",
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

        coro = self._client.chat.completions.create(**params)
        if timeout is not None:
            stream = await asyncio.wait_for(coro, timeout=timeout)
        else:
            stream = await coro

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            yield StreamChunk(
                content=delta.content or "",
                finish_reason=chunk.choices[0].finish_reason,
                raw=chunk,
            )
