"""
Slow speed degradation strategy.

Detects when token throughput falls below a threshold.
Only evaluated after a minimum number of tokens have been received
to avoid false positives during ramp-up.
"""

from __future__ import annotations

from typing import Optional

from .base import BaseStrategy, StrategyEvent, StrategyAction
from .registry import register_strategy


class SlowSpeedStrategy(BaseStrategy):

    MIN_TOKENS_FOR_EVAL = 20  # Don't evaluate until enough tokens received

    def __init__(
        self,
        threshold: float,
        provider: str,
        model: str,
    ) -> None:
        self._threshold = threshold  # tokens/sec
        self._provider = provider
        self._model = model
        self._has_content = False

    @property
    def name(self) -> str:
        return "slow_speed"

    def on_chunk(
        self,
        content: str,
        is_first: bool,
        elapsed: float,
        total_tokens: int,
    ) -> Optional[StrategyEvent]:
        if content:
            self._has_content = True

        if total_tokens < self.MIN_TOKENS_FOR_EVAL or elapsed < 2.0:
            return None

        tps = total_tokens / elapsed
        if tps < self._threshold:
            action = StrategyAction.RECORD if self._has_content else StrategyAction.SWITCH
            return StrategyEvent(
                strategy=self.name,
                action=action,
                provider=self._provider,
                model=self._model,
                detail={
                    "tokens_per_second": round(tps, 2),
                    "threshold": self._threshold,
                    "total_tokens": total_tokens,
                },
            )
        return None


@register_strategy
def create_slow_speed(cfg, provider: str, model: str):
    return SlowSpeedStrategy(cfg.slow_speed_threshold, provider, model)
