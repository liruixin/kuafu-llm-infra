"""
Empty frame detection strategy.

Triggers when N consecutive frames have empty content,
covering tool-call empty frames and thinking-mode empty frames.
Only fires before real content is sent (action=SWITCH).
"""

from __future__ import annotations

from typing import Optional

from .base import BaseStrategy, StrategyEvent, StrategyAction
from .registry import register_strategy


class EmptyFrameStrategy(BaseStrategy):

    def __init__(
        self,
        threshold: int,
        provider: str,
        model: str,
    ) -> None:
        self._threshold = threshold
        self._provider = provider
        self._model = model
        self._consecutive_empty = 0
        self._has_content = False

    @property
    def name(self) -> str:
        return "empty_frame"

    def on_chunk(
        self,
        content: str,
        is_first: bool,
        elapsed: float,
        total_tokens: int,
        chunk_arrived_at: Optional[float] = None,
        is_thinking: bool = False,
    ) -> Optional[StrategyEvent]:
        # 思考帧不算空帧
        if is_thinking:
            return None

        if content:
            self._has_content = True
            self._consecutive_empty = 0
            return None

        self._consecutive_empty += 1

        if self._consecutive_empty >= self._threshold and not self._has_content:
            return StrategyEvent(
                strategy=self.name,
                action=StrategyAction.SWITCH,
                provider=self._provider,
                model=self._model,
                detail={
                    "consecutive_empty": self._consecutive_empty,
                    "threshold": self._threshold,
                },
            )
        return None

    def on_complete(
        self,
        content: str,
        elapsed: float,
        total_tokens: int,
    ) -> Optional[StrategyEvent]:
        if not content.strip():
            action = StrategyAction.SWITCH if not self._has_content else StrategyAction.RECORD
            return StrategyEvent(
                strategy="empty_response",
                action=action,
                provider=self._provider,
                model=self._model,
                detail={"elapsed": elapsed},
            )
        return None


@register_strategy
def create_empty_frame(cfg, provider: str, model: str):
    return EmptyFrameStrategy(cfg.empty_frame_threshold, provider, model)
