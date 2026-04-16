"""
Prometheus metrics backend.

Instruments are auto-registered from ``ALL_METRICS`` — adding a new
``MetricDef`` in the registry requires zero changes here.

REQUEST-scope metrics carry ``["model", "provider"] + extra_labels``.
SYSTEM-scope metrics carry only their ``extra_labels``.

Note: Custom ``label_keys`` (e.g. user_id, module) are **no longer** added
to Prometheus metric dimensions to prevent high-cardinality explosion.
Labels are still passed through to alert channels for display.

**Multi-process support (Uvicorn workers):**

Set the environment variable ``PROMETHEUS_MULTIPROC_DIR`` to a writable
directory *before* importing this module.  Each worker writes metrics to
that directory; the HTTP server aggregates them on scrape.

    export PROMETHEUS_MULTIPROC_DIR=/tmp/prom_multiproc
    mkdir -p $PROMETHEUS_MULTIPROC_DIR
"""

from __future__ import annotations

import logging
import os
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
    from prometheus_client import multiprocess, CollectorRegistry
    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover
    _HAS_PROMETHEUS = False

_MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR")

# Module-level ref to keep the file-lock FD alive for the process lifetime.
_server_lock_fd: Any = None


class PrometheusCollector(MetricsCollector):
    """Metrics collector backed by prometheus_client.

    When ``PROMETHEUS_MULTIPROC_DIR`` is set, automatically uses the
    prometheus_client multi-process mode so that multiple Uvicorn workers
    can safely share a single metrics HTTP endpoint.
    """

    def __init__(
        self,
        label_keys: Optional[List[str]] = None,
        port: Optional[int] = None,
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
            kwargs: Dict[str, Any] = {}
            # In multi-process mode, Gauge needs an explicit aggregation
            # strategy.  "liveall" keeps per-pid values and lets the
            # MultiProcessCollector expose them all (the scraping server
            # picks one representative value per label-set).
            if defn.kind == MetricKind.GAUGE and _MULTIPROC_DIR:
                kwargs["multiprocess_mode"] = "liveall"
            self._instruments[defn.name] = cls(
                defn.name, defn.description, label_names, **kwargs,
            )

        if port is not None:
            self._try_start_http_server(port)

    @staticmethod
    def _try_start_http_server(port: int) -> None:
        """Start Prometheus HTTP server, handling multi-process safely.

        In multi-process mode (``PROMETHEUS_MULTIPROC_DIR`` set), uses a
        file lock so that only the first worker to acquire the lock starts
        the server.  Other workers skip server startup but still record
        metrics to the shared directory — the running server aggregates
        them on scrape.
        """
        global _server_lock_fd

        if _MULTIPROC_DIR:
            import fcntl
            lock_path = os.path.join(_MULTIPROC_DIR, ".server.lock")
            try:
                _server_lock_fd = open(lock_path, "w")
                fcntl.flock(_server_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Got the lock — this worker starts the server.
                registry = CollectorRegistry()
                multiprocess.MultiProcessCollector(registry)
                prom.start_http_server(port, registry=registry)
                logger.info(
                    "Prometheus HTTP server started in multi-process mode "
                    f"(port={port}, dir={_MULTIPROC_DIR})"
                )
            except OSError:
                # Another worker already holds the lock — skip.
                _server_lock_fd = None
                logger.info(
                    "Prometheus HTTP server already running in another worker, "
                    "skipping (metrics still recorded to shared dir)"
                )
        else:
            prom.start_http_server(port)

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
