"""
Generic webhook alert channel.
"""

from __future__ import annotations

from dataclasses import asdict

import httpx

from .base import AlertEvent, BaseAlertChannel


class WebhookAlertChannel(BaseAlertChannel):
    """Sends alert events as JSON POST to a generic webhook URL."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def send(self, event: AlertEvent) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(self._url, json=asdict(event))
