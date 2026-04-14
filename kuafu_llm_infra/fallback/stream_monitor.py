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
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[StreamChunk]:

        """
        Stream from adapter with real-time strategy monitoring.

        Yields StreamChunk objects. Raises StrategyTriggered on anomaly.
        On successful completion, records metrics via recorder.

        Args:
            timeout: SDK 连接超时，由 engine 根据 deadline 剩余预算传入。
                     若为 None 则回退到 ttft + 5。
        """
        strategies = create_strategies(
            strategy_cfg, ctx.provider_name, ctx.canonical_model,
        )

        sdk_timeout = timeout if timeout is not None else strategy_cfg.timeout.ttft + 5

        # 日志前缀：[business_key] [label1=v1, label2=v2]
        label_tag = ""
        if ctx.labels:
            parts = ", ".join(f"{k}={v}" for k, v in ctx.labels.items())
            label_tag = f" [{parts}]"
        log_prefix = f"[{ctx.business_key}] {ctx.provider_name}:{ctx.actual_model_id}{label_tag}"

        start = time.monotonic()
        token_estimate = 0
        first_chunk = True
        ttft: Optional[float] = None
        content_buffer = ""
        final_usage: Optional[TokenUsage] = None
        model_wait_time = 0.0   # 累计等待 SDK 返回 chunk 的纯模型生成时间

        try:
            stream = adapter.chat_stream(
                model=ctx.actual_model_id,
                messages=ctx.messages,
                max_tokens=ctx.max_tokens,
                temperature=ctx.temperature,
                timeout=sdk_timeout,
                tools=ctx.tools,
                tool_choice=ctx.tool_choice,
                **ctx.extra_kwargs,
            )

            wait_start = time.monotonic()
            stream_iter = stream.__aiter__()
            # 逐帧超时：首帧用 TTFT 超时，后续帧用 chunk_gap 超时
            chunk_gap_timeout = strategy_cfg.timeout.chunk_gap
            while True:
                # 首帧用 TTFT 阈值的剩余预算（sdk_timeout 含 +5 缓冲，
                # 那是给 SDK 连接建立的，首帧超时应严格按 TTFT 配置）
                if first_chunk:
                    ttft_budget = strategy_cfg.timeout.ttft - (time.monotonic() - start)
                    iter_timeout = max(ttft_budget, 0.1)
                else:
                    iter_timeout = chunk_gap_timeout

                try:
                    chunk = await asyncio.wait_for(
                        stream_iter.__anext__(),
                        timeout=iter_timeout,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - start
                    if first_chunk:
                        # 首帧超时，可以切换
                        logger.warning(
                            f"{log_prefix} 首帧等待超时 "
                            f"elapsed={elapsed:.3f}s timeout={iter_timeout:.3f}s"
                        )
                        raise StrategyTriggered(StrategyEvent(
                            strategy="ttft_timeout",
                            action=StrategyAction.SWITCH,
                            provider=ctx.provider_name,
                            model=ctx.canonical_model,
                            detail={
                                "elapsed": elapsed,
                                "trigger": "iter_timeout",
                            },
                        ))
                    else:
                        # 帧间超时：有内容只能 RECORD，无内容可 SWITCH
                        action = StrategyAction.RECORD if content_buffer else StrategyAction.SWITCH
                        logger.warning(
                            f"{log_prefix} 帧间等待超时 "
                            f"elapsed={elapsed:.3f}s gap_timeout={iter_timeout:.3f}s "
                            f"has_content={bool(content_buffer)} action={action.value}"
                        )
                        raise StrategyTriggered(StrategyEvent(
                            strategy="chunk_gap",
                            action=action,
                            provider=ctx.provider_name,
                            model=ctx.canonical_model,
                            detail={
                                "gap_seconds": iter_timeout,
                                "threshold": chunk_gap_timeout,
                                "trigger": "iter_timeout",
                            },
                        ))

                # chunk 刚从 SDK 到达，记录精确到达时间
                chunk_arrived_at = time.monotonic()
                chunk_gap = chunk_arrived_at - wait_start
                model_wait_time += chunk_gap

                elapsed = time.monotonic() - start
                content = chunk.content or ""

                # logger.info(
                #     f"{log_prefix} "
                #     f"{'首帧到达' if first_chunk else '帧间隔'} "
                #     f"{chunk_gap:.3f}s "
                #     f"elapsed={elapsed:.3f}s content_len={len(content)} "
                #     f"thinking={chunk.thinking}"
                # )

                # 非思考帧才累计 token
                if content and not chunk.thinking:
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
                        chunk_arrived_at=chunk_arrived_at,
                        is_thinking=chunk.thinking,
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

                if first_chunk and content and not chunk.thinking:
                    ttft = elapsed

                first_chunk = False

                # 思考帧不 yield 给调用方，不累计内容
                if chunk.thinking:
                    wait_start = time.monotonic()
                    continue

                content_buffer += content
                yield chunk
                # yield 返回后（调用方处理完毕），开始计时等待下一个 chunk
                wait_start = time.monotonic()

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

        # tps: 纯模型生成速度 = output_tokens / 等待SDK的总时间（排除 TTFT + 程序处理时间）
        generation_time = (model_wait_time - ttft) if ttft is not None else model_wait_time
        tps = output_tokens / generation_time if generation_time > 0 else 0

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
