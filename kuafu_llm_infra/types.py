"""
Core types used across the library.

These are the foundational data structures that flow through
providers, engine, metrics, and gateway layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token consumption for a single request."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class RequestContext:
    """
    Request metadata flowing through the fallback engine.

    Created at the gateway layer and enriched by the engine
    as it resolves strategy, model, and provider.
    """
    business_key: str
    messages: List[Dict[str, Any]]
    labels: Dict[str, str] = field(default_factory=dict)
    model: Optional[str] = None
    max_tokens: int = 4096
    temperature: Optional[float] = None
    extra_kwargs: Dict[str, Any] = field(default_factory=dict)

    # Filled by the engine during execution
    canonical_model: str = ""
    provider_name: str = ""
    actual_model_id: str = ""
