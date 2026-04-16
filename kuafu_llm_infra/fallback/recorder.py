"""
Request recorder — centralizes metrics recording and alert dispatch.

Both the fallback engine and health checker delegate to this module
instead of operating on metrics and alert channels directly.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from ..types import RequestContext, RequestRecord, TokenUsage
from ..metrics.collector import MetricsCollector, NoopCollector
from ..metrics import registry as m
from ..alert.dispatcher import AlertDispatcher
from ..alert.channels.base import AlertEvent
from ..recording.dispatcher import RecordDispatcher
from .scorer import Scorer

logger = logging.getLogger("kuafu_llm_infra.recorder")


class RequestRecorder:
    """Records request outcomes to metrics, scorer state, and alerts."""

    def __init__(
        self,
        scorer: Scorer,
        metrics: MetricsCollector,
        alert_dispatcher: Optional[AlertDispatcher] = None,
        record_dispatcher: Optional[RecordDispatcher] = None,
    ) -> None:
        self._scorer = scorer
        self._metrics = metrics
        self._alert = alert_dispatcher
        self._recording = record_dispatcher

    # ------------------------------------------------------------------
    # Success recording
    # ------------------------------------------------------------------

    async def record_success(
        self,
        ctx: RequestContext,
        *,
        duration: float,
        ttft: Optional[float] = None,
        tps: Optional[float] = None,
        usage: Optional[TokenUsage] = None,
    ) -> None:
        """Record a successful request to scorer + metrics."""
        model = ctx.canonical_model
        provider = ctx.provider_name

        # Scorer state
        await self._scorer.record_success(
            model, provider,
            ttft=ttft, tokens_per_second=tps, duration=duration,
        )

        # Request metrics
        self._metrics.inc(
            m.REQUEST_TOTAL,
            model=model, provider=provider, status="success",
        )
        self._metrics.observe(
            m.REQUEST_DURATION, duration,
            model=model, provider=provider,
        )

        if ttft is not None:
            self._metrics.observe(
                m.TTFT, ttft,
                model=model, provider=provider,
            )
        if tps is not None and tps > 0:
            self._metrics.observe(
                m.TOKENS_PER_SECOND, tps,
                model=model, provider=provider,
            )

        # Token usage
        if usage:
            if usage.prompt_tokens > 0:
                self._metrics.inc(
                    m.INPUT_TOKENS, usage.prompt_tokens,
                    model=model, provider=provider,
                )
            if usage.completion_tokens > 0:
                self._metrics.inc(
                    m.OUTPUT_TOKENS, usage.completion_tokens,
                    model=model, provider=provider,
                )
            if usage.cached_tokens > 0:
                self._metrics.inc(
                    m.CACHED_TOKENS, usage.cached_tokens,
                    model=model, provider=provider,
                )

        # 高维度数据收集（含 labels）
        if self._recording:
            self._recording.collect(RequestRecord(
                business_key=ctx.business_key,
                canonical_model=model,
                provider_name=provider,
                labels=ctx.labels,
                status="success",
                duration_ms=int(duration * 1000),
                ttft_ms=int(ttft * 1000) if ttft else 0,
                tps=tps or 0.0,
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                cached_tokens=usage.cached_tokens if usage else 0,
            ))

    # ------------------------------------------------------------------
    # Failure recording
    # ------------------------------------------------------------------

    async def record_failure(
        self,
        ctx: RequestContext,
        reason: str,
        detail: str,
    ) -> None:
        """Record a failed request to scorer + metrics."""
        model = ctx.canonical_model
        provider = ctx.provider_name

        await self._scorer.record_failure(model, provider, reason)

        self._metrics.inc(
            m.REQUEST_TOTAL,
            model=model, provider=provider, status="error",
        )
        self._metrics.inc(
            m.STRATEGY_TRIGGERED,
            model=model, provider=provider, strategy=reason,
        )

        # 高维度数据收集（含 labels）
        if self._recording:
            self._recording.collect(RequestRecord(
                business_key=ctx.business_key,
                canonical_model=model,
                provider_name=provider,
                labels=ctx.labels,
                status="error",
                duration_ms=0,
                error_reason=reason,
                error_detail=detail,
            ))

    # ------------------------------------------------------------------
    # Alert dispatch
    # ------------------------------------------------------------------

    def send_alert(
        self,
        level: str,
        title: str,
        message: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        business_key: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Send an alert event through the dispatcher."""
        if self._alert:
            self._alert.dispatch(AlertEvent(
                level=level,
                title=title,
                message=message,
                provider=provider,
                model=model,
                business_key=business_key,
                labels=labels or {},
            ))
