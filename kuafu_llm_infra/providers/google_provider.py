"""Google Gemini 协议适配器（google-genai SDK）。"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from google import genai
from google.genai import types

from ..types import TokenUsage
from .base import BaseProvider, StreamChunk, ToolCall, ToolCallFunction

_FINISH_REASON = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter"}
_TOOL_MODE = {"auto": "AUTO", "none": "NONE", "required": "ANY"}


class GoogleProvider(BaseProvider):

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 extra_headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(api_key, base_url, extra_headers)
        self._client = genai.Client(api_key=api_key)

    @staticmethod
    def _convert_messages(messages: List[Dict[str, Any]]) -> tuple[Optional[str], List[types.Content]]:
        """chat messages → (system_instruction, contents)。"""
        # tool 消息只带 tool_call_id，Gemini 要求带函数名，先建映射
        call_id_to_name = {
            tc.get("id"): tc.get("function", {}).get("name", "")
            for msg in messages if msg.get("role") == "assistant"
            for tc in msg.get("tool_calls") or []
        }
        system: List[str] = []
        contents: List[types.Content] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                system.append(msg.get("content") or "")
            elif role == "user":
                contents.append(types.Content(role="user", parts=[types.Part(text=msg.get("content") or "")]))
            elif role == "assistant":
                parts: List[types.Part] = []
                if msg.get("content"):
                    parts.append(types.Part(text=msg["content"]))
                for tc in msg.get("tool_calls") or []:
                    func = tc.get("function", {})
                    parts.append(types.Part(
                        function_call=types.FunctionCall(
                            id=tc.get("id", ""), name=func.get("name", ""), args=_parse_json(func.get("arguments")),
                        ),
                        # Gemini thinking 模型要求回传上一轮的 thought_signature
                        thought_signature=tc.get("thought_signature"),
                    ))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                call_id = msg.get("tool_call_id", "")
                result = _parse_json(msg.get("content"), wrap=True)
                part = types.Part(function_response=types.FunctionResponse(
                    id=call_id, name=call_id_to_name.get(call_id, "unknown"), response=result,
                ))
                # 连续的 tool 消息合并进同一条 user Content
                if contents and contents[-1].role == "user" and contents[-1].parts[0].function_response:
                    contents[-1].parts.append(part)
                else:
                    contents.append(types.Content(role="user", parts=[part]))
        return ("\n\n".join(system) or None), contents

    @staticmethod
    def _extract_usage(meta: Any) -> Optional[TokenUsage]:
        if not meta or not meta.total_token_count:
            return None
        return TokenUsage(
            prompt_tokens=meta.prompt_token_count or 0,
            completion_tokens=meta.candidates_token_count or 0,
            total_tokens=meta.total_token_count or 0,
            cached_tokens=meta.cached_content_token_count or 0,
        )

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
        system, contents = self._convert_messages(messages)
        config: Dict[str, Any] = {"max_output_tokens": max_tokens, **kwargs}
        if system:
            config["system_instruction"] = system
        if temperature is not None:
            config["temperature"] = temperature
        if tools is not None:
            config["tools"] = [types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name=t["function"].get("name", ""),
                    description=t["function"].get("description", ""),
                    parameters=t["function"].get("parameters", {}),
                ) for t in tools
            ])]
        if tool_choice is not None:
            config["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=_TOOL_MODE.get(tool_choice, "AUTO")),
            )

        stream = await self._client.aio.models.generate_content_stream(
            model=model, contents=contents, config=types.GenerateContentConfig(**config),
        )

        reasoning = ""
        tool_index = -1

        async for chunk in stream:
            usage = self._extract_usage(chunk.usage_metadata)
            candidate = chunk.candidates[0] if chunk.candidates else None
            if candidate is None:
                if usage:
                    yield StreamChunk(usage=usage, raw=chunk)
                continue

            for part in (candidate.content.parts if candidate.content else None) or []:
                if part.text and part.thought:
                    reasoning += part.text
                    yield StreamChunk(content=part.text, thinking=True, raw=chunk, reasoning_content=reasoning)
                elif part.text:
                    yield StreamChunk(content=part.text, raw=chunk, reasoning_content=reasoning)
                if part.function_call:
                    tool_index += 1
                    fc = part.function_call
                    yield StreamChunk(raw=chunk, tool_calls=[ToolCall(
                        id=fc.id or str(uuid.uuid4()), index=tool_index,
                        function=ToolCallFunction(name=fc.name, arguments=json.dumps(dict(fc.args or {}))),
                        thought_signature=part.thought_signature,
                    )])

            if candidate.finish_reason:
                reason = str(candidate.finish_reason).split(".")[-1]
                yield StreamChunk(
                    finish_reason="tool_calls" if tool_index >= 0 else _FINISH_REASON.get(reason, "stop"),
                    usage=usage, raw=chunk, reasoning_content=reasoning,
                )


def _parse_json(text: Any, wrap: bool = False) -> Dict[str, Any]:
    """解析 JSON 字符串；wrap=True 时非 dict 结果包成 {"result": ...}。"""
    try:
        value = json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        value = text
    if isinstance(value, dict):
        return value
    return {"result": value} if wrap else {}
