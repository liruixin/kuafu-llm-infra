"""
Redis state backend for multi-instance deployments.

Provides shared score cards, probe coordination (distributed locks),
and aggregated request metrics across multiple service instances.

Requires: ``pip install redis``
"""

from __future__ import annotations

import json
import time
import uuid
import logging
from typing import Dict, Optional, Tuple

from .backend import (
    StateBackend,
    ScoreCard,
    ProbeResult,
    RequestOutcome,
    AggregatedStats,
    SlidingWindowEntry,
)

logger = logging.getLogger("kuafu_llm_infra.state.redis")


class RedisBackend(StateBackend):
    """Redis-backed state storage for multi-instance coordination."""

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        key_prefix: str = "llm_infra:",
    ) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise ImportError(
                "redis package is required for RedisBackend. "
                "Install with: pip install redis"
            )

        self._redis = aioredis.from_url(url, decode_responses=True)
        self._prefix = key_prefix
        self._instance_id = str(uuid.uuid4())[:8]

    def _key(self, *parts: str) -> str:
        return self._prefix + ":".join(parts)

    # --- Score card ---

    async def get_score_card(self, model: str, provider: str) -> ScoreCard:
        key = self._key("scorecard", model, provider)
        raw = await self._redis.get(key)
        if raw:
            return self._deserialize_score_card(raw)
        return ScoreCard()

    async def update_score_card(self, model: str, provider: str, card: ScoreCard) -> None:
        key = self._key("scorecard", model, provider)
        await self._redis.set(key, self._serialize_score_card(card), ex=600)

    # --- Probe coordination ---

    async def try_acquire_probe_lock(self, provider: str, ttl: float) -> bool:
        key = self._key("probe_lock", provider)
        result = await self._redis.set(
            key, self._instance_id, nx=True, ex=int(ttl),
        )
        return result is not None

    async def set_probe_result(self, provider: str, result: ProbeResult) -> None:
        key = self._key("probe", provider)
        data = json.dumps({
            "provider": result.provider,
            "health": result.health,
            "ttft_ms": result.ttft_ms,
            "valid_response": result.valid_response,
            "timestamp": result.timestamp,
        })
        await self._redis.set(key, data, ex=120)

    async def get_probe_result(self, provider: str) -> Optional[ProbeResult]:
        key = self._key("probe", provider)
        raw = await self._redis.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        return ProbeResult(**data)

    # --- Request metrics aggregation ---

    async def record_request(self, model: str, provider: str, outcome: RequestOutcome) -> None:
        stats_key = self._key("stats", model, provider)
        pipe = self._redis.pipeline()
        pipe.hincrby(stats_key, "total", 1)
        if outcome.success:
            pipe.hincrby(stats_key, "success", 1)
        else:
            pipe.hincrby(stats_key, "failure", 1)
        pipe.expire(stats_key, 300)
        await pipe.execute()

        # Also update score card
        card = await self.get_score_card(model, provider)
        card.push_request(outcome)
        await self.update_score_card(model, provider, card)

    async def get_aggregated_stats(self, model: str, provider: str) -> AggregatedStats:
        key = self._key("stats", model, provider)
        data = await self._redis.hgetall(key)
        if not data:
            return AggregatedStats()
        return AggregatedStats(
            total=int(data.get("total", 0)),
            success=int(data.get("success", 0)),
            failure=int(data.get("failure", 0)),
        )

    # --- Serialisation helpers ---

    @staticmethod
    def _serialize_score_card(card: ScoreCard) -> str:
        window_data = []
        for entry in card.window[-card.window_size:]:
            window_data.append({
                "success": entry.success,
                "ttft_seconds": entry.ttft_seconds,
                "tokens_per_second": entry.tokens_per_second,
                "duration_seconds": entry.duration_seconds,
                "failure_reason": entry.failure_reason,
                "timestamp": entry.timestamp,
            })
        return json.dumps({
            "window": window_data,
            "consecutive_failures": card.consecutive_failures,
            "last_failure_time": card.last_failure_time,
            "health": card.health,
            "probe_ttft_ms": card.probe_ttft_ms,
            "probe_valid": card.probe_valid,
            "probe_time": card.probe_time,
        })

    @staticmethod
    def _deserialize_score_card(raw: str) -> ScoreCard:
        data = json.loads(raw)
        card = ScoreCard(
            consecutive_failures=data.get("consecutive_failures", 0),
            last_failure_time=data.get("last_failure_time", 0.0),
            health=data.get("health", True),
            probe_ttft_ms=data.get("probe_ttft_ms", 0.0),
            probe_valid=data.get("probe_valid", True),
            probe_time=data.get("probe_time", 0.0),
        )
        for entry_data in data.get("window", []):
            card.window.append(SlidingWindowEntry(
                success=entry_data["success"],
                ttft_seconds=entry_data.get("ttft_seconds"),
                tokens_per_second=entry_data.get("tokens_per_second"),
                duration_seconds=entry_data.get("duration_seconds"),
                failure_reason=entry_data.get("failure_reason"),
                timestamp=entry_data.get("timestamp", 0.0),
            ))
        return card
