"""
ClickHouse record sink — batch insert via clickhouse-connect.

Requires ``clickhouse-connect`` (optional dependency)::

    pip install clickhouse-connect
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from ..types import RequestRecord
from .base import BaseRecordSink

logger = logging.getLogger("kuafu_llm_infra.recording.clickhouse")


class ClickHouseRecordSink(BaseRecordSink):
    """批量写入 ClickHouse，run_in_executor 避免阻塞事件循环。"""

    def __init__(
        self,
        host: str,
        database: str = "default",
        table: str = "llm_request_metrics",
        label_columns: Optional[List[str]] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        secure: bool = False,
    ) -> None:
        import clickhouse_connect

        kwargs: dict = dict(host=host, database=database)
        if port is not None:
            kwargs["port"] = port
        if username:
            kwargs["username"] = username
        if password:
            kwargs["password"] = password
        if secure:
            kwargs["secure"] = secure
        self._client = clickhouse_connect.get_client(**kwargs)
        self._table = table
        self._label_columns = list(label_columns or [])

        # 启动时同步探测连接，错误立即暴露而不是首次写入时才发现
        try:
            self._client.query("SELECT 1")
        except Exception as exc:
            raise RuntimeError(
                f"ClickHouse ping failed (host={host} db={database}): {exc}"
            ) from exc

        # 预计算列名列表（固定列 + 动态 label 列），避免每次 write_batch 重复拼接
        self._column_names = (
            ["event_time"]
            + self._label_columns
            + [
                "provider", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "total_latency_ms", "ttft_ms",
                "status", "error_code",
            ]
        )
        logger.info(
            "ClickHouse sink ready: host=%s db=%s table=%s columns=%s",
            host, database, table, self._column_names,
        )

    async def write_batch(self, records: List[RequestRecord]) -> None:
        rows = []
        for r in records:
            row = [datetime.fromtimestamp(r.timestamp)]
            # 动态 label 列
            for key in self._label_columns:
                row.append(r.labels.get(key, ""))
            # 固定指标列
            row.extend([
                r.provider_name,
                r.canonical_model,
                r.input_tokens,
                r.output_tokens,
                r.cached_tokens,
                r.duration_ms,
                r.ttft_ms,
                r.status,
                r.error_reason,
            ])
            rows.append(row)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._client.insert(
                self._table, rows, column_names=self._column_names,
            ),
        )
        logger.info("Flushed %d records to ClickHouse", len(records))

    async def close(self) -> None:
        self._client.close()
