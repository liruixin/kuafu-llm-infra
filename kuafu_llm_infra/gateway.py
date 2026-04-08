"""
Gateway - OpenAI-compatible unified LLM calling interface.

Provides a drop-in replacement for ``AsyncOpenAI`` that internally
handles provider selection, fallback, metrics, and alerting.

Usage::

    from kuafu_llm_infra import create_client

    client = create_client("llm_stability.yaml")

    # Streaming (OpenAI-compatible)
    stream = await client.chat.completions.create(
        model="claude-opus-4-5-20251101",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        business_key="requirement_clarify",
    )
    async for chunk in stream:
        print(chunk.content, end="")

    # Non-streaming
    response = await client.chat.completions.create(
        model="gpt-4.1-2025-04-14",
        messages=[{"role": "user", "content": "hello"}],
        business_key="code_generation",
    )
    print(response.content)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from .config.schema import LLMStabilityConfig, ProviderConfig
from .config.loader import load_config
from .config.watcher import ConfigWatcher
from .providers.base import BaseProvider, ChatResponse, StreamChunk
from .providers.openai_provider import OpenAIProvider
from .providers.anthropic_provider import AnthropicProvider
from .state.backend import StateBackend
from .state.memory import MemoryBackend
from .metrics.collector import MetricsCollector, NoopCollector
from .metrics.simple import SimpleCollector
from .alert.channels.base import AlertEvent, BaseAlertChannel
from .alert.channels.log import LogAlertChannel
from .alert.channels.feishu import FeishuAlertChannel
from .alert.channels.webhook import WebhookAlertChannel
from .alert.rules import AlertRules
from .alert.dispatcher import AlertDispatcher
from .fallback.engine import FallbackEngine
from .fallback.scorer import Scorer
from .fallback.health_checker import HealthChecker

logger = logging.getLogger("kuafu_llm_infra.gateway")


# ============================================================================
# OpenAI-compatible response wrappers
# ============================================================================

class _Choice:
    def __init__(self, content: str, finish_reason: str = "stop") -> None:
        self.message = _Message(content)
        self.finish_reason = finish_reason
        self.index = 0


class _Message:
    def __init__(self, content: str) -> None:
        self.role = "assistant"
        self.content = content


class _StreamChunkWrapper:
    """Wraps StreamChunk to be more OpenAI-like."""
    def __init__(self, chunk: StreamChunk) -> None:
        self.content = chunk.content
        self.finish_reason = chunk.finish_reason
        self.raw = chunk.raw


class _CompletionResponse:
    """OpenAI-compatible completion response."""
    def __init__(self, chat_response: ChatResponse) -> None:
        self.content = chat_response.content
        self.model = chat_response.model
        self.choices = [_Choice(chat_response.content, chat_response.finish_reason)]
        self.usage = chat_response.usage
        self.raw = chat_response.raw


class _StreamWrapper:
    """Async iterator wrapper for streaming responses."""
    def __init__(self, aiter: AsyncIterator[StreamChunk]) -> None:
        self._aiter = aiter

    def __aiter__(self):
        return self

    async def __anext__(self) -> _StreamChunkWrapper:
        chunk = await self._aiter.__anext__()
        return _StreamChunkWrapper(chunk)


# ============================================================================
# Completions namespace (mimics openai.chat.completions)
# ============================================================================

class _Completions:
    """Mimics ``client.chat.completions`` namespace."""

    def __init__(self, engine: FallbackEngine) -> None:
        self._engine = engine

    async def create(
        self,
        *,
        model: Optional[str] = None,
        messages: List[Dict[str, Any]],
        stream: bool = False,
        business_key: str = "default",
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        **kwargs: Any,
    ) -> Union[_CompletionResponse, _StreamWrapper]:
        if stream:
            aiter = self._engine.execute_chat_stream(
                business_key=business_key,
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
            return _StreamWrapper(aiter)
        else:
            response = await self._engine.execute_chat(
                business_key=business_key,
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
            return _CompletionResponse(response)


class _Chat:
    """Mimics ``client.chat`` namespace."""

    def __init__(self, engine: FallbackEngine) -> None:
        self.completions = _Completions(engine)


# ============================================================================
# LLMClient - the main gateway
# ============================================================================

class LLMClient:
    """
    OpenAI-compatible LLM client with built-in fallback,
    monitoring, and alerting.
    """

    def __init__(
        self,
        config: LLMStabilityConfig,
        config_path: Optional[str] = None,
    ) -> None:
        self._config = config
        self._config_path = config_path

        # State backend
        self._state = self._create_state_backend(config)

        # Metrics
        self._metrics = self._create_metrics(config)

        # Alert
        self._alert_dispatcher = self._create_alert_dispatcher(config)
        self._alert_queue = self._alert_dispatcher._queue if self._alert_dispatcher else None

        # Provider adapters
        self._adapters: Dict[str, BaseProvider] = {}
        self._build_adapters(config)

        # Scorer
        self._scorer = Scorer(self._state, config.health_check, self._metrics)

        # Engine
        self._engine = FallbackEngine(
            config=config,
            adapters=self._adapters,
            scorer=self._scorer,
            state=self._state,
            metrics=self._metrics,
            alert_queue=self._alert_queue,
        )

        # Health checker
        self._health_checker = HealthChecker(
            config=config.health_check,
            providers=config.providers,
            adapters=self._adapters,
            state=self._state,
            metrics=self._metrics,
            alert_queue=self._alert_queue,
        )

        # Config watcher
        self._config_watcher = ConfigWatcher(
            config_path=config_path,
            on_change=self._on_config_change,
        )

        # OpenAI-compatible interface
        self.chat = _Chat(self._engine)

        # Auto-start background tasks
        self._started = False

    def start(self) -> None:
        """Start background tasks (health checker, config watcher, alert dispatcher)."""
        if self._started:
            return
        self._health_checker.start()
        self._config_watcher.start()
        if self._alert_dispatcher:
            self._alert_dispatcher.start()
        self._started = True
        logger.info("LLMClient started")

    async def shutdown(self) -> None:
        """Gracefully stop all background tasks."""
        self._health_checker.stop()
        self._config_watcher.stop()
        if self._alert_dispatcher:
            await self._alert_dispatcher.stop()
        self._started = False
        logger.info("LLMClient shutdown")

    def update_config(self, new_config: Union[Dict[str, Any], LLMStabilityConfig]) -> None:
        """Programmatically update configuration (hot-reload)."""
        if isinstance(new_config, dict):
            new_config = LLMStabilityConfig(**new_config)
        self._on_config_change(new_config)

    # ------------------------------------------------------------------
    # Internal: setup helpers
    # ------------------------------------------------------------------

    def _build_adapters(self, config: LLMStabilityConfig) -> None:
        """Build SDK adapters for all configured providers."""
        for name, provider_cfg in config.providers.items():
            if not provider_cfg.enabled:
                continue
            adapter = self._create_adapter(provider_cfg)
            if adapter:
                self._adapters[name] = adapter

    def _create_adapter(self, provider_cfg: ProviderConfig) -> Optional[BaseProvider]:
        """Create an SDK adapter based on provider config."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider_cfg.api_key}",
        }

        # Determine type: if anthropic_base_url is set and no openai_base_url,
        # it's anthropic. Otherwise default to openai.
        if provider_cfg.anthropic_base_url and not provider_cfg.openai_base_url:
            return AnthropicProvider(
                api_key=provider_cfg.api_key,
                base_url=provider_cfg.anthropic_base_url,
                extra_headers=headers,
            )

        if provider_cfg.openai_base_url:
            return OpenAIProvider(
                api_key=provider_cfg.api_key,
                base_url=provider_cfg.openai_base_url,
                extra_headers=headers,
            )

        # Both URLs set — create both, but we need the strategy config
        # to know which interface to use. Default to openai adapter.
        if provider_cfg.anthropic_base_url:
            # Store both; engine will pick based on model interface
            return OpenAIProvider(
                api_key=provider_cfg.api_key,
                base_url=provider_cfg.openai_base_url or provider_cfg.anthropic_base_url,
                extra_headers=headers,
            )

        return None

    @staticmethod
    def _create_state_backend(config: LLMStabilityConfig) -> StateBackend:
        backend_type = config.state_backend.type
        if backend_type == "redis":
            try:
                from .state.redis import RedisBackend
                redis_cfg = config.state_backend.redis
                return RedisBackend(
                    url=redis_cfg.url if redis_cfg else "redis://localhost:6379/0",
                    key_prefix=redis_cfg.key_prefix if redis_cfg else "llm_infra:",
                )
            except ImportError:
                logger.warning("redis package not installed, falling back to memory backend")
        return MemoryBackend()

    @staticmethod
    def _create_metrics(config: LLMStabilityConfig) -> MetricsCollector:
        if not config.metrics.enabled:
            return NoopCollector()
        if config.metrics.backend == "prometheus":
            try:
                from .metrics.prometheus import PrometheusCollector
                return PrometheusCollector(port=config.metrics.port)
            except ImportError:
                logger.warning("prometheus_client not installed, using simple metrics")
        return SimpleCollector()

    @staticmethod
    def _create_alert_dispatcher(config: LLMStabilityConfig) -> Optional[AlertDispatcher]:
        channels: List[BaseAlertChannel] = []
        for ch_cfg in config.alert.channels:
            if ch_cfg.type == "feishu" and ch_cfg.webhook_url:
                channels.append(FeishuAlertChannel(ch_cfg.webhook_url))
            elif ch_cfg.type == "webhook" and ch_cfg.url:
                channels.append(WebhookAlertChannel(ch_cfg.url))
            elif ch_cfg.type == "log":
                channels.append(LogAlertChannel())

        if not channels:
            channels.append(LogAlertChannel())

        rules = AlertRules(silence_seconds=config.alert.rules.silence_seconds)
        return AlertDispatcher(channels=channels, rules=rules)

    # ------------------------------------------------------------------
    # Internal: hot-reload
    # ------------------------------------------------------------------

    def _on_config_change(self, new_config: LLMStabilityConfig) -> None:
        """Handle configuration change."""
        old_config = self._config
        self._config = new_config

        # Rebuild adapters for changed providers
        for name, provider_cfg in new_config.providers.items():
            old_provider = old_config.providers.get(name)
            if not provider_cfg.enabled:
                self._adapters.pop(name, None)
                continue
            # Rebuild if new or changed
            if (
                old_provider is None
                or old_provider.api_key != provider_cfg.api_key
                or old_provider.openai_base_url != provider_cfg.openai_base_url
                or old_provider.anthropic_base_url != provider_cfg.anthropic_base_url
            ):
                adapter = self._create_adapter(provider_cfg)
                if adapter:
                    self._adapters[name] = adapter
                    logger.info(f"Provider adapter rebuilt: {name}")

        # Remove adapters for deleted providers
        for name in list(self._adapters.keys()):
            if name not in new_config.providers:
                del self._adapters[name]
                logger.info(f"Provider adapter removed: {name}")

        # Update engine references
        self._engine._config = new_config
        self._engine._adapters = self._adapters

        # Update health checker
        self._health_checker._config = new_config.health_check
        self._health_checker._providers = new_config.providers
        self._health_checker._adapters = self._adapters

        logger.info("Configuration hot-reloaded")


# ============================================================================
# Factory function
# ============================================================================

def create_client(
    source: Union[str, Path, Dict[str, Any], None] = None,
) -> LLMClient:
    """
    Create an LLMClient from a config source.

    Args:
        source: YAML file path, Python dict, or None (env var).

    Returns:
        Configured LLMClient with background tasks started.
    """
    config_path = str(source) if isinstance(source, (str, Path)) else None
    config = load_config(source)
    client = LLMClient(config=config, config_path=config_path)
    client.start()
    return client
