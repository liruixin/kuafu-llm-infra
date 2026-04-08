"""
Request recorder — centralizes metrics recording and alert dispatch.

Both the fallback engine and health checker delegate to this module
instead of operating on metrics and alert channels directly.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..types import RequestContext, TokenUsage
from ..metrics.collector import MetricsCollector, NoopCollector
from ..metrics import registry as m
from ..alert.dispatcher import AlertDispatcher
from ..alert.channels.base import AlertEvent
from .scorer import Scorer

logger = logging.getLogger("kuafu_llm_infra.recorder")


class RequestRecorder:
    """Records request outcomes to metrics, scorer state, and alerts."""

    def __init__(
        self,
        scorer: Scorer,
        metrics: MetricsCollector,
        alert_dispatcher: Optional[AlertDispatcher] = None,
    ) -> None:
        self._scorer = scorer
        self._metrics = metrics
        self._alert = alert_dispatcher

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
            **ctx.labels,
        )
        self._metrics.observe(
            m.REQUEST_DURATION, duration,
            model=model, provider=provider,
            **ctx.labels,
        )

        if ttft is not None:
            self._metrics.observe(
                m.TTFT, ttft,
                model=model, provider=provider,
                **ctx.labels,
            )
        if tps is not None and tps > 0:
            self._metrics.observe(
                m.TOKENS_PER_SECOND, tps,
                model=model, provider=provider,
                **ctx.labels,
            )

        # Token usage
        if usage:
            if usage.prompt_tokens > 0:
                self._metrics.inc(
                    m.INPUT_TOKENS, usage.prompt_tokens,
                    model=model, provider=provider,
                    **ctx.labels,
                )
            if usage.completion_tokens > 0:
                self._metrics.inc(
                    m.OUTPUT_TOKENS, usage.completion_tokens,
                    model=model, provider=provider,
                    **ctx.labels,
                )

    # ------------------------------------------------------------------
    # Failure recording
    # ------------------------------------------------------------------

    async def record_failure(
        self,
        ctx: RequestContext,
        reason: str,
        detail: str,
    ) -> None:
        """Record a failed request to scorer + metrics + alert."""
        model = ctx.canonical_model
        provider = ctx.provider_name

        await self._scorer.record_failure(model, provider, reason)

        self._metrics.inc(
            m.REQUEST_TOTAL,
            model=model, provider=provider, status="error",
            **ctx.labels,
        )
        self._metrics.inc(
            m.STRATEGY_TRIGGERED,
            model=model, provider=provider, strategy=reason,
            **ctx.labels,
        )

        self.send_alert(
            "warning",
            f"provider_degraded:{reason}",
            f"Provider {provider} degraded for model {model}: {detail}",
            provider=provider,
            model=model,
        )

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
    ) -> None:
        """Send an alert event through the dispatcher."""
        if self._alert:
            self._alert.dispatch(AlertEvent(
                level=level,
                title=title,
                message=message,
                provider=provider,
                model=model,
            ))
