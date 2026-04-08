"""
Fallback decision engine.

Orchestrates provider selection, real-time degradation detection,
and fallback chain traversal for both stream and block modes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from ..config.schema import (
    ProviderConfig,
    StrategyConfig,
    StrategyMode,
    LLMStabilityConfig,
)
from ..providers.base import BaseProvider, ChatResponse, StreamChunk
from ..state.backend import StateBackend
from ..metrics.collector import MetricsCollector, NoopCollector
from ..alert.channels.base import AlertEvent
from .scorer import Scorer, ScoredProvider
from .strategies import (
    BaseStrategy,
    StrategyEvent,
    StrategyAction,
    TtftTimeoutStrategy,
    EmptyFrameStrategy,
    ChunkGapStrategy,
    SlowSpeedStrategy,
    TotalTimeoutStrategy,
)

logger = logging.getLogger("kuafu_llm_infra.engine")


class StrategyTriggered(Exception):
    """Raised when a strategy detects an anomaly and requests provider switch."""

    def __init__(self, event: StrategyEvent) -> None:
        self.event = event
        super().__init__(f"Strategy {event.strategy} triggered: {event.detail}")


class AllProvidersExhausted(Exception):
    """Raised when all providers (including fallbacks) have failed."""
    pass


class FallbackEngine:
    """Core fallback engine that orchestrates provider selection and retry."""

    def __init__(
        self,
        config: LLMStabilityConfig,
        adapters: Dict[str, BaseProvider],
        scorer: Scorer,
        state: StateBackend,
        metrics: Optional[MetricsCollector] = None,
        alert_queue: Optional[asyncio.Queue] = None,
    ) -> None:
        self._config = config
        self._adapters = adapters
        self._scorer = scorer
        self._state = state
        self._metrics = metrics or NoopCollector()
        self._alert_queue = alert_queue

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_chat(
        self,
        business_key: str,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Execute a non-streaming chat request with fallback."""
        strategy_cfg = self._resolve_strategy(business_key, model)
        chains = self._build_chains(strategy_cfg)

        last_error: Optional[Exception] = None

        for model_name, providers in chains:
            ranked = await self._scorer.rank_providers(model_name, providers)

            for sp in ranked:
                adapter = self._get_adapter(sp.provider)
                if not adapter:
                    continue

                mapped_model = self._map_model(sp.provider, model_name)
                timeout = strategy_cfg.timeout.total
                start = time.monotonic()

                try:
                    response = await asyncio.wait_for(
                        adapter.chat(
                            model=mapped_model,
                            messages=messages,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            timeout=timeout,
                            **kwargs,
                        ),
                        timeout=timeout,
                    )

                    duration = time.monotonic() - start

                    # Check empty response
                    if not response.content.strip():
                        raise StrategyTriggered(StrategyEvent(
                            strategy="empty_response",
                            action=StrategyAction.SWITCH,
                            provider=sp.provider.name,
                            model=model_name,
                            detail={"elapsed": duration},
                        ))

                    # Record success
                    await self._scorer.record_success(
                        model_name, sp.provider.name, duration=duration,
                    )
                    self._metrics.inc_request_total(business_key, model_name, sp.provider.name, "success")
                    self._metrics.observe_request_duration(business_key, model_name, sp.provider.name, duration)

                    return response

                except asyncio.TimeoutError:
                    duration = time.monotonic() - start
                    logger.warning(f"[engine] {sp.provider.name} timeout after {duration:.1f}s for {model_name}")
                    await self._handle_failure(
                        business_key, model_name, sp.provider.name,
                        "total_timeout", f"timeout after {duration:.1f}s",
                    )
                    last_error = TimeoutError(f"Provider {sp.provider.name} timed out")
                    continue

                except StrategyTriggered as e:
                    logger.warning(f"[engine] {sp.provider.name} strategy triggered: {e.event.strategy}")
                    await self._handle_failure(
                        business_key, model_name, sp.provider.name,
                        e.event.strategy, str(e.event.detail),
                    )
                    last_error = e
                    continue

                except Exception as e:
                    duration = time.monotonic() - start
                    logger.error(f"[engine] {sp.provider.name} error: {e}")
                    await self._handle_failure(
                        business_key, model_name, sp.provider.name,
                        "error", str(e),
                    )
                    last_error = e
                    continue

        self._send_alert(
            "critical", "all_providers_exhausted",
            f"All providers exhausted for business_key={business_key}",
        )
        raise AllProvidersExhausted(
            f"No provider available for {business_key}. Last error: {last_error}"
        )

    async def execute_chat_stream(
        self,
        business_key: str,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Execute a streaming chat request with fallback."""
        strategy_cfg = self._resolve_strategy(business_key, model)
        chains = self._build_chains(strategy_cfg)

        last_error: Optional[Exception] = None

        for model_name, providers in chains:
            ranked = await self._scorer.rank_providers(model_name, providers)

            for sp in ranked:
                adapter = self._get_adapter(sp.provider)
                if not adapter:
                    continue

                mapped_model = self._map_model(sp.provider, model_name)

                try:
                    chunk_count = 0
                    async for chunk in self._stream_with_strategies(
                        adapter=adapter,
                        strategy_cfg=strategy_cfg,
                        provider=sp.provider,
                        model_name=model_name,
                        mapped_model=mapped_model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        business_key=business_key,
                        **kwargs,
                    ):
                        chunk_count += 1
                        yield chunk

                    if chunk_count > 0:
                        return  # Successfully streamed

                except StrategyTriggered as e:
                    if e.event.action == StrategyAction.SWITCH:
                        logger.warning(
                            f"[engine] {sp.provider.name} stream switch: {e.event.strategy}"
                        )
                        await self._handle_failure(
                            business_key, model_name, sp.provider.name,
                            e.event.strategy, str(e.event.detail),
                        )
                        last_error = e
                        continue
                    else:
                        # RECORD — cannot switch, just log
                        logger.warning(
                            f"[engine] {sp.provider.name} stream record: {e.event.strategy}"
                        )
                        await self._scorer.record_failure(
                            model_name, sp.provider.name, e.event.strategy,
                        )
                        return  # Content already sent, cannot retry

                except Exception as e:
                    logger.error(f"[engine] {sp.provider.name} stream error: {e}")
                    await self._handle_failure(
                        business_key, model_name, sp.provider.name,
                        "error", str(e),
                    )
                    last_error = e
                    continue

        self._send_alert(
            "critical", "all_providers_exhausted",
            f"All providers exhausted for business_key={business_key}",
        )
        raise AllProvidersExhausted(
            f"No provider available for {business_key}. Last error: {last_error}"
        )

    # ------------------------------------------------------------------
    # Internal: streaming with strategy monitoring
    # ------------------------------------------------------------------

    async def _stream_with_strategies(
        self,
        adapter: BaseProvider,
        strategy_cfg: StrategyConfig,
        provider: ProviderConfig,
        model_name: str,
        mapped_model: str,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        temperature: Optional[float],
        business_key: str,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Stream from adapter with real-time strategy monitoring."""
        # Create per-request strategy instances
        strategies = self._create_stream_strategies(strategy_cfg, provider.name, model_name)

        start = time.monotonic()
        total_tokens = 0
        first_chunk = True
        ttft: Optional[float] = None
        content_buffer = ""

        try:
            stream = adapter.chat_stream(
                model=mapped_model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=strategy_cfg.timeout.ttft + 5,  # Give a bit more than TTFT threshold
                **kwargs,
            )

            async for chunk in stream:
                elapsed = time.monotonic() - start
                content = chunk.content or ""
                if content:
                    total_tokens += max(len(content) // 4, 1)  # Rough token estimate

                # Run all strategies
                for strategy in strategies:
                    event = strategy.on_chunk(
                        content=content,
                        is_first=first_chunk,
                        elapsed=elapsed,
                        total_tokens=total_tokens,
                    )
                    if event:
                        self._metrics.inc_strategy_triggered(
                            business_key, model_name, provider.name, event.strategy,
                        )
                        raise StrategyTriggered(event)

                if first_chunk and content:
                    ttft = elapsed
                    self._metrics.observe_ttft(business_key, model_name, provider.name, ttft)

                first_chunk = False
                content_buffer += content
                yield chunk

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            for strategy in strategies:
                event = strategy.on_timeout(elapsed)
                if event:
                    raise StrategyTriggered(event)
            raise StrategyTriggered(StrategyEvent(
                strategy="ttft_timeout",
                action=StrategyAction.SWITCH,
                provider=provider.name,
                model=model_name,
                detail={"elapsed": elapsed},
            ))

        # Post-stream checks
        elapsed = time.monotonic() - start
        for strategy in strategies:
            event = strategy.on_complete(content_buffer, elapsed, total_tokens)
            if event:
                self._metrics.inc_strategy_triggered(
                    business_key, model_name, provider.name, event.strategy,
                )
                raise StrategyTriggered(event)

        # Record success metrics
        tps = total_tokens / elapsed if elapsed > 0 else 0
        await self._scorer.record_success(
            model_name, provider.name, ttft=ttft,
            tokens_per_second=tps, duration=elapsed,
        )
        self._metrics.inc_request_total(business_key, model_name, provider.name, "success")
        self._metrics.observe_request_duration(business_key, model_name, provider.name, elapsed)
        if tps > 0:
            self._metrics.observe_tokens_per_second(business_key, model_name, provider.name, tps)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_strategy(
        self,
        business_key: str,
        model: Optional[str],
    ) -> StrategyConfig:
        """Resolve business_key to a StrategyConfig."""
        if business_key in self._config.strategies:
            return self._config.strategies[business_key]

        # Fallback: if user passed a model directly, build an ad-hoc strategy
        if model:
            return StrategyConfig(
                mode=StrategyMode.BLOCK,
                primary=self._find_model_route(model),
            )

        raise ValueError(f"Unknown business_key '{business_key}' and no model specified")

    def _find_model_route(self, model: str):
        """Build a ModelRouteConfig for a model from provider configs."""
        from ..config.schema import ModelRouteConfig
        # Find all providers that have this model enabled
        providers = [
            name for name, p in self._config.providers.items()
            if p.enabled and (model in p.model_mapping or p.ping_model)
        ]
        if not providers:
            providers = [name for name, p in self._config.providers.items() if p.enabled]
        return ModelRouteConfig(model=model, providers=providers)

    def _build_chains(
        self,
        strategy_cfg: StrategyConfig,
    ) -> List[tuple]:
        """Build ordered list of (model_name, [ProviderConfig]) chains."""
        chains = []

        # Primary
        model_name = strategy_cfg.primary.model
        providers = self._resolve_providers(strategy_cfg.primary.providers)
        chains.append((model_name, providers))

        # Fallbacks
        for fb in strategy_cfg.fallback:
            fb_providers = self._resolve_providers(fb.providers)
            chains.append((fb.model, fb_providers))

        return chains

    def _resolve_providers(self, provider_names: List[str]) -> List[ProviderConfig]:
        """Resolve provider names to ProviderConfig objects."""
        result = []
        for name in provider_names:
            if name in self._config.providers:
                p = self._config.providers[name]
                if p.enabled:
                    result.append(p)
        return result

    def _get_adapter(self, provider: ProviderConfig) -> Optional[BaseProvider]:
        """Get the SDK adapter for a provider."""
        return self._adapters.get(provider.name)

    def _map_model(self, provider: ProviderConfig, model_name: str) -> str:
        """Apply provider model mapping."""
        return provider.model_mapping.get(model_name, model_name)

    def _create_stream_strategies(
        self,
        cfg: StrategyConfig,
        provider: str,
        model: str,
    ) -> List[BaseStrategy]:
        """Create per-request strategy instances for stream mode."""
        return [
            TtftTimeoutStrategy(cfg.timeout.ttft, provider, model),
            EmptyFrameStrategy(cfg.empty_frame_threshold, provider, model),
            ChunkGapStrategy(cfg.timeout.chunk_gap, provider, model),
            SlowSpeedStrategy(cfg.slow_speed_threshold, provider, model),
        ]

    async def _handle_failure(
        self,
        business_key: str,
        model: str,
        provider: str,
        reason: str,
        detail: str,
    ) -> None:
        """Record failure and emit metrics/alerts."""
        await self._scorer.record_failure(model, provider, reason)
        self._metrics.inc_request_total(business_key, model, provider, "error")
        self._metrics.inc_strategy_triggered(business_key, model, provider, reason)
        self._send_alert(
            "warning", f"provider_degraded:{reason}",
            f"Provider {provider} degraded for model {model}: {detail}",
            provider=provider, model=model,
        )

    def _send_alert(
        self,
        level: str,
        title: str,
        message: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Enqueue an alert event (non-blocking)."""
        if self._alert_queue:
            self._alert_queue.put_nowait(AlertEvent(
                level=level,
                title=title,
                message=message,
                provider=provider,
                model=model,
            ))
