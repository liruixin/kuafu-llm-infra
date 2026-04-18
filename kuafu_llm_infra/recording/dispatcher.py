"""
Record dispatcher — async queue + batch worker for request records.

Mirrors AlertDispatcher's lifecycle pattern (start/stop/queue),
but adds batch aggregation and backpressure (bounded queue, drop on full).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from ..types import RequestRecord
from .base import BaseRecordSink

logger = logging.getLogger("kuafu_llm_infra.recording.dispatcher")


class RecordDispatcher:
    """Async dispatcher: Queue → batch worker → sinks."""

    def __init__(
        self,
        sinks: List[BaseRecordSink],
        *,
        batch_size: int = 500,
        flush_interval: float = 2.0,
        queue_size: int = 50_000,
    ) -> None:
        self._sinks = sinks
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue[RequestRecord] = asyncio.Queue(maxsize=queue_size)
        self._task: Optional[asyncio.Task] = None
        self._collected = 0
        self._dropped = 0

    def collect(self, record: RequestRecord) -> None:
        """主链路调用，put_nowait 非阻塞，队列满则 drop。"""
        try:
            self._queue.put_nowait(record)
            self._collected += 1
            if self._collected <= 5 or self._collected % 100 == 0:
                logger.info(
                    "Record collected (total=%d, queue=%d)",
                    self._collected, self._queue.qsize(),
                )
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning("Record queue full, dropped=%d", self._dropped)

    def start(self) -> None:
        """Start the background batch worker."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._flush_worker())
        logger.info(
            "RecordDispatcher started (batch=%d flush=%.1fs queue_max=%d sinks=%d)",
            self._batch_size, self._flush_interval,
            self._queue.maxsize, len(self._sinks),
        )

    async def stop(self) -> None:
        """优雅关闭：排空队列 → flush 最后一批 → 关闭 sink。"""
        # 排空队列
        remaining: List[RequestRecord] = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if remaining:
            await self._write_to_sinks(remaining)

        # 取消后台任务
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # 关闭所有 sink
        for sink in self._sinks:
            try:
                await sink.close()
            except Exception:
                logger.exception("Failed to close sink %s", type(sink).__name__)

    async def _flush_worker(self) -> None:
        """后台常驻，攒 batch 写入 sink。"""
        buffer: List[RequestRecord] = []
        last_flush = time.monotonic()

        while True:
            try:
                timeout = self._flush_interval - (time.monotonic() - last_flush)
                record = await asyncio.wait_for(
                    self._queue.get(), timeout=max(timeout, 0.01),
                )
                buffer.append(record)
            except asyncio.TimeoutError:
                pass

            should_flush = (
                len(buffer) >= self._batch_size
                or (buffer and time.monotonic() - last_flush >= self._flush_interval)
            )

            if should_flush and buffer:
                await self._write_to_sinks(buffer)
                buffer = []
                last_flush = time.monotonic()

    async def _write_to_sinks(self, batch: List[RequestRecord]) -> None:
        """将一批记录写入所有 sink，单个 sink 失败不影响其他。"""
        for sink in self._sinks:
            try:
                await sink.write_batch(batch)
            except Exception:
                logger.exception(
                    "Failed to write %d records via %s",
                    len(batch), type(sink).__name__,
                )
