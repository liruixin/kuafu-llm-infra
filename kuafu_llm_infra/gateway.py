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

from .config.schema import LLMStabilityConfig, ProviderConfig, adapter_key
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

    def __init__(self, engine: FallbackEngine, client: LLMClient) -> None:
        self._engine = engine
        self._client = client

    async def create(
        self,
        *,
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
        """Create a chat completion."""
        if not self._client._config_loaded:
            raise RuntimeError("Config not loaded from Redis yet")
        if stream:
            aiter = self._engine.execute_chat_stream(
                business_key=business_key,
                messages=messages,
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

    def __init__(self, engine: FallbackEngine, client: LLMClient) -> None:
        self.completions = _Completions(engine, client)


# ============================================================================
# LLMClient - the main gateway
# ============================================================================

class LLMClient:
    """
    OpenAI-compatible LLM client with built-in fallback,
    monitoring, and alerting.

    Hot-reload: call ``push_config()`` to write new config to Redis,
    all instances will pick it up on the next pull cycle.
    """

    def __init__(
        self,
        config: LLMStabilityConfig,
        *,
        state: Optional[StateBackend] = None,
        config_loaded: bool = True,
    ) -> None:
        self._config = config
        self._config_loaded = config_loaded
        self._last_config_raw: Optional[str] = None

        # State backend（Redis 模式下由外部传入）
        self._state = state or self._create_state_backend(config)

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
        self.chat = _Chat(self._engine, self)

        self._started = False
        self._pull_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start background tasks (health checker, config pull, alert dispatcher)."""
        if self._started:
            return
        self._health_checker.start()
        if self._alert_dispatcher:
            self._alert_dispatcher.start()
        self._pull_task = asyncio.create_task(self._config_pull_loop())
        self._started = True
        logger.info("LLMClient started")

    async def shutdown(self) -> None:
        """Gracefully stop all background tasks."""
        self._health_checker.stop()
        if self._pull_task and not self._pull_task.done():
            self._pull_task.cancel()
        if self._alert_dispatcher:
            await self._alert_dispatcher.stop()
        self._started = False
        logger.info("LLMClient shutdown")

    async def push_config(
        self,
        new_config: Union[Dict[str, Any], LLMStabilityConfig],
    ) -> None:
        """
        写入新配置到 Redis 并立即在本实例生效。

        其他实例会在下一次 pull 周期自动拿到新配置。
        """
        if isinstance(new_config, dict):
            new_config = LLMStabilityConfig(**new_config)

        new_config.validate_references()

        config_json = new_config.model_dump_json()
        await self._state.save_config(config_json)
        self._last_config_raw = config_json
        self._apply_config(new_config)
        logger.info("Config pushed and applied")

    # ------------------------------------------------------------------
    # Internal: config pull loop
    # ------------------------------------------------------------------

    async def _config_pull_loop(self, interval: float = 10.0) -> None:
        """Periodically pull config from Redis and apply if changed."""
        while True:
            try:
                raw = await self._state.load_config()
                if raw is not None and raw != self._last_config_raw:
                    data = json.loads(raw)
                    new_config = LLMStabilityConfig(**data)
                    new_config.validate_references()
                    self._apply_config(new_config)
                    self._last_config_raw = raw
                    if not self._config_loaded:
                        self._config_loaded = True
                        logger.info("Config loaded from Redis (first load)")
                    else:
                        logger.info("Config updated from Redis")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Config pull failed: {e}")

            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Internal: apply config changes
    # ------------------------------------------------------------------

    def _apply_config(self, new_config: LLMStabilityConfig) -> None:
        """Apply config diff — rebuild only what changed."""
        old_config = self._config
        self._config = new_config

        # Build set of expected adapter keys from new config
        expected_keys = set()
        for name, provider_cfg in new_config.providers.items():
            if not provider_cfg.enabled:
                # Remove all adapters for disabled providers
                for ep_type in provider_cfg.endpoints:
                    self._adapters.pop(adapter_key(name, ep_type), None)
                continue

            for ep_type, ep_cfg in provider_cfg.endpoints.items():
                key = adapter_key(name, ep_type)
                expected_keys.add(key)

                # Check if this endpoint changed
                old_provider = old_config.providers.get(name)
                old_ep = (
                    old_provider.endpoints.get(ep_type) if old_provider else None
                )
                if (
                    old_ep is None
                    or old_provider.api_key != provider_cfg.api_key
                    or old_ep.base_url != ep_cfg.base_url
                ):
                    adpt = self._create_adapter(
                        provider_type=ep_type,
                        api_key=provider_cfg.api_key,
                        base_url=ep_cfg.base_url,
                    )
                    if adpt:
                        self._adapters[key] = adpt
                        logger.info(f"Provider adapter rebuilt: {key}")

        # Remove adapters no longer in config
        for key in list(self._adapters.keys()):
            if key not in expected_keys:
                del self._adapters[key]
                logger.info(f"Provider adapter removed: {key}")

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
            for ep_type, ep_cfg in provider_cfg.endpoints.items():
                key = adapter_key(name, ep_type)
                adpt = self._create_adapter(
                    provider_type=ep_type,
                    api_key=provider_cfg.api_key,
                    base_url=ep_cfg.base_url,
                )
                if adpt:
                    self._adapters[key] = adpt

    @staticmethod
    def _create_adapter(
        provider_type: str,
        api_key: str,
        base_url: str,
    ) -> Optional[BaseProvider]:
        if not base_url:
            return None

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        return create_provider(
            provider_type,
            api_key=api_key,
            base_url=base_url,
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
    *,
    redis_url: Optional[str] = None,
    redis_ssl: bool = False,
    key_prefix: str = "kuafu_llm_infra:",
) -> LLMClient:
    """
    Create an LLMClient.

    两种模式：

    1. 本地配置文件::

        client = create_client("llm_stability.yaml")

    2. 从 Redis 拉取配置（非阻塞，后台加载）::

        client = create_client(redis_url="redis://localhost:6379/0", redis_ssl=True)

    Redis 模式下 create_client 立即返回，配置在后台 pull loop 中加载。
    配置加载完成前发起请求会抛出 RuntimeError。
    """
    if redis_url:
        from .state.redis import RedisBackend
        state = RedisBackend(url=redis_url, key_prefix=key_prefix, ssl=redis_ssl)
        config = LLMStabilityConfig()
        client = LLMClient(config=config, state=state, config_loaded=False)
    else:
        config = load_config(source)
        client = LLMClient(config=config)

    client.start()
    return client
