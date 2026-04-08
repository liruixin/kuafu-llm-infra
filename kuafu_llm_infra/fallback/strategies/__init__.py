"""
Pluggable fallback strategies.
"""

from .base import BaseStrategy, StrategyEvent, StrategyAction
from .ttft_timeout import TtftTimeoutStrategy
from .empty_frame import EmptyFrameStrategy
from .chunk_gap import ChunkGapStrategy
from .slow_speed import SlowSpeedStrategy
from .total_timeout import TotalTimeoutStrategy

__all__ = [
    "BaseStrategy",
    "StrategyEvent",
    "StrategyAction",
    "TtftTimeoutStrategy",
    "EmptyFrameStrategy",
    "ChunkGapStrategy",
    "SlowSpeedStrategy",
    "TotalTimeoutStrategy",
]
