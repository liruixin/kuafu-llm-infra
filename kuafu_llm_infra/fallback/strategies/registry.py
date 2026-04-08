"""
Strategy registry — declarative strategy registration.

Each strategy module self-registers via ``@register_strategy``.
The engine calls ``create_strategies()`` to build all active
strategies for a request, eliminating hardcoded class lists.

Adding a new strategy:
    1. Create ``my_strategy.py`` implementing ``BaseStrategy``
    2. At module level: ``@register_strategy``
    3. Done — the engine picks it up automatically
"""

from __future__ import annotations

from typing import Callable, Dict, List

from ...config.schema import StrategyConfig
from .base import BaseStrategy

# strategy_name → factory(cfg, provider, model) → BaseStrategy | None
_REGISTRY: Dict[str, Callable[..., BaseStrategy | None]] = {}


def register_strategy(factory: Callable[..., BaseStrategy | None]) -> Callable:
    """
    Decorator that registers a strategy factory.

    The factory signature must be:
        ``(cfg: StrategyConfig, provider: str, model: str) -> BaseStrategy | None``

    Returning ``None`` means "not applicable for this config".
    """
    # Use a sentinel attribute on the factory to derive the name lazily
    name = getattr(factory, "_strategy_name", factory.__name__)
    _REGISTRY[name] = factory
    return factory


def create_strategies(
    cfg: StrategyConfig,
    provider: str,
    model: str,
) -> List[BaseStrategy]:
    """Instantiate all registered strategies for a given request config."""
    result = []
    for factory in _REGISTRY.values():
        strategy = factory(cfg, provider, model)
        if strategy is not None:
            result.append(strategy)
    return result
