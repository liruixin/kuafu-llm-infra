"""
Alert rules: deduplication, silence windows, rate limiting.
"""

from __future__ import annotations

import time
from typing import Dict, Tuple

from .channels.base import AlertEvent


class AlertRules:
    """Implements silence/dedup logic for alert events."""

    def __init__(self, silence_seconds: float = 60.0) -> None:
        self._silence_seconds = silence_seconds
        self._last_sent: Dict[Tuple[str, str | None], float] = {}

    def should_send(self, event: AlertEvent) -> bool:
        """Return False if same (title, provider) was sent within silence window."""
        key = (event.title, event.provider)
        now = time.time()
        last = self._last_sent.get(key)
        if last is not None and (now - last) < self._silence_seconds:
            return False
        self._last_sent[key] = now
        return True
