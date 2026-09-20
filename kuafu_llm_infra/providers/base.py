"""Provider 适配器基类：把各家 SDK 统一成 chat_stream / chat / probe 三个方法。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from ..types import TokenUsage


@dataclass
class ToolCallFunction:
    name: str = ""
    arguments: str = ""  # JSON 字符串


@dataclass
class ToolCall:
    id: str
    type: str = "function"
    function: ToolCallFunction = field(default_factory=ToolCallFunction)
    index: Optional[int] = None  # 流式增量的序号，同一 index 的 arguments 需拼接
    # 仅 Gemini thinking 模型有值，业务回传消息时放回 tool_call dict 顶层即可
    thought_signature: Optional[bytes] = None


@dataclass
class ChatResponse:
    """非流式完整响应。"""
    content: str = ""
    reasoning_content: str = ""
    model: str = ""
    finish_reason: str = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    tool_calls: Optional[List[ToolCall]] = None
    raw: Any = None


@dataclass
class StreamChunk:
    """流式响应的一帧。"""
    content: str = ""
    finish_reason: Optional[str] = None
    usage: Optional[TokenUsage] = None
    tool_calls: Optional[List[ToolCall]] = None
    raw: Any = None
    thinking: bool = False  # True = 思考帧，content 是推理文本，不应展示给用户
    reasoning_content: str = ""  # 累积到当前的完整思考内容


class BaseProvider(ABC):
    """子类只需实现 chat_stream，chat 和 probe 有默认实现。"""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._extra_headers = extra_headers

    @abstractmethod
    def chat_stream(
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
        """流式请求。messages / tools / tool_choice 均为 OpenAI chat 格式，子类负责转换。"""
        ...

    async def chat(self, model: str, messages: List[Dict[str, Any]], **kwargs: Any) -> ChatResponse:
        """非流式请求：消费 chat_stream 聚合成完整响应。"""
        return await aggregate_chunks(self.chat_stream(model, messages, **kwargs), model)

    def probe(self, model: str, *, max_tokens: int = 5) -> AsyncIterator[StreamChunk]:
        """健康探测：最小流式请求。"""
        return self.chat_stream(model, [{"role": "user", "content": "hi"}], max_tokens=max_tokens)


async def aggregate_chunks(chunks: AsyncIterator[StreamChunk], model: str) -> ChatResponse:
    """把流式的帧拼成一个完整响应。"""

    content = ""
    reasoning = ""
    finish_reason = "stop"
    usage = TokenUsage()
    raw = None
    tool_calls: Dict[int, ToolCall] = {}  # 按 index 聚合增量参数

    async for chunk in chunks:
        raw = chunk.raw
        if chunk.thinking:
            reasoning += chunk.content
        else:
            content += chunk.content
        if chunk.reasoning_content:
            reasoning = chunk.reasoning_content
        if chunk.finish_reason:
            finish_reason = chunk.finish_reason
        if chunk.usage:
            usage = chunk.usage
        for delta in chunk.tool_calls or []:
            index = delta.index if delta.index is not None else len(tool_calls)
            if index not in tool_calls:
                tool_calls[index] = ToolCall(
                    id=delta.id, type=delta.type,
                    function=ToolCallFunction(name=delta.function.name, arguments=""),
                    index=index, thought_signature=delta.thought_signature,
                )
            tool_calls[index].function.arguments += delta.function.arguments

    return ChatResponse(
        content=content,
        reasoning_content=reasoning,
        model=model,
        finish_reason=finish_reason,
        usage=usage,
        tool_calls=[tool_calls[i] for i in sorted(tool_calls)] or None,
        raw=raw,
    )
