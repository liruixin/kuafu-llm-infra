"""
Alert dispatcher.

Routes alert events to configured channels, applying
deduplication and silence rules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from .channels.base import AlertEvent, BaseAlertChannel
from .rules import AlertRules

logger = logging.getLogger("kuafu_llm_infra.alert.dispatcher")


class AlertDispatcher:
    """Async dispatcher that routes alerts through channels via a background task."""

    def __init__(
        self,
        channels: List[BaseAlertChannel],
        rules: AlertRules,
    ) -> None:
        self._channels = channels
        self._rules = rules
        self._queue: asyncio.Queue[AlertEvent] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def dispatch(self, event: AlertEvent) -> None:
        """Enqueue an alert event (non-blocking)."""
        self._queue.put_nowait(event)

    def start(self) -> None:
        """Start the background consumer task."""
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        """Cancel the background consumer task."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        """Consume events from the queue and dispatch to channels."""
        while True:
            event = await self._queue.get()
            if not self._rules.should_send(event):
                continue
            for channel in self._channels:
                try:
                    await channel.send(event)
                except Exception:
                    logger.exception("Failed to send alert via %s", type(channel).__name__)
