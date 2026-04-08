"""
Abstract metrics collector interface.

Defines the metric types that the library records at every
decision point. Two implementations are provided:

- ``SimpleCollector``: in-memory counters (zero dependencies)
- ``PrometheusCollector``: registers real Prometheus metrics
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class MetricsCollector(ABC):
    """Abstract metrics collector."""

    # --- Request-level metrics ---

    @abstractmethod
    def inc_request_total(
        self,
        business_key: str,
        model: str,
        provider: str,
        status: str,
    ) -> None:
        """Increment request counter. status: success|error|timeout|fallback"""
        ...

    @abstractmethod
    def observe_request_duration(
        self,
        business_key: str,
        model: str,
        provider: str,
        duration: float,
    ) -> None:
        """Record request duration in seconds."""
        ...

    @abstractmethod
    def observe_ttft(
        self,
        business_key: str,
        model: str,
        provider: str,
        ttft: float,
    ) -> None:
        """Record time-to-first-token in seconds."""
        ...

    @abstractmethod
    def observe_tokens_per_second(
        self,
        business_key: str,
        model: str,
        provider: str,
        tps: float,
    ) -> None:
        """Record token throughput."""
        ...

    # --- Fallback events ---

    @abstractmethod
    def inc_fallback_total(
        self,
        business_key: str,
        model: str,
        from_provider: str,
        to_provider: str,
        reason: str,
    ) -> None:
        """Increment fallback counter."""
        ...

    @abstractmethod
    def inc_strategy_triggered(
        self,
        business_key: str,
        model: str,
        provider: str,
        strategy: str,
    ) -> None:
        """Increment strategy trigger counter."""
        ...

    # --- Provider-level metrics ---

    @abstractmethod
    def set_provider_health(self, provider: str, healthy: bool) -> None:
        """Set provider health gauge."""
        ...

    @abstractmethod
    def set_provider_score(self, model: str, provider: str, score: float) -> None:
        """Set provider composite score gauge."""
        ...

    @abstractmethod
    def observe_probe_ttft(self, provider: str, ttft: float) -> None:
        """Record probe TTFT in seconds."""
        ...

    @abstractmethod
    def inc_probe_total(self, provider: str, status: str) -> None:
        """Increment probe counter. status: success|error"""
        ...


class NoopCollector(MetricsCollector):
    """Collector that discards everything. Used when metrics are disabled."""

    def inc_request_total(self, *a, **kw) -> None: pass
    def observe_request_duration(self, *a, **kw) -> None: pass
    def observe_ttft(self, *a, **kw) -> None: pass
    def observe_tokens_per_second(self, *a, **kw) -> None: pass
    def inc_fallback_total(self, *a, **kw) -> None: pass
    def inc_strategy_triggered(self, *a, **kw) -> None: pass
    def set_provider_health(self, *a, **kw) -> None: pass
    def set_provider_score(self, *a, **kw) -> None: pass
    def observe_probe_ttft(self, *a, **kw) -> None: pass
    def inc_probe_total(self, *a, **kw) -> None: pass
