"""
Metrics - Monitoring data collection module.

Pluggable metrics backends: Prometheus, in-memory, or custom.
Metric definitions are centralized in ``metrics.registry``.
"""

from .collector import MetricsCollector, NoopCollector
from .simple import SimpleCollector
from .prometheus import PrometheusCollector
from . import registry

__all__ = [
    "MetricsCollector",
    "NoopCollector",
    "SimpleCollector",
    "PrometheusCollector",
    "registry",
]
