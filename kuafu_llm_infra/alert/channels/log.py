"""
Log-based alert channel (default fallback).
"""

from __future__ import annotations

import logging

from .base import AlertEvent, BaseAlertChannel

logger = logging.getLogger("kuafu_llm_infra.alert")

_LEVEL_MAP = {
    "info": logging.INFO,
    "warning": logging.WARNING,
    "critical": logging.CRITICAL,
}


class LogAlertChannel(BaseAlertChannel):
    """Sends alerts to the Python logging system."""

    async def send(self, event: AlertEvent) -> None:
        level = _LEVEL_MAP.get(event.level, logging.WARNING)
        logger.log(
            level,
            "[%s] %s | %s (provider=%s, model=%s)",
            event.level.upper(),
            event.title,
            event.message,
            event.provider,
            event.model,
        )
