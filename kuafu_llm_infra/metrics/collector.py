"""
Abstract metrics collector interface.

Three generic methods replace 12 specific ones:
- ``inc(metric, value, **labels)`` — counters
- ``observe(metric, value, **labels)`` — histograms
- ``set(metric, value, **labels)`` — gauges

Metric definitions live in ``metrics.registry``.  Backends iterate
``ALL_METRICS`` to auto-register instruments — adding a new metric
requires zero changes in any backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .registry import MetricDef


class MetricsCollector(ABC):
    """Abstract metrics collector with 3 generic recording methods."""

    @abstractmethod
    def inc(self, metric: MetricDef, value: float = 1.0, **labels: str) -> None:
        """Increment a counter metric."""
        ...

    @abstractmethod
    def observe(self, metric: MetricDef, value: float, **labels: str) -> None:
        """Observe a value for a histogram metric."""
        ...

    @abstractmethod
    def set(self, metric: MetricDef, value: float, **labels: str) -> None:
        """Set a gauge metric to an absolute value."""
        ...


class NoopCollector(MetricsCollector):
    """Collector that discards everything. Used when metrics are disabled."""

    def inc(self, metric: MetricDef, value: float = 1.0, **labels: str) -> None:
        pass

    def observe(self, metric: MetricDef, value: float, **labels: str) -> None:
        pass

    def set(self, metric: MetricDef, value: float, **labels: str) -> None:
        pass
