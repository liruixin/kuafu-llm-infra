"""
Prometheus metrics backend.

Instruments are auto-registered from ``ALL_METRICS`` — adding a new
``MetricDef`` in the registry requires zero changes here.

REQUEST-scope metrics carry ``["model", "provider"] + extra_labels``.
SYSTEM-scope metrics carry only their ``extra_labels``.

Note: Custom ``label_keys`` (e.g. user_id, module) are **no longer** added
to Prometheus metric dimensions to prevent high-cardinality explosion.
Labels are still passed through to alert channels for display.

本库不启动任何 HTTP 端口，业务侧通过 ``LLMClient.get_metrics()`` 获取
Prometheus 文本数据，挂到自己的 HTTP 路由暴露给采集服务。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .collector import MetricsCollector
from .registry import (
    ALL_METRICS,
    LabelScope,
    MetricDef,
    MetricKind,
)

logger = logging.getLogger("kuafu_llm_infra.metrics.prometheus")

try:
    import prometheus_client as prom
    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover
    _HAS_PROMETHEUS = False


class PrometheusCollector(MetricsCollector):
    """Metrics collector backed by prometheus_client.

    不启动 HTTP server，不支持多进程。业务侧调用 :meth:`get_metrics`
    自行挂到 HTTP 路由上。
    """

    def __init__(
        self,
        label_keys: Optional[List[str]] = None,
    ) -> None:
        if not _HAS_PROMETHEUS:
            raise ImportError(
                "prometheus_client is required for PrometheusCollector. "
                "Install it with: pip install prometheus-client"
            )

        self._label_keys = list(label_keys or [])
        self._instruments: Dict[str, Any] = {}

        # Auto-register all declared metrics
        kind_to_class = {
            MetricKind.COUNTER: prom.Counter,
            MetricKind.HISTOGRAM: prom.Histogram,
            MetricKind.GAUGE: prom.Gauge,
        }

        for defn in ALL_METRICS:
            label_names = self._compute_labels(defn)
            cls = kind_to_class[defn.kind]
            self._instruments[defn.name] = cls(
                defn.name, defn.description, label_names,
            )

    def get_metrics(self) -> bytes:
        """返回 Prometheus 文本格式指标数据，供业务侧 HTTP 接口调用。"""
        return prom.generate_latest()

    # ------------------------------------------------------------------
    # Label computation
    # ------------------------------------------------------------------

    def _compute_labels(self, defn: MetricDef) -> list[str]:
        """Compute the full label list for a metric definition.

        Note: label_keys are intentionally excluded to avoid
        high-cardinality explosion in Prometheus.
        """
        if defn.scope == LabelScope.REQUEST:
            return ["model", "provider"] + list(defn.extra_labels)
        else:
            return list(defn.extra_labels)

    def _resolve_labels(self, defn: MetricDef, labels: Dict[str, str]) -> Dict[str, str]:
        """Build the full label dict, filling missing custom keys with ''."""
        if defn.scope == LabelScope.REQUEST:
            result: Dict[str, str] = {}
            for key in ["model", "provider"] + list(defn.extra_labels):
                result[key] = labels.get(key, "")
            return result
        else:
            return {key: labels.get(key, "") for key in defn.extra_labels}

    # ------------------------------------------------------------------
    # Generic recording methods
    # ------------------------------------------------------------------

    def inc(self, metric: MetricDef, value: float = 1.0, **labels: str) -> None:
        instrument = self._instruments.get(metric.name)
        if instrument is None:
            return
        resolved = self._resolve_labels(metric, labels)
        instrument.labels(**resolved).inc(value)

    def observe(self, metric: MetricDef, value: float, **labels: str) -> None:
        instrument = self._instruments.get(metric.name)
        if instrument is None:
            return
        resolved = self._resolve_labels(metric, labels)
        instrument.labels(**resolved).observe(value)

    def set(self, metric: MetricDef, value: float, **labels: str) -> None:
        instrument = self._instruments.get(metric.name)
        if instrument is None:
            return
        resolved = self._resolve_labels(metric, labels)
        instrument.labels(**resolved).set(value)
