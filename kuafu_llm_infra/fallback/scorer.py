"""
Provider composite scorer.

Maintains per-(model, provider) score cards and computes a
composite score used to rank providers for each request.

Score = health_gate * (
    w_priority  * priority_score  +
    w_speed     * speed_score     +
    w_success   * success_rate    +
    w_stability * stability_score
)
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config.schema import ModelProviderEntry, HealthCheckConfig, adapter_key
from ..state.backend import StateBackend, ScoreCard
from ..metrics.collector import MetricsCollector, NoopCollector
from ..metrics import registry as m

logger = logging.getLogger("kuafu_llm_infra.scorer")


# ============================================================================
# Scoring weights (sum = 1.0)
# ============================================================================

W_PRIORITY = 0.25
W_SPEED = 0.30
W_SUCCESS = 0.35
W_STABILITY = 0.10


@dataclass
class ScoredProvider:
    """A provider entry with its computed score."""
    provider_name: str
    entry: ModelProviderEntry
    score: float
    reason: str = ""


class Scorer:
    """Computes and maintains provider scores."""

    def __init__(
        self,
        state: StateBackend,
        health_config: HealthCheckConfig,
        metrics: Optional[MetricsCollector] = None,
    ) -> None:
        self._state = state
        self._health_config = health_config
        self._metrics = metrics or NoopCollector()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def rank_providers(
        self,
        model: str,
        entries: List[ModelProviderEntry],
    ) -> List[ScoredProvider]:
        """
        Rank provider entries by composite score, descending.

        Providers that are unhealthy or in cooldown are excluded.
        """
        results: List[ScoredProvider] = []

        for entry in entries:
            key = adapter_key(entry.provider, entry.endpoint)
            card = await self._state.get_score_card(model, key)
            score, reason = self._compute_score(entry, card)

            if score <= 0:
                logger.debug(f"[scorer] {key} excluded for {model}: {reason}")
                continue

            results.append(ScoredProvider(
                provider_name=key,
                entry=entry,
                score=score,
                reason=reason,
            ))
            self._metrics.set(m.PROVIDER_SCORE, score, model=model, provider=key)

        results.sort(key=lambda x: x.score, reverse=True)
        return results

    async def record_success(
        self,
        model: str,
        provider: str,
        ttft: Optional[float] = None,
        tokens_per_second: Optional[float] = None,
        duration: Optional[float] = None,
    ) -> None:
        from ..state.backend import RequestOutcome
        outcome = RequestOutcome(
            success=True,
            ttft_seconds=ttft,
            tokens_per_second=tokens_per_second,
            duration_seconds=duration,
        )
        await self._state.record_request(model, provider, outcome)

    async def record_failure(
        self,
        model: str,
        provider: str,
        reason: str = "",
    ) -> None:
        from ..state.backend import RequestOutcome
        outcome = RequestOutcome(success=False, failure_reason=reason)
        await self._state.record_request(model, provider, outcome)

    async def get_score_card(self, model: str, provider: str) -> ScoreCard:
        return await self._state.get_score_card(model, provider)

    # ------------------------------------------------------------------
    # Score computation
    # ------------------------------------------------------------------

    def _compute_score(
        self,
        entry: ModelProviderEntry,
        card: ScoreCard,
    ) -> Tuple[float, str]:
        if not card.health:
            return 0.0, "unhealthy"

        cooldown = self._health_config.cooldown
        threshold = self._health_config.failure_threshold
        if card.consecutive_failures >= threshold:
            elapsed = time.time() - card.last_failure_time
            if elapsed < cooldown:
                return 0.0, f"cooldown ({cooldown - elapsed:.0f}s remaining)"

        priority_score = self._priority_score(entry.priority)
        speed_score = self._speed_score(card)
        success_rate = card.success_rate
        stability_score = self._stability_score(card.consecutive_failures, threshold)

        composite = (
            W_PRIORITY * priority_score
            + W_SPEED * speed_score
            + W_SUCCESS * success_rate
            + W_STABILITY * stability_score
        )

        return round(composite, 4), "ok"

    @staticmethod
    def _priority_score(priority: int) -> float:
        return max(1.0 / priority, 0.05) if priority > 0 else 1.0

    @staticmethod
    def _speed_score(card: ScoreCard) -> float:
        recent = card.recent_entries(within_seconds=300.0)
        recent_count = len(recent)

        actual_ttft = card.avg_ttft_seconds
        probe_ttft = card.probe_ttft_ms / 1000.0 if card.probe_ttft_ms > 0 else None

        if actual_ttft is not None and recent_count >= 10:
            ttft = actual_ttft
        elif actual_ttft is not None and probe_ttft is not None:
            w = min(recent_count / 10.0, 1.0)
            ttft = w * actual_ttft + (1.0 - w) * probe_ttft
        elif probe_ttft is not None:
            ttft = probe_ttft
        else:
            return 0.5

        if ttft <= 0:
            return 1.0
        return max(1.0 / (1.0 + ttft), 0.05)

    @staticmethod
    def _stability_score(consecutive_failures: int, threshold: int) -> float:
        if consecutive_failures == 0:
            return 1.0
        return max(1.0 - consecutive_failures / max(threshold, 1), 0.0)
