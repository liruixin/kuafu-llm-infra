"""
Metrics registry — single source of truth for all metric definitions.

Adding a new metric to the library:
    1. Declare a ``MetricDef`` constant in this file
    2. Add it to ``ALL_METRICS``
    3. Call ``metrics.inc()`` / ``metrics.observe()`` / ``metrics.set()``
       at the recording site

That's it. No changes needed in collector, simple, or prometheus modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MetricKind(Enum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"


class LabelScope(Enum):
    REQUEST = "request"  # model + provider + custom label_keys + extra_labels
    SYSTEM = "system"    # fixed labels only (no custom label_keys)


@dataclass(frozen=True, slots=True)
class MetricDef:
    """
    Declarative metric definition.

    ``extra_labels`` are metric-specific labels beyond the scope defaults.
    For REQUEST scope, base labels are (model, provider, *label_keys).
    For SYSTEM scope, labels come solely from ``extra_labels``.
    """
    name: str
    kind: MetricKind
    description: str
    scope: LabelScope = LabelScope.REQUEST
    extra_labels: tuple[str, ...] = ()


# ============================================================================
# Request-level metrics
# ============================================================================

REQUEST_TOTAL = MetricDef(
    "llm_request_total",
    MetricKind.COUNTER,
    "Total LLM requests",
    extra_labels=("status",),
)

REQUEST_DURATION = MetricDef(
    "llm_request_duration_seconds",
    MetricKind.HISTOGRAM,
    "LLM request duration in seconds",
)

TTFT = MetricDef(
    "llm_ttft_seconds",
    MetricKind.HISTOGRAM,
    "Time to first token in seconds",
)

TOKENS_PER_SECOND = MetricDef(
    "llm_tokens_per_second",
    MetricKind.HISTOGRAM,
    "Token throughput per second",
)

INPUT_TOKENS = MetricDef(
    "llm_input_tokens_total",
    MetricKind.COUNTER,
    "Total input (prompt) tokens",
)

OUTPUT_TOKENS = MetricDef(
    "llm_output_tokens_total",
    MetricKind.COUNTER,
    "Total output (completion) tokens",
)

CACHED_TOKENS = MetricDef(
    "llm_cached_tokens_total",
    MetricKind.COUNTER,
    "Total cached input tokens (prompt cache hits)",
)

STRATEGY_TRIGGERED = MetricDef(
    "llm_strategy_triggered_total",
    MetricKind.COUNTER,
    "Total strategy triggers",
    extra_labels=("strategy",),
)

# ============================================================================
# System-level metrics (probes, health — no custom label_keys)
# ============================================================================

PROVIDER_HEALTH = MetricDef(
    "llm_provider_health",
    MetricKind.GAUGE,
    "Provider health status (1=healthy, 0=unhealthy)",
    scope=LabelScope.SYSTEM,
    extra_labels=("provider",),
)

PROVIDER_SCORE = MetricDef(
    "llm_provider_score",
    MetricKind.GAUGE,
    "Provider composite score",
    scope=LabelScope.SYSTEM,
    extra_labels=("model", "provider"),
)

SCORE_COMPONENT = MetricDef(
    "llm_score_component",
    MetricKind.GAUGE,
    "Provider score breakdown by component",
    scope=LabelScope.SYSTEM,
    extra_labels=("model", "provider", "component"),
)

PROBE_TTFT = MetricDef(
    "llm_probe_ttft_seconds",
    MetricKind.HISTOGRAM,
    "Probe time to first token in seconds",
    scope=LabelScope.SYSTEM,
    extra_labels=("provider",),
)

PROBE_TOTAL = MetricDef(
    "llm_probe_total",
    MetricKind.COUNTER,
    "Total probe requests",
    scope=LabelScope.SYSTEM,
    extra_labels=("provider", "status"),
)

# ============================================================================
# Master list — backends iterate this to auto-register instruments
# ============================================================================

ALL_METRICS: tuple[MetricDef, ...] = (
    REQUEST_TOTAL,
    REQUEST_DURATION,
    TTFT,
    TOKENS_PER_SECOND,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    CACHED_TOKENS,
    STRATEGY_TRIGGERED,
    PROVIDER_HEALTH,
    PROVIDER_SCORE,
    SCORE_COMPONENT,
    PROBE_TTFT,
    PROBE_TOTAL,
)
