"""
Base class for fallback strategies.

Each strategy monitors an ongoing request (stream or block) and
decides whether the current provider should be abandoned. Strategies
are stateful per-request — a new instance is created for each attempt.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class StrategyAction(str, Enum):
    """What the engine should do when a strategy fires."""
    SWITCH = "switch"   # Abort current provider, try next
    RECORD = "record"   # Cannot switch (content already sent), record only


@dataclass
class StrategyEvent:
    """Event emitted when a strategy detects an anomaly."""
    strategy: str       # Strategy name, e.g. "ttft_timeout"
    action: StrategyAction
    provider: str
    model: str
    detail: Dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """
    Per-request strategy instance.

    Created fresh for each provider attempt. The engine calls
    ``on_chunk()`` for every stream chunk (or ``on_complete()``
    for block requests), and the strategy returns an event if
    anomaly is detected.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy identifier, used in metrics labels."""
        ...

    @abstractmethod
    def on_chunk(
        self,
        content: str,
        is_first: bool,
        elapsed: float,
        total_tokens: int,
        chunk_arrived_at: Optional[float] = None,
        is_thinking: bool = False,
    ) -> Optional[StrategyEvent]:
        """
        Called for each stream chunk.

        Args:
            content: The text content of this chunk (may be empty).
            is_first: Whether this is the very first chunk.
            elapsed: Seconds since request start.
            total_tokens: Cumulative tokens received so far.
            chunk_arrived_at: monotonic timestamp when chunk arrived from SDK,
                excluding yield/caller processing overhead. Strategies that
                measure inter-chunk gaps should use this instead of
                ``time.monotonic()`` to avoid counting caller processing time.
            is_thinking: Whether this chunk is from the model's thinking
                phase (e.g. ``<think>`` tags). Strategies may skip certain
                checks during thinking while keeping timers updated.

        Returns:
            A StrategyEvent if anomaly detected, else None.
        """
        ...

    def on_complete(
        self,
        content: str,
        elapsed: float,
        total_tokens: int,
    ) -> Optional[StrategyEvent]:
        """
        Called when a block (non-stream) request completes.

        Default implementation does nothing. Override in strategies
        that apply to non-streaming scenarios.
        """
        return None

    def on_timeout(self, elapsed: float) -> Optional[StrategyEvent]:
        """
        Called when the request times out before any response.

        Default implementation does nothing. Override in strategies
        that handle timeout scenarios.
        """
        return None
