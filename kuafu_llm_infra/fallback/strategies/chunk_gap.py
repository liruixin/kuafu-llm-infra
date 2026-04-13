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
        chunk_arrived_at: Optional[float] = None,
        is_thinking: bool = False,
    ) -> Optional[StrategyEvent]:
        now = chunk_arrived_at if chunk_arrived_at is not None else time.monotonic()
        gap = now - self._last_chunk_time
        self._last_chunk_time = now

        # 思考阶段：保持时间更新但跳过 gap 检查
        if is_thinking:
            return None

        # Only fire if we already have content (mid-stream stall)
        # 注意：先检查再更新 _has_content，避免首个内容帧把自己触发
        # （首帧的 gap 是 TTFT，应由 ttft_timeout 策略负责）
        if self._has_content and gap > self._threshold:
            return StrategyEvent(
                strategy=self.name,
                action=StrategyAction.RECORD,
                provider=self._provider,
                model=self._model,
                detail={"gap_seconds": gap, "threshold": self._threshold},
            )

        if content:
            self._has_content = True

        return None


@register_strategy
def create_chunk_gap(cfg, provider: str, model: str):
    return ChunkGapStrategy(cfg.timeout.chunk_gap, provider, model)
