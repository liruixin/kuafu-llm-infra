"""
Configuration data structures and validation.

All configuration is defined as Pydantic models for automatic
validation, serialization, and clear documentation.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Any
from enum import Enum
from pydantic import BaseModel, Field


# ============================================================================
# Provider config
# ============================================================================

class ProviderConfig(BaseModel):
    """Single LLM provider configuration."""
    name: str = ""
    api_key: str = ""
    openai_base_url: Optional[str] = None
    anthropic_base_url: Optional[str] = None
    ping_model: str = ""
    priority: int = 99
    enabled: bool = True
    model_mapping: Dict[str, str] = Field(default_factory=dict)

    def get_base_url(self, interface: str) -> Optional[str]:
        if interface == "openai":
            return self.openai_base_url
        if interface == "google":
            return None
        return self.anthropic_base_url


# ============================================================================
# Model & strategy config
# ============================================================================

class ModelRouteConfig(BaseModel):
    """A model with its provider list."""
    model: str
    providers: List[str]


class StrategyMode(str, Enum):
    STREAM = "stream"
    BLOCK = "block"


class TimeoutConfig(BaseModel):
    """Timeout thresholds for a strategy."""
    ttft: float = 8.0
    chunk_gap: float = 15.0
    total: float = 60.0


class StrategyConfig(BaseModel):
    """Business-key level strategy configuration."""
    mode: StrategyMode = StrategyMode.STREAM
    primary: ModelRouteConfig
    fallback: List[ModelRouteConfig] = Field(default_factory=list)
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    empty_frame_threshold: int = 5
    slow_speed_threshold: float = 5.0  # tokens/sec


# ============================================================================
# Health check & probe config
# ============================================================================

class HealthCheckConfig(BaseModel):
    """Background probing configuration."""
    interval: float = 20.0
    failure_threshold: int = 3
    recovery_threshold: int = 3
    timeout: float = 10.0
    probe_max_tokens: int = 5
    stagger_interval: float = 2.0
    cooldown: float = 60.0


# ============================================================================
# Metrics config
# ============================================================================

class MetricsConfig(BaseModel):
    """Metrics backend configuration."""
    enabled: bool = True
    backend: str = "simple"  # "simple" | "prometheus"
    port: Optional[int] = None  # Only for prometheus


# ============================================================================
# Alert config
# ============================================================================

class AlertChannelConfig(BaseModel):
    """Single alert channel configuration."""
    type: str = "log"  # "log" | "feishu" | "webhook"
    webhook_url: Optional[str] = None
    url: Optional[str] = None


class AlertRulesConfig(BaseModel):
    """Alert deduplication and rate limiting rules."""
    silence_seconds: float = 60.0


class AlertConfig(BaseModel):
    """Alert system configuration."""
    channels: List[AlertChannelConfig] = Field(
        default_factory=lambda: [AlertChannelConfig(type="log")]
    )
    rules: AlertRulesConfig = Field(default_factory=AlertRulesConfig)


# ============================================================================
# State backend config
# ============================================================================

class RedisConfig(BaseModel):
    """Redis connection configuration."""
    url: str = "redis://localhost:6379/0"
    key_prefix: str = "llm_infra:"


class StateBackendConfig(BaseModel):
    """State backend configuration."""
    type: str = "memory"  # "memory" | "redis"
    redis: Optional[RedisConfig] = None


# ============================================================================
# Top-level config
# ============================================================================

class LLMStabilityConfig(BaseModel):
    """
    Top-level configuration for kuafu-llm-infra.

    This is the root config parsed from the ``llm_stability`` key
    in the user's YAML file.
    """
    providers: Dict[str, ProviderConfig] = Field(default_factory=dict)
    strategies: Dict[str, StrategyConfig] = Field(default_factory=dict)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    alert: AlertConfig = Field(default_factory=AlertConfig)
    state_backend: StateBackendConfig = Field(default_factory=StateBackendConfig)

    def model_post_init(self, __context: Any) -> None:
        # Inject provider name into each ProviderConfig
        for name, provider in self.providers.items():
            if not provider.name:
                provider.name = name
