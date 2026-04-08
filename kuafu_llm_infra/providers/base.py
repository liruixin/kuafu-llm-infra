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
class ChatResponse:
    """Non-streaming LLM response."""
    content: str = ""
    model: str = ""
    finish_reason: str = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: Any = None  # Original SDK response for passthrough


@dataclass
class StreamChunk:
    """A single chunk in a streaming response."""
    content: str = ""
    finish_reason: Optional[str] = None
    usage: Optional[TokenUsage] = None  # Present on final chunk if SDK supports it
    raw: Any = None


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
        **kwargs: Any,
    ) -> ChatResponse:
        """Non-streaming chat completion."""
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
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming chat completion. Yields StreamChunk objects."""
        ...

    async def probe(
        self,
        model: str,
        *,
        max_tokens: int = 5,
        timeout: float = 10.0,
    ) -> AsyncIterator[StreamChunk]:
        """
        Minimal streaming request for health/speed probing.

        Default implementation delegates to ``chat_stream`` with a
        trivial prompt. Subclasses may override for SDK-specific
        optimisations.
        """
        async for chunk in self.chat_stream(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=max_tokens,
            timeout=timeout,
        ):
            yield chunk
