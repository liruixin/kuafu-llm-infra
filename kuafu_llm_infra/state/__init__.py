"""
State - Backend abstraction for score card storage and probe coordination.
"""

from .backend import (
    StateBackend,
    ScoreCard,
    ProbeResult,
    RequestOutcome,
    AggregatedStats,
    SlidingWindowEntry,
)
from .memory import MemoryBackend

__all__ = [
    "StateBackend",
    "ScoreCard",
    "ProbeResult",
    "RequestOutcome",
    "AggregatedStats",
    "SlidingWindowEntry",
    "MemoryBackend",
]
