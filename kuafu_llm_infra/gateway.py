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
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from .config.schema import LLMStabilityConfig, ProviderConfig
from .config.loader import load_config
from .providers.base import BaseProvider, ChatResponse, StreamChunk, ToolCall
from .providers.registry import create_provider
from .state.backend import StateBackend
from .state.memory import MemoryBackend
from .metrics.collector import MetricsCollector, NoopCollector
from .metrics.simple import SimpleCollector
from .alert.channels.base import BaseAlertChannel
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
    def __init__(self, content: str, finish_reason: str = "stop",
                 tool_calls: Optional[List[ToolCall]] = None) -> None:
        self.message = _Message(content, tool_calls=tool_calls)
        self.finish_reason = finish_reason
        self.index = 0


class _Message:
    def __init__(self, content: str,
                 tool_calls: Optional[List[ToolCall]] = None) -> None:
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls


class _StreamChunkWrapper:
    """Wraps StreamChunk to be more OpenAI-like."""
    def __init__(self, chunk: StreamChunk) -> None:
        self.content = chunk.content
        self.finish_reason = chunk.finish_reason
        self.tool_calls = chunk.tool_calls
        self.raw = chunk.raw


class _CompletionResponse:
    """OpenAI-compatible completion response."""
    def __init__(self, chat_response: ChatResponse) -> None:
        self.content = chat_response.content
        self.model = chat_response.model
        self.tool_calls = chat_response.tool_calls
        self.choices = [_Choice(
            chat_response.content,
            chat_response.finish_reason,
            tool_calls=chat_response.tool_calls,
        )]
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
        labels: Optional[Dict[str, str]] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs: Any,
    ) -> Union[_CompletionResponse, _StreamWrapper]:
        """Create a chat completion.

        Args:
            tools: Tool definitions for function calling. Each item
                follows the **OpenAI tool format**::

                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get current weather",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "city": {"type": "string"}
                                },
                                "required": ["city"]
                            }
                        }
                    }

            tool_choice: Controls how the model selects tools.
                ``"auto"`` | ``"none"``.        """
        if stream:
            aiter = self._engine.execute_chat_stream(
                business_key=business_key,
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
                labels=labels,
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
                tools=tools,
                tool_choice=tool_choice,
                labels=labels,
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

    Hot-reload: call ``update_config()`` to apply new config.
    In multi-instance mode (Redis backend), the update is
    automatically broadcast to all instances via pub/sub.
    """

    def __init__(self, config: LLMStabilityConfig) -> None:
        self._config = config

        # State backend
        self._state = self._create_state_backend(config)

        # Metrics
        self._metrics = self._create_metrics(config)

        # Alert dispatcher
        self._alert_dispatcher = self._create_alert_dispatcher(config)

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
            alert_dispatcher=self._alert_dispatcher,
        )

        # Health checker
        self._health_checker = HealthChecker(
            config=config,
            adapters=self._adapters,
            state=self._state,
            metrics=self._metrics,
            alert_dispatcher=self._alert_dispatcher,
        )

        # OpenAI-compatible interface
        self.chat = _Chat(self._engine)

        self._started = False

    def start(self) -> None:
        """Start background tasks (health checker, config subscriber, alert dispatcher)."""
        if self._started:
            return
        self._health_checker.start()
        if self._alert_dispatcher:
            self._alert_dispatcher.start()
        asyncio.create_task(self._start_config_subscriber())
        self._started = True
        logger.info("LLMClient started")

    async def shutdown(self) -> None:
        """Gracefully stop all background tasks."""
        self._health_checker.stop()
        await self._state.unsubscribe_config()
        if self._alert_dispatcher:
            await self._alert_dispatcher.stop()
        self._started = False
        logger.info("LLMClient shutdown")

    async def update_config(
        self,
        new_config: Union[Dict[str, Any], LLMStabilityConfig],
        *,
        broadcast: bool = True,
    ) -> None:
        """
        Apply new configuration (hot-reload).

        Args:
            new_config: New config as dict or LLMStabilityConfig.
            broadcast: If True and Redis backend is active,
                       publish to other instances via pub/sub.
        """
        if isinstance(new_config, dict):
            new_config = LLMStabilityConfig(**new_config)

        self._apply_config(new_config)

        if broadcast:
            config_json = new_config.model_dump_json()
            await self._state.publish_config(config_json)

    # ------------------------------------------------------------------
    # Internal: config subscriber
    # ------------------------------------------------------------------

    async def _start_config_subscriber(self) -> None:
        """Subscribe to config updates from other instances."""
        def _on_remote_config(config_json: str):
            try:
                data = json.loads(config_json)
                new_config = LLMStabilityConfig(**data)
                self._apply_config(new_config)
                logger.info("Remote config update applied")
            except Exception as e:
                logger.error(f"Failed to apply remote config: {e}", exc_info=True)

        await self._state.subscribe_config(_on_remote_config)

    # ------------------------------------------------------------------
    # Internal: apply config changes
    # ------------------------------------------------------------------

    def _apply_config(self, new_config: LLMStabilityConfig) -> None:
        """Apply config diff — rebuild only what changed."""
        old_config = self._config
        self._config = new_config

        # Rebuild adapters for changed providers
        for name, provider_cfg in new_config.providers.items():
            old_provider = old_config.providers.get(name)
            if not provider_cfg.enabled:
                self._adapters.pop(name, None)
                continue
            if (
                old_provider is None
                or old_provider.api_key != provider_cfg.api_key
                or old_provider.base_url != provider_cfg.base_url
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

        # Update sub-components via public APIs (no private attribute access)
        self._engine.update_config(new_config, self._adapters)
        self._health_checker.update_config(new_config, self._adapters)

        logger.info("Configuration applied")

    # ------------------------------------------------------------------
    # Internal: setup helpers
    # ------------------------------------------------------------------

    def _build_adapters(self, config: LLMStabilityConfig) -> None:
        for name, provider_cfg in config.providers.items():
            if not provider_cfg.enabled:
                continue
            adapter = self._create_adapter(provider_cfg)
            if adapter:
                self._adapters[name] = adapter

    @staticmethod
    def _create_adapter(provider_cfg: ProviderConfig) -> Optional[BaseProvider]:
        if not provider_cfg.base_url:
            return None

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider_cfg.api_key}",
        }

        return create_provider(
            provider_cfg.type,
            api_key=provider_cfg.api_key,
            base_url=provider_cfg.base_url,
            extra_headers=headers,
        )

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
                return PrometheusCollector(
                    label_keys=config.metrics.label_keys,
                    port=config.metrics.port,
                )
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
    config = load_config(source)
    client = LLMClient(config=config)
    client.start()
    return client
