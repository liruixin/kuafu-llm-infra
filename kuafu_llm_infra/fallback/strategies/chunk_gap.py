"""
Chunk gap timeout strategy.

Detects when streaming stalls mid-output — no new chunk arrives
within the configured gap threshold. Since content has already
been sent to the user, action is RECORD (cannot switch).
"""

from __future__ import annotations

import time
from typing import Optional

from .base import BaseStrategy, StrategyEvent, StrategyAction
from .registry import register_strategy


class ChunkGapStrategy(BaseStrategy):

    def __init__(self, threshold: float, provider: str, model: str) -> None:
        self._threshold = threshold
        self._provider = provider
        self._model = model
        self._last_chunk_time = time.monotonic()
        self._has_content = False

    @property
    def name(self) -> str:
        return "chunk_gap"

    def on_chunk(
        self,
        content: str,
        is_first: bool,
        elapsed: float,
        total_tokens: int,
    ) -> Optional[StrategyEvent]:
        now = time.monotonic()
        gap = now - self._last_chunk_time
        self._last_chunk_time = now

        if content:
            self._has_content = True

        # Only fire if we already have content (mid-stream stall)
        if self._has_content and gap > self._threshold:
            return StrategyEvent(
                strategy=self.name,
                action=StrategyAction.RECORD,
                provider=self._provider,
                model=self._model,
                detail={"gap_seconds": gap, "threshold": self._threshold},
            )
        return None


@register_strategy
def create_chunk_gap(cfg, provider: str, model: str):
    return ChunkGapStrategy(cfg.timeout.chunk_gap, provider, model)
