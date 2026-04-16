"""
Core types used across the library.

These are the foundational data structures that flow through
providers, engine, metrics, and gateway layers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token consumption for a single request."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class RequestContext:
    """
    Request metadata flowing through the fallback engine.

    Created at the gateway layer and enriched by the engine
    as it resolves strategy and provider.
    """
    business_key: str
    messages: List[Dict[str, Any]]
    labels: Dict[str, str] = field(default_factory=dict)
    max_tokens: int = 4096
    temperature: Optional[float] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[str] = None
    extra_kwargs: Dict[str, Any] = field(default_factory=dict)

    # Filled by the engine during execution
    canonical_model: str = ""
    provider_name: str = ""
    actual_model_id: str = ""


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """一次请求的完整记录，含高维度 labels，用于 ClickHouse 等独立存储。"""
    business_key: str
    canonical_model: str
    provider_name: str
    labels: Dict[str, str]
    status: str                          # "success" | "error"
    duration_ms: int                     # 总耗时（毫秒）
    ttft_ms: int = 0                     # 首 token 延迟（毫秒）
    tps: float = 0.0                     # tokens/sec
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    error_reason: str = ""
    error_detail: str = ""
    timestamp: float = field(default_factory=time.time)
