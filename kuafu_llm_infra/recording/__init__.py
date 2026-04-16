"""
Recording — async request-level data collection for high-dimensional analytics.

Collects complete request records (including high-cardinality labels)
into ClickHouse or custom sinks, decoupled from Prometheus metrics.
"""

from .base import BaseRecordSink
from .dispatcher import RecordDispatcher

__all__ = [
    "BaseRecordSink",
    "RecordDispatcher",
]
