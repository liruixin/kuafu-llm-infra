"""
State backend abstraction.

Defines the interface for score card storage, probe coordination,
request metrics aggregation, and config broadcast. Two implementations:

- ``MemoryBackend``: in-process, zero dependencies (default)
- ``RedisBackend``: multi-instance shared state (optional)
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Callable


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class RequestOutcome:
    """Result of a single LLM request, fed into the scorer."""
    success: bool
    ttft_seconds: Optional[float] = None
    tokens_per_second: Optional[float] = None
    duration_seconds: Optional[float] = None
    failure_reason: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ProbeResult:
    """Result of a background health/speed probe."""
    provider: str
    model: str = ""
    health: bool = True
    ttft_ms: float = 0.0
    valid_response: bool = True
    timestamp: float = field(default_factory=time.time)


@dataclass
class SlidingWindowEntry:
    """Single entry in the sliding window."""
    success: bool
    ttft_seconds: Optional[float] = None
    tokens_per_second: Optional[float] = None
    duration_seconds: Optional[float] = None
    failure_reason: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ScoreCard:
    """
    Per-(model, provider) scoring state.

    Combines data from actual requests (sliding window) and
    background probes to produce a composite score.
    """
    # --- From actual requests ---
    window: List[SlidingWindowEntry] = field(default_factory=list)
    consecutive_failures: int = 0
    last_failure_time: float = 0.0

    # --- From background probes ---
    health: bool = True
    probe_ttft_ms: float = 0.0
    probe_valid: bool = True
    probe_time: float = 0.0

    # --- Sliding window config ---
    window_size: int = 20
    window_ttl: float = 300.0  # 5 minutes

    def push_request(self, outcome: RequestOutcome) -> None:
        """Record a request outcome into the sliding window."""
        entry = SlidingWindowEntry(
            success=outcome.success,
            ttft_seconds=outcome.ttft_seconds,
            tokens_per_second=outcome.tokens_per_second,
            duration_seconds=outcome.duration_seconds,
            failure_reason=outcome.failure_reason,
            timestamp=outcome.timestamp,
        )
        self.window.append(entry)
        if len(self.window) > self.window_size:
            self.window = self.window[-self.window_size:]

        if outcome.success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            self.last_failure_time = outcome.timestamp

    def update_probe(self, result: ProbeResult) -> None:
        """Update probe data."""
        self.health = result.health
        self.probe_ttft_ms = result.ttft_ms
        self.probe_valid = result.valid_response
        self.probe_time = result.timestamp

    def recent_entries(self, within_seconds: float = 300.0) -> List[SlidingWindowEntry]:
        """Return entries within the given time window."""
        cutoff = time.time() - within_seconds
        return [e for e in self.window if e.timestamp >= cutoff]

    @property
    def success_rate(self) -> float:
        """Success rate over the recent window."""
        recent = self.recent_entries()
        if not recent:
            return 1.0 if self.health else 0.0
        return sum(1 for e in recent if e.success) / len(recent)

    @property
    def avg_ttft_seconds(self) -> Optional[float]:
        """Average TTFT from recent successful requests."""
        recent = self.recent_entries()
        ttfts = [e.ttft_seconds for e in recent
                 if e.success and e.ttft_seconds is not None]
        if not ttfts:
            return None
        return sum(ttfts) / len(ttfts)


@dataclass
class AggregatedStats:
    """Aggregated request statistics (for multi-instance scenarios)."""
    total: int = 0
    success: int = 0
    failure: int = 0

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 1.0
        return self.success / self.total


# ============================================================================
# Backend interface
# ============================================================================

class StateBackend(ABC):
    """Abstract state storage backend."""

    # --- Score card ---
    @abstractmethod
    async def get_score_card(self, model: str, provider: str) -> ScoreCard:
        ...

    @abstractmethod
    async def update_score_card(self, model: str, provider: str, card: ScoreCard) -> None:
        ...

    # --- Probe coordination (per model+provider) ---
    @abstractmethod
    async def try_acquire_probe_lock(self, model: str, provider: str, ttl: float) -> bool:
        ...

    @abstractmethod
    async def set_probe_result(self, model: str, provider: str, result: ProbeResult) -> None:
        ...

    @abstractmethod
    async def get_probe_result(self, model: str, provider: str) -> Optional[ProbeResult]:
        ...

    # --- Request metrics aggregation ---
    @abstractmethod
    async def record_request(self, model: str, provider: str, outcome: RequestOutcome) -> None:
        ...

    @abstractmethod
    async def get_aggregated_stats(self, model: str, provider: str) -> AggregatedStats:
        ...

    # --- Config broadcast (no-op for memory backend) ---
    async def publish_config(self, config_json: str) -> None:
        """Publish config update to other instances. No-op for memory backend."""
        pass

    async def subscribe_config(self, callback: Callable[[str], None]) -> None:
        """Subscribe to config updates from other instances. No-op for memory backend."""
        pass

    async def unsubscribe_config(self) -> None:
        """Stop config subscription. No-op for memory backend."""
        pass
