"""OpenAI Responses 协议适配器。入参仍是 OpenAI chat 格式，内部转成 Responses 格式。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from ..types import TokenUsage
from .base import BaseProvider, StreamChunk, ToolCall, ToolCallFunction

# Responses 结束状态 → OpenAI chat finish_reason
_FINISH_REASON = {"max_output_tokens": "length", "content_filter": "content_filter"}


class OpenAIResponsesProvider(BaseProvider):

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 extra_headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(api_key, base_url, extra_headers)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, default_headers=extra_headers)

    @staticmethod
    def _convert_messages(messages: List[Dict[str, Any]]) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """chat messages → (instructions, input)。system 抽成 instructions，工具调用转 item。"""
        instructions: List[str] = []
        items: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                instructions.append(msg.get("content") or "")
            elif role == "assistant":
                if msg.get("content"):
                    items.append({"role": "assistant", "content": msg["content"]})
                for tc in msg.get("tool_calls") or []:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call", "call_id": tc.get("id", ""),
                        "name": func.get("name", ""), "arguments": func.get("arguments") or "{}",
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output", "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content") or "",
                })
            else:
                items.append({"role": role, "content": msg.get("content") or ""})
        return ("\n\n".join(instructions) or None), items

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """chat tools（嵌套 function）→ Responses tools（扁平）。"""
        return [
            {"type": "function", **tool["function"]}
            for tool in tools if tool.get("type") == "function"
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
        instructions, items = self._convert_messages(messages)
        params: Dict[str, Any] = dict(model=model, input=items, max_output_tokens=max_tokens, stream=True, **kwargs)
        if instructions:
            params["instructions"] = instructions
        if temperature is not None:
            params["temperature"] = temperature
        if tools is not None:
            params["tools"] = self._convert_tools(tools)
        if tool_choice is not None:
            params["tool_choice"] = tool_choice

        stream = await self._client.responses.create(**params)

        reasoning = ""                       # 累积的思考摘要
        tool_index: Dict[int, int] = {}      # output_index → 工具调用序号
        has_tool_call = False

        async for event in stream:
            kind = event.type

            if kind == "response.output_text.delta":
                yield StreamChunk(content=event.delta, raw=event, reasoning_content=reasoning)

            elif kind == "response.reasoning_summary_text.delta":
                reasoning += event.delta
                yield StreamChunk(content=event.delta, thinking=True, raw=event, reasoning_content=reasoning)

            elif kind == "response.output_item.added" and event.item.type == "function_call":
                # 工具调用开始：先发 id + name，参数后续增量到达
                has_tool_call = True
                index = tool_index.setdefault(event.output_index, len(tool_index))
                yield StreamChunk(raw=event, tool_calls=[ToolCall(
                    id=event.item.call_id, index=index,
                    function=ToolCallFunction(name=event.item.name),
                )])

            elif kind == "response.function_call_arguments.delta":
                index = tool_index.get(event.output_index, 0)
                yield StreamChunk(raw=event, tool_calls=[ToolCall(
                    id="", index=index, function=ToolCallFunction(arguments=event.delta),
                )])

            elif kind in ("response.completed", "response.incomplete"):
                response = event.response
                usage = response.usage
                reason = "tool_calls" if has_tool_call else "stop"
                if response.incomplete_details:
                    reason = _FINISH_REASON.get(response.incomplete_details.reason, "stop")
                yield StreamChunk(
                    finish_reason=reason, raw=event, reasoning_content=reasoning,
                    usage=TokenUsage(
                        prompt_tokens=usage.input_tokens,
                        completion_tokens=usage.output_tokens,
                        total_tokens=usage.total_tokens,
                        cached_tokens=usage.input_tokens_details.cached_tokens,
                    ) if usage else None,
                )

            elif kind == "response.failed":
                error = event.response.error
                raise RuntimeError(f"Responses API failed: {error.message if error else 'unknown'}")
