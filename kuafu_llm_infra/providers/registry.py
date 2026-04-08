"""
Provider registry — declarative provider type registration.

Each provider module self-registers via the ``@register_provider``
decorator. The gateway uses ``create_provider()`` to instantiate
adapters by type name, eliminating URL-based heuristics.

Adding a new provider:
    1. Create ``my_provider.py`` implementing ``BaseProvider``
    2. At module level: ``@register_provider("my_type")``
    3. Add ``type: my_type`` to the provider config in YAML
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from .base import BaseProvider

logger = logging.getLogger("kuafu_llm_infra.providers.registry")

# type_name → factory(api_key, base_url, extra_headers) → BaseProvider
_REGISTRY: Dict[str, Callable[..., BaseProvider]] = {}


def register_provider(type_name: str) -> Callable:
    """Decorator that registers a provider factory under ``type_name``."""
    def decorator(factory: Callable[..., BaseProvider]) -> Callable[..., BaseProvider]:
        if type_name in _REGISTRY:
            logger.warning(f"Provider type '{type_name}' registered twice — overwriting")
        _REGISTRY[type_name] = factory
        return factory
    return decorator


def create_provider(
    provider_type: str,
    *,
    api_key: str,
    base_url: str,
    extra_headers: Optional[Dict[str, str]] = None,
) -> BaseProvider:
    """
    Instantiate a provider adapter by its registered type name.

    Raises ``ValueError`` if the type is unknown.
    """
    factory = _REGISTRY.get(provider_type)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(
            f"Unknown provider type '{provider_type}'. "
            f"Available: {available}"
        )
    return factory(api_key=api_key, base_url=base_url, extra_headers=extra_headers)


def registered_types() -> list[str]:
    """Return all registered provider type names."""
    return sorted(_REGISTRY)
