"""
Metrics - Monitoring data collection module.

Pluggable metrics backends: Prometheus, in-memory, or custom.
"""

from .collector import MetricsCollector, NoopCollector
from .simple import SimpleCollector
from .prometheus import PrometheusCollector

__all__ = [
    "MetricsCollector",
    "NoopCollector",
    "SimpleCollector",
    "PrometheusCollector",
]
