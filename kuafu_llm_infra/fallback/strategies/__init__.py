"""
Pluggable fallback strategies.

Strategy modules self-register via ``@register_strategy``.
Use ``create_strategies()`` to build all strategies for a request.
"""

from .base import BaseStrategy, StrategyEvent, StrategyAction
from .registry import create_strategies, register_strategy

# Import strategy modules to trigger self-registration
from . import ttft_timeout as _ttft  # noqa: F401
from . import empty_frame as _empty  # noqa: F401
from . import chunk_gap as _gap  # noqa: F401
from . import slow_speed as _slow  # noqa: F401
from . import total_timeout as _total  # noqa: F401

__all__ = [
    "BaseStrategy",
    "StrategyEvent",
    "StrategyAction",
    "create_strategies",
    "register_strategy",
]
