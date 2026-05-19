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

    # Anthropic messages 数组里每条只认 role + content,
    # OpenAI/DeepSeek 协议带的 reasoning_content / name / tool_call_id 等额外字段
    # 必须在适配层显式剥掉,否则 Anthropic API 校验拒(400 BadRequest,且 openai-next
    # 这种代理网关会把真实错因吞成空字符串,排查痛苦)。
    _ANTHROPIC_MSG_KEYS = frozenset({"role", "content"})

    @staticmethod
    def _convert_messages(
        messages: List[Dict[str, Any]],
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """Convert OpenAI-format messages to Anthropic message format.

        Handles:
        - ``system`` messages → extracted as top-level system prompt
        - ``assistant`` messages with ``tool_calls`` → Anthropic ``tool_use`` blocks
        - ``tool`` messages → Anthropic ``tool_result`` blocks (merged into user turn)
        - Other messages → 只保留 role + content,过滤 reasoning_content / name / tool_call_id 等
          OpenAI/DeepSeek 协议字段(Anthropic 不认,会 400)
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
                    # 只保留 Anthropic 接受的字段,过滤 reasoning_content 等
                    converted.append({
                        k: v for k, v in msg.items()
                        if k in AnthropicProvider._ANTHROPIC_MSG_KEYS
                    })
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
                # 默认分支(user 等):只保留 Anthropic 接受的字段
                converted.append({
                    k: v for k, v in msg.items()
                    if k in AnthropicProvider._ANTHROPIC_MSG_KEYS
                })
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

        # 内部用 stream 拿完整 message,避开 Anthropic SDK 对长操作(>10 分钟,
        # 含长 thinking / 大 cache write / 超大 max_tokens 场景)的硬要求:
        #   "Streaming is required for operations that may take longer than 10 minutes"
        #
        # 用 messages.create(stream=True) 而不是 messages.stream() —— 后者是 SDK
        # 高级上下文管理器,会偷偷加 extra_headers / beta features 等参数,某些
        # OpenAI 兼容代理网关(如 openai-next 这种把 /v1/messages 转给 anthropic
        # 的多协议网关)不认会返回 400。chat_stream() 已经用 stream=True 跑通的形态,
        # chat() 复用同一形态最稳。
        coro = self._client.messages.create(stream=True, **params)
        if timeout is not None:
            stream = await asyncio.wait_for(coro, timeout=timeout)
        else:
            stream = await coro

        # 消费 stream events 合并成 Message 同构形态(对外 ChatResponse 接口零变化)
        content = ""
        reasoning = ""
        # tool_calls 按 content_block index 累积:partial_json 边收边拼
        # 每个 tool_use block 来一次 content_block_start(带 id+name) + 多次
        # content_block_delta(partial_json),最后 content_block_stop
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        current_tool_index: Optional[int] = None
        stop_reason: Optional[str] = None
        model_name: Optional[str] = None
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0

        async for event in stream:
            etype = event.type

            if etype == "message_start":
                msg = event.message
                model_name = getattr(msg, "model", None)
                if getattr(msg, "usage", None):
                    input_tokens = msg.usage.input_tokens or 0
                    cached_tokens = getattr(msg.usage, "cache_read_input_tokens", 0) or 0

            elif etype == "content_block_start":
                block = event.content_block
                btype = getattr(block, "type", "")
                if btype == "tool_use":
                    current_tool_index = event.index
                    tool_calls_acc[current_tool_index] = {
                        "id": block.id,
                        "name": block.name,
                        "arguments": "",
                    }
                # text / thinking block 不需要 start 时初始化,delta 时累积即可

            elif etype == "content_block_delta":
                delta = event.delta
                dtype = getattr(delta, "type", "")
                if dtype == "text_delta":
                    content += delta.text
                elif dtype == "thinking_delta":
                    reasoning += delta.thinking
                elif dtype == "input_json_delta" and current_tool_index is not None:
                    tool_calls_acc[current_tool_index]["arguments"] += delta.partial_json

            elif etype == "content_block_stop":
                current_tool_index = None

            elif etype == "message_delta":
                if getattr(event.delta, "stop_reason", None):
                    stop_reason = event.delta.stop_reason
                if getattr(event, "usage", None):
                    output_tokens = event.usage.output_tokens or 0

            # message_stop 不带数据,忽略

        # 按 content_block index 顺序输出 tool_calls
        tool_calls = []
        for idx in sorted(tool_calls_acc.keys()):
            tc = tool_calls_acc[idx]
            tool_calls.append(ToolCall(
                id=tc["id"],
                type="function",
                function=ToolCallFunction(
                    name=tc["name"],
                    arguments=tc["arguments"] or "{}",
                ),
            ))

        usage = TokenUsage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cached_tokens=cached_tokens,
        )

        return ChatResponse(
            content=content,
            reasoning_content=reasoning,
            model=model_name or model,
            finish_reason=self._FINISH_REASON_MAP.get(stop_reason, stop_reason or "stop"),
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            raw=None,
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
