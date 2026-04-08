"""
Configuration hot-reload watcher.

Supports:
- File mtime polling (default)
- SIGHUP signal reload
- Programmatic update via callback
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path
from typing import Callable, Optional

from .loader import load_config
from .schema import LLMStabilityConfig

logger = logging.getLogger("kuafu_llm_infra.config.watcher")


class ConfigWatcher:
    """Watches config file for changes and triggers reload callbacks."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        poll_interval: float = 10.0,
        on_change: Optional[Callable[[LLMStabilityConfig], None]] = None,
    ) -> None:
        self._config_path = Path(config_path) if config_path else None
        self._poll_interval = poll_interval
        self._on_change = on_change
        self._last_mtime: float = 0.0
        self._task: Optional[asyncio.Task] = None
        self._sighup_registered = False

    def start(self) -> None:
        """Start file polling and register SIGHUP handler."""
        if self._config_path:
            if self._task is None or self._task.done():
                self._last_mtime = self._get_mtime()
                self._task = asyncio.create_task(self._poll_loop())
                logger.info(
                    f"Config watcher started: {self._config_path} "
                    f"(poll every {self._poll_interval}s)"
                )

        self._register_sighup()

    def stop(self) -> None:
        """Stop the polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Config watcher stopped")

    async def _poll_loop(self) -> None:
        """Periodically check file mtime and reload if changed."""
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                current_mtime = self._get_mtime()
                if current_mtime > self._last_mtime:
                    logger.info("Config file changed, reloading...")
                    self._last_mtime = current_mtime
                    self._reload()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Config reload error: {e}", exc_info=True)

    def _reload(self) -> None:
        """Reload config from file and invoke callback."""
        if not self._config_path:
            return
        try:
            new_config = load_config(self._config_path)
            if self._on_change:
                self._on_change(new_config)
            logger.info("Config reloaded successfully")
        except Exception as e:
            logger.error(f"Failed to reload config: {e}", exc_info=True)

    def _get_mtime(self) -> float:
        """Get file modification time."""
        if self._config_path and self._config_path.exists():
            return self._config_path.stat().st_mtime
        return 0.0

    def _register_sighup(self) -> None:
        """Register SIGHUP handler for manual reload trigger."""
        if self._sighup_registered:
            return
        # SIGHUP not available on Windows
        if not hasattr(signal, "SIGHUP"):
            return
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
            self._sighup_registered = True
            logger.info("SIGHUP handler registered for config reload")
        except (RuntimeError, NotImplementedError):
            # Not in an async context or platform doesn't support it
            pass

    def _on_sighup(self) -> None:
        """Handle SIGHUP signal."""
        logger.info("Received SIGHUP, reloading config...")
        self._reload()
