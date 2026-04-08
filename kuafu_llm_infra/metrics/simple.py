"""
Simple in-memory metrics backend.

Zero external dependencies. Suitable for development, testing,
or lightweight deployments.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Tuple

from .collector import MetricsCollector


class SimpleCollector(MetricsCollector):
    """In-memory metrics collector backed by Python dicts."""

    def __init__(self) -> None:
        self._counters: Dict[str, Dict[Tuple, int]] = defaultdict(lambda: defaultdict(int))
        self._gauges: Dict[str, Dict[Tuple, float]] = defaultdict(dict)
        self._histograms: Dict[str, Dict[Tuple, List[float]]] = defaultdict(lambda: defaultdict(list))

    # --- Request-level metrics ---

    def inc_request_total(
        self, business_key: str, model: str, provider: str, status: str,
    ) -> None:
        self._counters["request_total"][(business_key, model, provider, status)] += 1

    def observe_request_duration(
        self, business_key: str, model: str, provider: str, duration: float,
    ) -> None:
        self._histograms["request_duration"][(business_key, model, provider)].append(duration)

    def observe_ttft(
        self, business_key: str, model: str, provider: str, ttft: float,
    ) -> None:
        self._histograms["ttft"][(business_key, model, provider)].append(ttft)

    def observe_tokens_per_second(
        self, business_key: str, model: str, provider: str, tps: float,
    ) -> None:
        self._histograms["tokens_per_second"][(business_key, model, provider)].append(tps)

    # --- Fallback events ---

    def inc_fallback_total(
        self, business_key: str, model: str, from_provider: str,
        to_provider: str, reason: str,
    ) -> None:
        self._counters["fallback_total"][
            (business_key, model, from_provider, to_provider, reason)
        ] += 1

    def inc_strategy_triggered(
        self, business_key: str, model: str, provider: str, strategy: str,
    ) -> None:
        self._counters["strategy_triggered"][(business_key, model, provider, strategy)] += 1

    # --- Provider-level metrics ---

    def set_provider_health(self, provider: str, healthy: bool) -> None:
        self._gauges["provider_health"][(provider,)] = 1.0 if healthy else 0.0

    def set_provider_score(self, model: str, provider: str, score: float) -> None:
        self._gauges["provider_score"][(model, provider)] = score

    def observe_probe_ttft(self, provider: str, ttft: float) -> None:
        self._histograms["probe_ttft"][(provider,)].append(ttft)

    def inc_probe_total(self, provider: str, status: str) -> None:
        self._counters["probe_total"][(provider, status)] += 1

    # --- Debugging ---

    def get_summary(self) -> dict:
        """Dump all collected metrics for debugging/logging."""
        return {
            "counters": {
                name: dict(labels_map)
                for name, labels_map in self._counters.items()
            },
            "gauges": {
                name: dict(labels_map)
                for name, labels_map in self._gauges.items()
            },
            "histograms": {
                name: {
                    labels: {"count": len(values), "sum": sum(values)}
                    for labels, values in labels_map.items()
                }
                for name, labels_map in self._histograms.items()
            },
        }
