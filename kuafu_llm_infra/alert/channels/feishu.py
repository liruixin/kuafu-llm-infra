"""
Feishu (Lark) webhook alert channel.
"""

from __future__ import annotations

import datetime

import httpx

from .base import AlertEvent, BaseAlertChannel

# 级别对应的标识符
_LEVEL_ICON = {
    "info": "ℹ️",
    "warning": "⚠️",
    "critical": "🔴",
}


class FeishuAlertChannel(BaseAlertChannel):
    """Sends alerts to a Feishu webhook as text messages."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send(self, event: AlertEvent) -> None:
        icon = _LEVEL_ICON.get(event.level, "⚠️")
        ts = datetime.datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            f"{icon} [{event.level.upper()}] {event.title}",
            f"",
            f"{event.message}",
            f"",
        ]
        # 按实际有值的字段输出详情
        if event.business_key:
            lines.append(f"业务标识: {event.business_key}")
        if event.model:
            lines.append(f"模型: {event.model}")
        if event.provider:
            lines.append(f"提供商: {event.provider}")
        if event.labels:
            label_str = ", ".join(f"{k}={v}" for k, v in event.labels.items())
            lines.append(f"标签: {label_str}")
        lines.append(f"时间: {ts}")

        text = "\n".join(lines)
        payload = {"msg_type": "text", "content": {"text": text}}
        async with httpx.AsyncClient() as client:
            await client.post(self._webhook_url, json=payload)
