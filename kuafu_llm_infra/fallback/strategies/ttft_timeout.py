"""
TTFT (Time To First Token) timeout strategy.

Triggers when the first meaningful token hasn't arrived
within the configured threshold. Only fires during the
pre-content phase (action=SWITCH).
"""

from __future__ import annotations

from typing import Optional

from .base import BaseStrategy, StrategyEvent, StrategyAction
from .registry import register_strategy


class TtftTimeoutStrategy(BaseStrategy):

    def __init__(self, threshold: float, provider: str, model: str) -> None:
        self._threshold = threshold
        self._provider = provider
        self._model = model
        self._first_token_received = False

    @property
    def name(self) -> str:
        return "ttft_timeout"

    def on_chunk(
        self,
        content: str,
        is_first: bool,
        elapsed: float,
        total_tokens: int,
    ) -> Optional[StrategyEvent]:
        if content:
            self._first_token_received = True
            return None

        if not self._first_token_received and elapsed > self._threshold:
            return StrategyEvent(
                strategy=self.name,
                action=StrategyAction.SWITCH,
                provider=self._provider,
                model=self._model,
                detail={"ttft_threshold": self._threshold, "elapsed": elapsed},
            )
        return None

    def on_timeout(self, elapsed: float) -> Optional[StrategyEvent]:
        if not self._first_token_received:
            return StrategyEvent(
                strategy=self.name,
                action=StrategyAction.SWITCH,
                provider=self._provider,
                model=self._model,
                detail={"ttft_threshold": self._threshold, "elapsed": elapsed},
            )
        return None


@register_strategy
def create_ttft_timeout(cfg, provider: str, model: str):
    return TtftTimeoutStrategy(cfg.timeout.ttft, provider, model)
