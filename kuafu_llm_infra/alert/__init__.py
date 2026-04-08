"""
Alert - Alerting and notification module.

Dispatches alerts through pluggable channels with built-in
deduplication and rate limiting.
"""

from .channels import (
    AlertEvent,
    BaseAlertChannel,
    LogAlertChannel,
    FeishuAlertChannel,
    WebhookAlertChannel,
)
from .rules import AlertRules
from .dispatcher import AlertDispatcher

__all__ = [
    "AlertEvent",
    "BaseAlertChannel",
    "LogAlertChannel",
    "FeishuAlertChannel",
    "WebhookAlertChannel",
    "AlertRules",
    "AlertDispatcher",
]
