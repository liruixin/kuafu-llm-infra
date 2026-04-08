"""
Prometheus metrics backend.

Exposes LLM infrastructure metrics in Prometheus format.
"""

from __future__ import annotations

from typing import Optional

from .collector import MetricsCollector

try:
    import prometheus_client as prom

    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover
    _HAS_PROMETHEUS = False


class PrometheusCollector(MetricsCollector):
    """Metrics collector backed by prometheus_client."""

    def __init__(self, port: Optional[int] = None) -> None:
        if not _HAS_PROMETHEUS:
            raise ImportError(
                "prometheus_client is required for PrometheusCollector. "
                "Install it with: pip install prometheus-client"
            )

        self._request_total = prom.Counter(
            "llm_request_total",
            "Total LLM requests",
            ["business_key", "model", "provider", "status"],
        )
        self._request_duration = prom.Histogram(
            "llm_request_duration_seconds",
            "LLM request duration in seconds",
            ["business_key", "model", "provider"],
        )
        self._ttft = prom.Histogram(
            "llm_ttft_seconds",
            "Time to first token in seconds",
            ["business_key", "model", "provider"],
        )
        self._tokens_per_second = prom.Histogram(
            "llm_tokens_per_second",
            "Token throughput per second",
            ["business_key", "model", "provider"],
        )
        self._fallback_total = prom.Counter(
            "llm_fallback_total",
            "Total fallback events",
            ["business_key", "model", "from_provider", "to_provider", "reason"],
        )
        self._strategy_triggered = prom.Counter(
            "llm_strategy_triggered_total",
            "Total strategy triggers",
            ["business_key", "model", "provider", "strategy"],
        )
        self._provider_health = prom.Gauge(
            "llm_provider_health",
            "Provider health status (1=healthy, 0=unhealthy)",
            ["provider"],
        )
        self._provider_score = prom.Gauge(
            "llm_provider_score",
            "Provider composite score",
            ["model", "provider"],
        )
        self._probe_ttft = prom.Histogram(
            "llm_probe_ttft_seconds",
            "Probe time to first token in seconds",
            ["provider"],
        )
        self._probe_total = prom.Counter(
            "llm_probe_total",
            "Total probe requests",
            ["provider", "status"],
        )

        if port is not None:
            prom.start_http_server(port)

    # --- Request-level metrics ---

    def inc_request_total(
        self, business_key: str, model: str, provider: str, status: str,
    ) -> None:
        self._request_total.labels(
            business_key=business_key, model=model, provider=provider, status=status,
        ).inc()

    def observe_request_duration(
        self, business_key: str, model: str, provider: str, duration: float,
    ) -> None:
        self._request_duration.labels(
            business_key=business_key, model=model, provider=provider,
        ).observe(duration)

    def observe_ttft(
        self, business_key: str, model: str, provider: str, ttft: float,
    ) -> None:
        self._ttft.labels(
            business_key=business_key, model=model, provider=provider,
        ).observe(ttft)

    def observe_tokens_per_second(
        self, business_key: str, model: str, provider: str, tps: float,
    ) -> None:
        self._tokens_per_second.labels(
            business_key=business_key, model=model, provider=provider,
        ).observe(tps)

    # --- Fallback events ---

    def inc_fallback_total(
        self, business_key: str, model: str, from_provider: str,
        to_provider: str, reason: str,
    ) -> None:
        self._fallback_total.labels(
            business_key=business_key, model=model,
            from_provider=from_provider, to_provider=to_provider, reason=reason,
        ).inc()

    def inc_strategy_triggered(
        self, business_key: str, model: str, provider: str, strategy: str,
    ) -> None:
        self._strategy_triggered.labels(
            business_key=business_key, model=model,
            provider=provider, strategy=strategy,
        ).inc()

    # --- Provider-level metrics ---

    def set_provider_health(self, provider: str, healthy: bool) -> None:
        self._provider_health.labels(provider=provider).set(1.0 if healthy else 0.0)

    def set_provider_score(self, model: str, provider: str, score: float) -> None:
        self._provider_score.labels(model=model, provider=provider).set(score)

    def observe_probe_ttft(self, provider: str, ttft: float) -> None:
        self._probe_ttft.labels(provider=provider).observe(ttft)

    def inc_probe_total(self, provider: str, status: str) -> None:
        self._probe_total.labels(provider=provider, status=status).inc()
