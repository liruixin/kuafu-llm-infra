"""
Stream monitor — real-time strategy evaluation for streaming requests.

Wraps a raw provider stream with strategy monitoring.  Extracted from
the engine to keep the engine as pure orchestration logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, List, Optional

from ..types import RequestContext, TokenUsage
from ..config.schema import StrategyConfig
from ..providers.base import BaseProvider, StreamChunk
from ..metrics.collector import MetricsCollector
from ..metrics import registry as m
from .strategies.base import BaseStrategy, StrategyEvent, StrategyAction
from .strategies.registry import create_strategies
from .recorder import RequestRecorder

logger = logging.getLogger("kuafu_llm_infra.stream_monitor")


class StrategyTriggered(Exception):
    """Raised when a strategy detects an anomaly and requests provider switch."""

    def __init__(self, event: StrategyEvent) -> None:
        self.event = event
        super().__init__(f"Strategy {event.strategy} triggered: {event.detail}")


class StreamMonitor:
    """Monitors a streaming response with real-time strategy evaluation."""

    def __init__(
        self,
        metrics: MetricsCollector,
        recorder: RequestRecorder,
    ) -> None:
        self._metrics = metrics
        self._recorder = recorder

    async def monitored_stream(
        self,
        adapter: BaseProvider,
        strategy_cfg: StrategyConfig,
        ctx: RequestContext,
    ) -> AsyncIterator[StreamChunk]:
        """
        Stream from adapter with real-time strategy monitoring.

        Yields StreamChunk objects. Raises StrategyTriggered on anomaly.
        On successful completion, records metrics via recorder.
        """
        strategies = create_strategies(
            strategy_cfg, ctx.provider_name, ctx.canonical_model,
        )

        start = time.monotonic()
        token_estimate = 0
        first_chunk = True
        ttft: Optional[float] = None
        content_buffer = ""
        final_usage: Optional[TokenUsage] = None

        try:
            stream = adapter.chat_stream(
                model=ctx.actual_model_id,
                messages=ctx.messages,
                max_tokens=ctx.max_tokens,
                temperature=ctx.temperature,
                timeout=strategy_cfg.timeout.ttft + 5,
                **ctx.extra_kwargs,
            )

            async for chunk in stream:
                elapsed = time.monotonic() - start
                content = chunk.content or ""
                if content:
                    token_estimate += max(len(content) // 4, 1)

                if chunk.usage:
                    final_usage = chunk.usage

                # Run all strategies
                for strategy in strategies:
                    event = strategy.on_chunk(
                        content=content,
                        is_first=first_chunk,
                        elapsed=elapsed,
                        total_tokens=token_estimate,
                    )
                    if event:
                        self._metrics.inc(
                            m.STRATEGY_TRIGGERED,
                            model=ctx.canonical_model,
                            provider=ctx.provider_name,
                            strategy=event.strategy,
                            **ctx.labels,
                        )
                        raise StrategyTriggered(event)

                if first_chunk and content:
                    ttft = elapsed
                    self._metrics.observe(
                        m.TTFT, ttft,
                        model=ctx.canonical_model,
                        provider=ctx.provider_name,
                        **ctx.labels,
                    )

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
                provider=ctx.provider_name,
                model=ctx.canonical_model,
                detail={"elapsed": elapsed},
            ))

        # Post-stream checks
        elapsed = time.monotonic() - start
        for strategy in strategies:
            event = strategy.on_complete(content_buffer, elapsed, token_estimate)
            if event:
                self._metrics.inc(
                    m.STRATEGY_TRIGGERED,
                    model=ctx.canonical_model,
                    provider=ctx.provider_name,
                    strategy=event.strategy,
                    **ctx.labels,
                )
                raise StrategyTriggered(event)

        # Compute final usage
        if final_usage:
            output_tokens = final_usage.completion_tokens
            input_tokens = final_usage.prompt_tokens
        else:
            output_tokens = token_estimate
            input_tokens = 0

        tps = output_tokens / elapsed if elapsed > 0 else 0

        await self._recorder.record_success(
            ctx,
            duration=elapsed,
            ttft=ttft,
            tps=tps,
            usage=final_usage or TokenUsage(
                completion_tokens=output_tokens,
                prompt_tokens=input_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
