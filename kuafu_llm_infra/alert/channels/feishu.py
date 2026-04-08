"""
Feishu (Lark) webhook alert channel.
"""

from __future__ import annotations

import httpx

from .base import AlertEvent, BaseAlertChannel


class FeishuAlertChannel(BaseAlertChannel):
    """Sends alerts to a Feishu webhook as text messages."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send(self, event: AlertEvent) -> None:
        text = (
            f"[{event.level.upper()}] {event.title}\n"
            f"{event.message}\n"
            f"provider: {event.provider or '-'} | model: {event.model or '-'}"
        )
        payload = {"msg_type": "text", "content": {"text": text}}
        async with httpx.AsyncClient() as client:
            await client.post(self._webhook_url, json=payload)
