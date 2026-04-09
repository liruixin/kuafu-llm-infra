"""
Anthropic SDK provider adapter.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional

from anthropic import AsyncAnthropic

from ..types import TokenUsage
from .base import BaseProvider, ChatResponse, StreamChunk, ToolCall
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
        """Extract system prompt and convert to Anthropic message format.

        Non-system messages are passed through as-is to preserve
        complex content structures (e.g. tool_result blocks, tool_use
        in assistant messages).
        """
        system_parts: List[str] = []
        converted: List[Dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "system":
                system_parts.append(msg.get("content", ""))
            else:
                converted.append({k: v for k, v in msg.items()})
        system = "\n\n".join(system_parts) if system_parts else None
        return system, converted

    @staticmethod
    def _convert_tools(
        tools: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert OpenAI-format tools to Anthropic-format tools.

        OpenAI format::

            {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}

        Anthropic format::

            {"name": ..., "description": ..., "input_schema": ...}
        """
        converted = []
        for tool in tools:
            func = tool.get("function", {})
            converted.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return converted

    @staticmethod
    def _convert_tool_choice(tool_choice: str) -> Dict[str, str]:
        """Convert OpenAI-format tool_choice string to Anthropic-format.

        OpenAI → Anthropic mapping:
        - ``"auto"`` → ``{"type": "auto"}``
        - ``"none"`` → ``{"type": "auto"}`` (caller should omit tools)
        """
        if tool_choice == "none":
            return {"type": "auto"}
        return {"type": "auto"}

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
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
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
        if tools is not None:
            params["tools"] = self._convert_tools(tools)
        if tool_choice is not None:
            params["tool_choice"] = self._convert_tool_choice(tool_choice)

        coro = self._client.messages.create(**params)
        if timeout is not None:
            resp = await asyncio.wait_for(coro, timeout=timeout)
        else:
            resp = await coro

        content = ""
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    type="function",
                    function_name=block.name,
                    function_arguments=json.dumps(block.input) if isinstance(block.input, dict) else str(block.input),
                ))

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
            tool_calls=tool_calls if tool_calls else None,
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
        if tools is not None:
            params["tools"] = self._convert_tools(tools)
        if tool_choice is not None:
            params["tool_choice"] = self._convert_tool_choice(tool_choice)

        coro = self._client.messages.create(stream=True, **params)
        if timeout is not None:
            stream = await asyncio.wait_for(coro, timeout=timeout)
        else:
            stream = await coro

        input_tokens = 0
        output_tokens = 0
        current_tool_call: Optional[ToolCall] = None

        async for event in stream:
            if event.type == "message_start":
                if hasattr(event.message, "usage") and event.message.usage:
                    input_tokens = event.message.usage.input_tokens
                continue

            if event.type == "content_block_start":
                block = event.content_block
                if hasattr(block, "type") and block.type == "tool_use":
                    current_tool_call = ToolCall(
                        id=block.id,
                        type="function",
                        function_name=block.name,
                        function_arguments="",
                    )
                continue

            if event.type == "content_block_delta":
                if hasattr(event.delta, "text"):
                    yield StreamChunk(
                        content=event.delta.text,
                        raw=event,
                    )
                elif hasattr(event.delta, "partial_json") and current_tool_call:
                    current_tool_call.function_arguments += event.delta.partial_json
                    yield StreamChunk(
                        content="",
                        tool_calls=[ToolCall(
                            id=current_tool_call.id,
                            type="function",
                            function_name=current_tool_call.function_name,
                            function_arguments=event.delta.partial_json,
                        )],
                        raw=event,
                    )

            elif event.type == "content_block_stop":
                current_tool_call = None

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
