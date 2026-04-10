"""
Fallback decision engine.

Pure orchestration: resolve strategy -> build model chain ->
rank providers -> try each -> delegate to StreamMonitor / RequestRecorder.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from ..types import RequestContext, TokenUsage
from ..config.schema import (
    StrategyConfig,
    LLMStabilityConfig,
)
from ..providers.base import BaseProvider, ChatResponse, StreamChunk
from ..state.backend import StateBackend
from ..metrics.collector import MetricsCollector, NoopCollector
from ..metrics import registry as m
from ..alert.dispatcher import AlertDispatcher
from .scorer import Scorer
from .recorder import RequestRecorder
from .stream_monitor import StreamMonitor, StrategyTriggered
from .strategies.base import StrategyEvent, StrategyAction

logger = logging.getLogger("kuafu_llm_infra.engine")


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
        alert_dispatcher: Optional[AlertDispatcher] = None,
    ) -> None:
        self._config = config
        self._adapters = adapters
        self._scorer = scorer
        self._state = state
        self._metrics = metrics or NoopCollector()
        self._recorder = RequestRecorder(scorer, self._metrics, alert_dispatcher)
        self._stream_monitor = StreamMonitor(self._metrics, self._recorder)

    def update_config(
        self,
        config: LLMStabilityConfig,
        adapters: Dict[str, BaseProvider],
    ) -> None:
        """Apply new configuration (called by gateway on hot-reload)."""
        self._config = config
        self._adapters = adapters

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
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Execute a non-streaming chat request with fallback.

        Args:
            business_key: Strategy key defined in config.
            messages: Conversation messages in OpenAI format.
            model: Override model (bypasses strategy resolution).
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
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

                The library uses OpenAI format as the unified standard.
                Provider adapters convert to their native format
                automatically (e.g. Anthropic ``input_schema``).
            tool_choice: Controls how the model selects tools.
                - ``"auto"`` – model decides (default when tools present)
                - ``"none"`` – model must not call tools
            labels: Custom label key-value pairs for metrics.
            **kwargs: Additional provider-specific parameters.
        """
        ctx = RequestContext(
            business_key=business_key,
            messages=messages,
            labels=labels or {},
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            extra_kwargs=kwargs,
        )
        strategy_cfg = self._resolve_strategy(ctx)
        chain = self._build_model_chain(strategy_cfg)

        last_error: Optional[Exception] = None

        for canonical_model in chain:
            entries = self._config.get_model_providers(canonical_model)
            ranked = await self._scorer.rank_providers(canonical_model, entries)

            for sp in ranked:
                adapter = self._adapters.get(sp.provider_name)
                if not adapter:
                    continue

                ctx.canonical_model = canonical_model
                ctx.provider_name = sp.provider_name
                ctx.actual_model_id = self._config.resolve_model_id(
                    canonical_model, sp.provider_name,
                )

                timeout = strategy_cfg.timeout.total
                start = time.monotonic()

                try:
                    response = await asyncio.wait_for(
                        adapter.chat(
                            model=ctx.actual_model_id,
                            messages=messages,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            timeout=timeout,
                            tools=tools,
                            tool_choice=tool_choice,
                            **kwargs,
                        ),
                        timeout=timeout,
                    )

                    duration = time.monotonic() - start

                    if not response.content.strip() and not response.tool_calls:
                        raise StrategyTriggered(StrategyEvent(
                            strategy="empty_response",
                            action=StrategyAction.SWITCH,
                            provider=sp.provider_name,
                            model=canonical_model,
                            detail={"elapsed": duration},
                        ))

                    await self._recorder.record_success(
                        ctx,
                        duration=duration,
                        usage=response.usage,
                    )
                    return response

                except asyncio.TimeoutError:
                    duration = time.monotonic() - start
                    logger.warning(
                        f"[engine] {sp.provider_name} timeout after {duration:.1f}s "
                        f"for {canonical_model}"
                    )
                    await self._recorder.record_failure(
                        ctx, "total_timeout", f"timeout after {duration:.1f}s",
                    )
                    last_error = TimeoutError(f"Provider {sp.provider_name} timed out")

                except StrategyTriggered as e:
                    logger.warning(
                        f"[engine] {sp.provider_name} strategy triggered: "
                        f"{e.event.strategy}"
                    )
                    await self._recorder.record_failure(
                        ctx, e.event.strategy, str(e.event.detail),
                    )
                    last_error = e

                except Exception as e:
                    logger.error(f"[engine] {sp.provider_name} error: {e}")
                    await self._recorder.record_failure(ctx, "error", str(e))
                    last_error = e

        self._recorder.send_alert(
            "critical", "all_providers_exhausted",
            f"All providers exhausted for business_key={ctx.business_key}",
        )
        raise AllProvidersExhausted(
            f"No provider available for {ctx.business_key}. Last error: {last_error}"
        )

    async def execute_chat_stream(
        self,
        business_key: str,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Execute a streaming chat request with fallback.

        Args:
            tools: Same format as :meth:`execute_chat`. See that method
                for the full tool definition schema.
            tool_choice: Same format as :meth:`execute_chat`.
        """
        ctx = RequestContext(
            business_key=business_key,
            messages=messages,
            labels=labels or {},
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            extra_kwargs=kwargs,
        )
        # 拿到具体要执行的策略组
        strategy_cfg = self._resolve_strategy(ctx)
        chain = self._build_model_chain(strategy_cfg)

        last_error: Optional[Exception] = None

        for canonical_model in chain:
            entries = self._config.get_model_providers(canonical_model)
            ranked = await self._scorer.rank_providers(canonical_model, entries)

            for sp in ranked:
                adapter = self._adapters.get(sp.provider_name)
                if not adapter:
                    continue

                ctx.canonical_model = canonical_model
                ctx.provider_name = sp.provider_name
                ctx.actual_model_id = self._config.resolve_model_id(
                    canonical_model, sp.provider_name,
                )

                try:
                    chunk_count = 0
                    async for chunk in self._stream_monitor.monitored_stream(
                        adapter, strategy_cfg, ctx,
                    ):
                        chunk_count += 1
                        yield chunk

                    if chunk_count > 0:
                        return

                except StrategyTriggered as e:
                    if e.event.action == StrategyAction.SWITCH:
                        logger.warning(
                            f"[engine] {sp.provider_name} stream switch: "
                            f"{e.event.strategy}"
                        )
                        await self._recorder.record_failure(
                            ctx, e.event.strategy, str(e.event.detail),
                        )
                        last_error = e
                    else:
                        logger.warning(
                            f"[engine] {sp.provider_name} stream record: "
                            f"{e.event.strategy}"
                        )
                        await self._scorer.record_failure(
                            canonical_model, sp.provider_name, e.event.strategy,
                        )
                        return

                except Exception as e:
                    logger.error(f"[engine] {sp.provider_name} stream error: {e}")
                    await self._recorder.record_failure(ctx, "error", str(e))
                    last_error = e

        self._recorder.send_alert(
            "critical", "all_providers_exhausted",
            f"All providers exhausted for business_key={ctx.business_key}",
        )
        raise AllProvidersExhausted(
            f"No provider available for {ctx.business_key}. Last error: {last_error}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_strategy(self, ctx: RequestContext) -> StrategyConfig:
        if ctx.business_key in self._config.strategies:
            return self._config.strategies[ctx.business_key]

        if ctx.model:
            return StrategyConfig(
                primary=ctx.model,
            )

        raise ValueError(
            f"Unknown business_key '{ctx.business_key}' and no model specified"
        )

    @staticmethod
    def _build_model_chain(strategy_cfg: StrategyConfig) -> List[str]:
        chain = [strategy_cfg.primary]
        chain.extend(strategy_cfg.fallback)
        return chain
