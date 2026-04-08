"""
In-memory state backend.

Default backend for single-instance deployments.
All data lives in process memory — zero external dependencies.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from .backend import (
    StateBackend,
    ScoreCard,
    ProbeResult,
    RequestOutcome,
    AggregatedStats,
)


class MemoryBackend(StateBackend):
    """In-process memory state backend."""

    def __init__(self) -> None:
        self._score_cards: Dict[Tuple[str, str], ScoreCard] = {}
        self._probe_results: Dict[Tuple[str, str], ProbeResult] = {}
        self._agg_stats: Dict[Tuple[str, str], AggregatedStats] = {}

    def _key(self, model: str, provider: str) -> Tuple[str, str]:
        return (model, provider)

    # --- Score card ---

    async def get_score_card(self, model: str, provider: str) -> ScoreCard:
        key = self._key(model, provider)
        if key not in self._score_cards:
            self._score_cards[key] = ScoreCard()
        return self._score_cards[key]

    async def update_score_card(self, model: str, provider: str, card: ScoreCard) -> None:
        self._score_cards[self._key(model, provider)] = card

    # --- Probe coordination (per model+provider) ---

    async def try_acquire_probe_lock(self, model: str, provider: str, ttl: float) -> bool:
        return True

    async def set_probe_result(self, model: str, provider: str, result: ProbeResult) -> None:
        self._probe_results[self._key(model, provider)] = result

    async def get_probe_result(self, model: str, provider: str) -> Optional[ProbeResult]:
        return self._probe_results.get(self._key(model, provider))

    # --- Request metrics aggregation ---

    async def record_request(self, model: str, provider: str, outcome: RequestOutcome) -> None:
        key = self._key(model, provider)
        if key not in self._agg_stats:
            self._agg_stats[key] = AggregatedStats()

        stats = self._agg_stats[key]
        stats.total += 1
        if outcome.success:
            stats.success += 1
        else:
            stats.failure += 1

        card = await self.get_score_card(model, provider)
        card.push_request(outcome)

    async def get_aggregated_stats(self, model: str, provider: str) -> AggregatedStats:
        key = self._key(model, provider)
        if key not in self._agg_stats:
            self._agg_stats[key] = AggregatedStats()
        return self._agg_stats[key]
