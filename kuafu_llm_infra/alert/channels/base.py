"""
Base class for alert channels.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Optional
import time


@dataclass
class AlertEvent:
    """A single alert event."""
    level: str  # "info" | "warning" | "critical"
    title: str
    message: str
    provider: Optional[str] = None
    model: Optional[str] = None
    business_key: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class BaseAlertChannel(ABC):
    """Abstract base for alert channels."""

    @abstractmethod
    async def send(self, event: AlertEvent) -> None:
        """Send an alert event through this channel."""
        ...
