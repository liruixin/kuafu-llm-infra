"""
Anthropic SDK provider adapter.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional

from anthropic import AsyncAnthropic

from ..types import TokenUsage
from .base import BaseProvider, ChatResponse, StreamChunk, ToolCall, ToolCallFunction
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
    # Probe（禁用 thinking 以加速探测）
    # ------------------------------------------------------------------

    async def probe(
        self,
        model: str,
        *,
        max_tokens: int = 5,
        timeout: float = 10.0,
    ) -> AsyncIterator[StreamChunk]:
        """Anthropic 探测：显式关闭 thinking 避免模型思考导致 TTFT 超时。"""
        async for chunk in self.chat_stream(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=max_tokens,
            timeout=timeout,
            # thinking={"type": "disabled"},
        ):
            yield chunk

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(
        messages: List[Dict[str, Any]],
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """Convert OpenAI-format messages to Anthropic message format.

        Handles:
        - ``system`` messages → extracted as top-level system prompt
        - ``assistant`` messages with ``tool_calls`` → Anthropic ``tool_use`` blocks
        - ``tool`` messages → Anthropic ``tool_result`` blocks (merged into user turn)
        - Other messages → passed through as-is
        """
        system_parts: List[str] = []
        converted: List[Dict[str, Any]] = []

        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")

            if role == "system":
                system_parts.append(msg.get("content", ""))
                i += 1

            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    # Convert to Anthropic content blocks
                    blocks: List[Dict[str, Any]] = []
                    text = msg.get("content")
                    if text:
                        blocks.append({"type": "text", "text": text})
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        args_str = func.get("arguments", "{}")
                        try:
                            input_data = json.loads(args_str)
                        except (json.JSONDecodeError, TypeError):
                            input_data = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": input_data,
                        })
                    converted.append({"role": "assistant", "content": blocks})
                else:
                    converted.append({k: v for k, v in msg.items()})
                i += 1

            elif role == "tool":
                # Merge consecutive tool messages into a single user message
                # with tool_result content blocks (Anthropic requirement)
                tool_results: List[Dict[str, Any]] = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    t = messages[i]
                    result_block: Dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": t.get("tool_call_id", ""),
                    }
                    content = t.get("content")
                    if content is not None:
                        result_block["content"] = content
                    tool_results.append(result_block)
                    i += 1
                converted.append({"role": "user", "content": tool_results})

            else:
                converted.append({k: v for k, v in msg.items()})
                i += 1

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
        - ``"none"`` → ``{"type": "none"}``
        - ``"required"`` → ``{"type": "any"}``
        """
        mapping = {
            "auto": {"type": "auto"},
            "none": {"type": "none"},
            "required": {"type": "any"},
        }
        return mapping.get(tool_choice, {"type": "auto"})

    # Anthropic → OpenAI finish_reason mapping
    _FINISH_REASON_MAP = {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
    }

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
        reasoning = ""
        tool_calls = []
        for block in resp.content:
            if block.type == "thinking":
                reasoning += block.thinking
            elif block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    type="function",
                    function=ToolCallFunction(
                        name=block.name,
                        arguments=json.dumps(block.input) if isinstance(block.input, dict) else str(block.input),
                    ),
                ))

        usage = TokenUsage()
        if resp.usage:
            cached = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            usage = TokenUsage(
                prompt_tokens=resp.usage.input_tokens,
                completion_tokens=resp.usage.output_tokens,
                total_tokens=resp.usage.input_tokens + resp.usage.output_tokens,
                cached_tokens=cached,
            )

        return ChatResponse(
            content=content,
            reasoning_content=reasoning,
            model=resp.model,
            finish_reason=self._FINISH_REASON_MAP.get(resp.stop_reason, resp.stop_reason or "stop"),
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
        cached_tokens = 0
        current_tool_call: Optional[ToolCall] = None
        tool_call_index = 0  # Tracks index for OpenAI-compatible streaming

        async for event in stream:
            if event.type == "message_start":
                if hasattr(event.message, "usage") and event.message.usage:
                    input_tokens = event.message.usage.input_tokens
                    cached_tokens = getattr(event.message.usage, "cache_read_input_tokens", 0) or 0
                continue

            if event.type == "content_block_start":
                block = event.content_block
                if hasattr(block, "type") and block.type == "tool_use":
                    current_tool_call = ToolCall(
                        id=block.id,
                        type="function",
                        function=ToolCallFunction(
                            name=block.name,
                            arguments="",
                        ),
                        index=tool_call_index,
                    )
                    # Yield the initial tool call delta with id and name
                    yield StreamChunk(
                        content="",
                        tool_calls=[ToolCall(
                            id=block.id,
                            type="function",
                            function=ToolCallFunction(
                                name=block.name,
                                arguments="",
                            ),
                            index=tool_call_index,
                        )],
                        raw=event,
                    )
                    tool_call_index += 1
                continue

            if event.type == "content_block_delta":
                if hasattr(event.delta, "text"):
                    yield StreamChunk(
                        content=event.delta.text,
                        raw=event,
                    )
                elif hasattr(event.delta, "partial_json") and current_tool_call:
                    current_tool_call.function.arguments += event.delta.partial_json
                    yield StreamChunk(
                        content="",
                        tool_calls=[ToolCall(
                            id="",
                            type="function",
                            function=ToolCallFunction(
                                name="",
                                arguments=event.delta.partial_json,
                            ),
                            index=current_tool_call.index,
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
                    cached_tokens=cached_tokens,
                )
                yield StreamChunk(
                    content="",
                    finish_reason=self._FINISH_REASON_MAP.get(
                        event.delta.stop_reason,
                        event.delta.stop_reason or "stop",
                    ),
                    usage=usage,
                    raw=event,
                )


@register_provider("anthropic")
def _create_anthropic(
    api_key: str, base_url: str, extra_headers: dict | None = None,
) -> AnthropicProvider:
    return AnthropicProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)
