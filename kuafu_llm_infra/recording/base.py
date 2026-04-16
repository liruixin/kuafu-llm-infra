"""
Abstract base for request record sinks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from ..types import RequestRecord


class BaseRecordSink(ABC):
    """Abstract base for request record sinks."""

    @abstractmethod
    async def write_batch(self, records: List[RequestRecord]) -> None:
        """Write a batch of request records to this sink."""
        ...

    async def close(self) -> None:
        """Optional cleanup on shutdown."""
        pass
