"""
Simple in-memory metrics backend.

Zero external dependencies. Suitable for development, testing,
or lightweight deployments.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Tuple

from .collector import MetricsCollector
from .registry import MetricDef, MetricKind


def _make_key(**labels: str) -> Tuple[Tuple[str, str], ...]:
    """Build a hashable key from label kwargs."""
    return tuple(sorted(labels.items()))


class SimpleCollector(MetricsCollector):
    """In-memory metrics collector backed by Python dicts."""

    def __init__(self) -> None:
        self._counters: Dict[str, Dict[Tuple, float]] = defaultdict(lambda: defaultdict(float))
        self._gauges: Dict[str, Dict[Tuple, float]] = defaultdict(dict)
        self._histograms: Dict[str, Dict[Tuple, List[float]]] = defaultdict(lambda: defaultdict(list))

    def inc(self, metric: MetricDef, value: float = 1.0, **labels: str) -> None:
        key = _make_key(**labels)
        self._counters[metric.name][key] += value

    def observe(self, metric: MetricDef, value: float, **labels: str) -> None:
        key = _make_key(**labels)
        self._histograms[metric.name][key].append(value)

    def set(self, metric: MetricDef, value: float, **labels: str) -> None:
        key = _make_key(**labels)
        self._gauges[metric.name][key] = value

    # ================================================================
    # Debugging
    # ================================================================

    def get_summary(self) -> dict:
        """Dump all collected metrics for debugging/logging."""
        return {
            "counters": {
                name: {str(k): v for k, v in labels_map.items()}
                for name, labels_map in self._counters.items()
            },
            "gauges": {
                name: {str(k): v for k, v in labels_map.items()}
                for name, labels_map in self._gauges.items()
            },
            "histograms": {
                name: {
                    str(k): {"count": len(values), "sum": sum(values)}
                    for k, values in labels_map.items()
                }
                for name, labels_map in self._histograms.items()
            },
        }
