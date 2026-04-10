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
# Provider config (pure credentials, no model info)
# ============================================================================

class EndpointConfig(BaseModel):
    """A single protocol endpoint under a provider."""
    base_url: str = ""


class ProviderConfig(BaseModel):
    """
    Single LLM provider connection credentials.

    A provider can expose multiple protocol endpoints (openai, anthropic, etc.),
    each with its own base_url. Model entries reference a specific endpoint.
    """
    name: str = ""
    api_key: str = ""
    endpoints: Dict[str, EndpointConfig] = Field(default_factory=dict)
    enabled: bool = True


def adapter_key(provider: str, endpoint: str) -> str:
    """Build the internal adapter key from provider name and endpoint type."""
    return f"{provider}:{endpoint}"


# ============================================================================
# Model config (model is the first-class citizen)
# ============================================================================

class ModelProviderEntry(BaseModel):
    """One provider's entry under a model definition."""
    provider: str                            # references key in providers dict
    endpoint: str                            # references key in provider's endpoints dict
    model_id: Optional[str] = None           # actual ID at this provider; None → same as model key
    priority: int = 99
    probe: bool = True                       # whether to health-probe this (model, provider)


class ModelConfig(BaseModel):
    """
    Configuration for a single canonical model.

    The dict key is the official model ID (e.g. ``claude-opus-4-5-20251101``).
    Each entry lists the providers that offer this model.
    """
    providers: List[ModelProviderEntry] = Field(default_factory=list)


# ============================================================================
# Strategy config
# ============================================================================


class TimeoutConfig(BaseModel):
    """Timeout thresholds for a strategy."""
    ttft: float = 8.0
    chunk_gap: float = 15.0
    total: float = 60.0


class StrategyConfig(BaseModel):
    """
    Business-key level strategy configuration.

    ``primary`` and ``fallback`` reference canonical model IDs
    (keys in the ``models`` dict).
    """
    primary: str                              # canonical model ID
    fallback: List[str] = Field(default_factory=list)  # ordered fallback model IDs
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
    label_keys: List[str] = Field(default_factory=list)  # Custom label names for request-level metrics


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
    models: Dict[str, ModelConfig] = Field(default_factory=dict)
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

    def resolve_model_id(self, canonical_model: str, adapter_key_str: str) -> str:
        """Resolve the actual model_id for a (model, adapter_key) pair."""
        model_cfg = self.models.get(canonical_model)
        if model_cfg:
            for entry in model_cfg.providers:
                if adapter_key(entry.provider, entry.endpoint) == adapter_key_str:
                    return entry.model_id or canonical_model
        return canonical_model

    def get_model_providers(self, canonical_model: str) -> List[ModelProviderEntry]:
        """Get all provider entries for a canonical model."""
        model_cfg = self.models.get(canonical_model)
        if model_cfg:
            return model_cfg.providers
        return []

    def validate_references(self) -> None:
        """
        Validate cross-references in the config.

        Raises ``ValueError`` with a summary of all broken references.
        """
        errors: List[str] = []

        # Each model must reference existing, enabled providers and valid endpoints
        for model_name, model_cfg in self.models.items():
            enabled_count = 0
            for entry in model_cfg.providers:
                provider_cfg = self.providers.get(entry.provider)
                if provider_cfg is None:
                    errors.append(
                        f"Model '{model_name}' references unknown provider '{entry.provider}'"
                    )
                elif entry.endpoint not in provider_cfg.endpoints:
                    errors.append(
                        f"Model '{model_name}' references unknown endpoint "
                        f"'{entry.endpoint}' on provider '{entry.provider}'"
                    )
                elif provider_cfg.enabled:
                    enabled_count += 1
            if enabled_count == 0 and model_cfg.providers:
                errors.append(
                    f"Model '{model_name}' has no enabled providers"
                )

        # Each strategy must reference existing models
        for key, strategy in self.strategies.items():
            if strategy.primary not in self.models:
                errors.append(
                    f"Strategy '{key}' references unknown primary model '{strategy.primary}'"
                )
            for fb in strategy.fallback:
                if fb not in self.models:
                    errors.append(
                        f"Strategy '{key}' references unknown fallback model '{fb}'"
                    )

        if errors:
            raise ValueError(
                "Config reference validation failed:\n  - " + "\n  - ".join(errors)
            )
