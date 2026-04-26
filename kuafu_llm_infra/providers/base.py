"""
Base provider adapter interface.

All LLM SDK adapters must implement this interface. The library
routes requests through adapters so that the gateway, fallback
engine, and strategies can work with any SDK uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from ..types import TokenUsage


# ============================================================================
# Unified response / stream types
# ============================================================================

@dataclass
class ChatMessage:
    """A single message in a conversation."""
    role: str
    content: str = ""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


@dataclass
class ToolCallFunction:
    """Function details within a tool call."""
    name: str = ""
    arguments: str = ""  # JSON string


@dataclass
class ToolCall:
    """A single tool/function call returned by the model."""
    id: str
    type: str = "function"
    function: ToolCallFunction = field(default_factory=ToolCallFunction)
    index: Optional[int] = None  # Stream delta index (OpenAI compatibility)
    # Gemini thinking models only — 对其他 provider 永远为 None。业务侧回传消息时
    # 只要把它原样放回 tool_call dict 的顶层 key，Google provider 会自动回填到 Part。
    thought_signature: Optional[bytes] = None


@dataclass
class ChatResponse:
    """Non-streaming LLM response."""
    content: str = ""
    reasoning_content: str = ""  # 模型思考过程（DeepSeek reasoning_content / Anthropic thinking block / Gemini thought part）
    model: str = ""
    finish_reason: str = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    tool_calls: Optional[List[ToolCall]] = None
    raw: Any = None  # Original SDK response for passthrough


@dataclass
class StreamChunk:
    """A single chunk in a streaming response."""
    content: str = ""
    reasoning_content: str = ""  # 增量思考内容（DeepSeek reasoning_content / Anthropic thinking / Gemini thought）
    finish_reason: Optional[str] = None
    usage: Optional[TokenUsage] = None  # Present on final chunk if SDK supports it
    tool_calls: Optional[List[ToolCall]] = None  # Incremental tool call deltas
    raw: Any = None
    thinking: bool = False  # True when chunk is from model thinking phase (<think>)


# ============================================================================
# Provider adapter base
# ============================================================================

class BaseProvider(ABC):
    """Abstract LLM provider adapter."""

    @property
    @abstractmethod
    def provider_type(self) -> str:
        """Return 'openai' or 'anthropic' or 'google'."""
        ...

    @abstractmethod
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
        """Non-streaming chat completion.

        Args:
            model: Model identifier, e.g. ``"gpt-4.1"``.
            messages: Conversation messages in OpenAI format.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            timeout: Request timeout in seconds.
            tools: Tool definitions for function calling. Each item
                follows the **OpenAI tool format**::

                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get current weather for a city",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "city": {
                                        "type": "string",
                                        "description": "City name"
                                    }
                                },
                                "required": ["city"]
                            }
                        }
                    }

                Provider adapters are responsible for converting this
                unified format to their SDK's native format (e.g.
                Anthropic ``input_schema``).
            tool_choice: Controls how the model selects tools.
                - ``"auto"`` – model decides whether to call a tool
                  (default when tools are present).
                - ``"none"`` – model must not call any tool.
            **kwargs: Additional provider-specific parameters.
        """
        ...

    @abstractmethod
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
        """Streaming chat completion. Yields ``StreamChunk`` objects.

        Args:
            tools: Same format as :meth:`chat`. See that method for the
                full tool definition schema.
            tool_choice: Same format as :meth:`chat`.

        When the model invokes a tool during streaming, chunks will
        carry ``tool_calls`` with incremental argument fragments. The
        caller should concatenate ``function.arguments`` across chunks
        sharing the same ``ToolCall.id`` to reconstruct the full JSON.
        """
        ...

    @abstractmethod
    async def probe(
        self,
        model: str,
        *,
        max_tokens: int = 5,
        timeout: float = 10.0,
    ) -> AsyncIterator[StreamChunk]:
        """
        Minimal streaming request for health/speed probing.

        Each provider must implement this method with SDK-specific
        optimisations (e.g. disabling thinking for Anthropic).
        """
        ...
