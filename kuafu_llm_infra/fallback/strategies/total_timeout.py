"""
Total timeout strategy (non-streaming).

Triggers when a block request exceeds the total timeout.
Always action=SWITCH since no content has been sent.
"""

from __future__ import annotations

from typing import Optional

from .base import BaseStrategy, StrategyEvent, StrategyAction
from .registry import register_strategy


class TotalTimeoutStrategy(BaseStrategy):

    def __init__(self, threshold: float, provider: str, model: str) -> None:
        self._threshold = threshold
        self._provider = provider
        self._model = model

    @property
    def name(self) -> str:
        return "total_timeout"

    def on_chunk(
        self,
        content: str,
        is_first: bool,
        elapsed: float,
        total_tokens: int,
        chunk_arrived_at: Optional[float] = None,
    ) -> Optional[StrategyEvent]:
        # Not used in block mode, but satisfy interface
        return None

    def on_timeout(self, elapsed: float) -> Optional[StrategyEvent]:
        return StrategyEvent(
            strategy=self.name,
            action=StrategyAction.SWITCH,
            provider=self._provider,
            model=self._model,
            detail={"timeout": self._threshold, "elapsed": elapsed},
        )

    def on_complete(
        self,
        content: str,
        elapsed: float,
        total_tokens: int,
    ) -> Optional[StrategyEvent]:
        if not content.strip():
            return StrategyEvent(
                strategy="empty_response",
                action=StrategyAction.SWITCH,
                provider=self._provider,
                model=self._model,
                detail={"elapsed": elapsed},
            )
        return None


@register_strategy
def create_total_timeout(cfg, provider: str, model: str):
    return TotalTimeoutStrategy(cfg.timeout.per_request, provider, model)
