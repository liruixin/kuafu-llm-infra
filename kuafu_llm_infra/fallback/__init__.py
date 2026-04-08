"""
Fallback - Degradation strategy engine.
"""

from .engine import FallbackEngine, StrategyTriggered, AllProvidersExhausted
from .scorer import Scorer, ScoredProvider
from .health_checker import HealthChecker
from .strategies import (
    BaseStrategy,
    StrategyEvent,
    StrategyAction,
    TtftTimeoutStrategy,
    EmptyFrameStrategy,
    ChunkGapStrategy,
    SlowSpeedStrategy,
    TotalTimeoutStrategy,
)

__all__ = [
    "FallbackEngine",
    "StrategyTriggered",
    "AllProvidersExhausted",
    "Scorer",
    "ScoredProvider",
    "HealthChecker",
    "BaseStrategy",
    "StrategyEvent",
    "StrategyAction",
    "TtftTimeoutStrategy",
    "EmptyFrameStrategy",
    "ChunkGapStrategy",
    "SlowSpeedStrategy",
    "TotalTimeoutStrategy",
]
