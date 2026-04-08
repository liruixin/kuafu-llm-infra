"""
Fallback - Degradation strategy engine.
"""

from .engine import FallbackEngine, AllProvidersExhausted
from .stream_monitor import StreamMonitor, StrategyTriggered
from .recorder import RequestRecorder
from .scorer import Scorer, ScoredProvider
from .health_checker import HealthChecker
from .strategies import (
    BaseStrategy,
    StrategyEvent,
    StrategyAction,
    create_strategies,
)

__all__ = [
    "FallbackEngine",
    "AllProvidersExhausted",
    "StrategyTriggered",
    "StreamMonitor",
    "RequestRecorder",
    "Scorer",
    "ScoredProvider",
    "HealthChecker",
    "BaseStrategy",
    "StrategyEvent",
    "StrategyAction",
    "create_strategies",
]
