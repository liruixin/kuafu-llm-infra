"""Anthropic Messages 协议适配器。"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

from anthropic import AsyncAnthropic

from ..types import TokenUsage
from .base import BaseProvider, StreamChunk, ToolCall, ToolCallFunction

_FINISH_REASON = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}
_TOOL_CHOICE = {"auto": {"type": "auto"}, "none": {"type": "none"}, "required": {"type": "any"}}


class AnthropicProvider(BaseProvider):

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 extra_headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(api_key, base_url, extra_headers)
        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url, default_headers=extra_headers)

    @staticmethod
    def _convert_messages(messages: List[Dict[str, Any]]) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """chat messages → (system, anthropic messages)。只保留 role + content，多余字段会被 API 拒绝。"""
        system: List[str] = []
        converted: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                system.append(msg.get("content") or "")
            elif role == "assistant" and msg.get("tool_calls"):
                blocks: List[Dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    blocks.append({
                        "type": "tool_use", "id": tc.get("id", ""), "name": func.get("name", ""),
                        "input": _parse_json(func.get("arguments")),
                    })
                converted.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": msg.get("tool_call_id", ""), "content": msg.get("content") or ""}
                # 连续的 tool 消息必须合并进同一条 user 消息
                if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                    converted[-1]["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
            else:
                converted.append({"role": role, "content": msg.get("content") or ""})
        return ("\n\n".join(system) or None), converted

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "name": t["function"].get("name", ""),
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

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
        system, converted = self._convert_messages(messages)
        params: Dict[str, Any] = dict(model=model, messages=converted, max_tokens=max_tokens, stream=True, **kwargs)
        if system:
            params["system"] = system
        if temperature is not None:
            params["temperature"] = temperature
        if tools is not None:
            params["tools"] = self._convert_tools(tools)
        if tool_choice is not None:
            params["tool_choice"] = _TOOL_CHOICE.get(tool_choice, {"type": "auto"})

        stream = await self._client.messages.create(**params)

        input_tokens = 0
        cached_tokens = 0
        reasoning = ""
        tool_index = -1  # 当前工具调用的序号

        async for event in stream:
            kind = event.type

            if kind == "message_start":
                usage = event.message.usage
                input_tokens = usage.input_tokens or 0
                cached_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0

            elif kind == "content_block_start" and event.content_block.type == "tool_use":
                tool_index += 1
                yield StreamChunk(raw=event, tool_calls=[ToolCall(
                    id=event.content_block.id, index=tool_index,
                    function=ToolCallFunction(name=event.content_block.name),
                )])

            elif kind == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    yield StreamChunk(content=delta.text, raw=event, reasoning_content=reasoning)
                elif delta.type == "thinking_delta":
                    reasoning += delta.thinking
                    yield StreamChunk(content=delta.thinking, thinking=True, raw=event, reasoning_content=reasoning)
                elif delta.type == "input_json_delta":
                    yield StreamChunk(raw=event, tool_calls=[ToolCall(
                        id="", index=tool_index, function=ToolCallFunction(arguments=delta.partial_json),
                    )])

            elif kind == "message_delta":
                output_tokens = event.usage.output_tokens or 0
                yield StreamChunk(
                    finish_reason=_FINISH_REASON.get(event.delta.stop_reason, "stop"),
                    raw=event, reasoning_content=reasoning,
                    usage=TokenUsage(
                        prompt_tokens=input_tokens, completion_tokens=output_tokens,
                        total_tokens=input_tokens + output_tokens, cached_tokens=cached_tokens,
                    ),
                )


def _parse_json(text: Any) -> Dict[str, Any]:
    try:
        return json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
