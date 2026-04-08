"""
Background health checker and speed prober (combined).

Periodically sends minimal requests to each provider to
determine health status and measure TTFT. Results are written
to the state backend and exposed via metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from ..config.schema import ProviderConfig, HealthCheckConfig
from ..providers.base import BaseProvider
from ..state.backend import StateBackend, ProbeResult
from ..metrics.collector import MetricsCollector, NoopCollector

logger = logging.getLogger("kuafu_llm_infra.health_checker")


class HealthChecker:
    """Combined health checker and speed prober."""

    def __init__(
        self,
        config: HealthCheckConfig,
        providers: Dict[str, ProviderConfig],
        adapters: Dict[str, BaseProvider],
        state: StateBackend,
        metrics: Optional[MetricsCollector] = None,
        alert_queue: Optional[asyncio.Queue] = None,
    ) -> None:
        self._config = config
        self._providers = providers
        self._adapters = adapters
        self._state = state
        self._metrics = metrics or NoopCollector()
        self._alert_queue = alert_queue
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start the periodic probe loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._probe_loop())
            logger.info("Health checker started")

    def stop(self) -> None:
        """Stop the periodic probe loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Health checker stopped")

    async def _probe_loop(self) -> None:
        """Main loop: probe each provider in staggered sequence."""
        while True:
            try:
                for name, provider_cfg in self._providers.items():
                    if not provider_cfg.enabled:
                        continue
                    if name not in self._adapters:
                        continue

                    # Probe coordination: try to acquire lock
                    acquired = await self._state.try_acquire_probe_lock(
                        name, self._config.interval
                    )
                    if not acquired:
                        logger.debug(f"Probe lock not acquired for {name}, skipping")
                        continue

                    asyncio.create_task(self._probe_one(name, provider_cfg))

                    # Stagger probes
                    await asyncio.sleep(self._config.stagger_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Probe loop error: {e}", exc_info=True)

            await asyncio.sleep(self._config.interval)

    async def _probe_one(self, name: str, provider_cfg: ProviderConfig) -> None:
        """Probe a single provider: health + TTFT measurement."""
        adapter = self._adapters.get(name)
        if not adapter:
            return

        ping_model = provider_cfg.ping_model
        if not ping_model:
            return

        start = time.monotonic()
        first_token_time: Optional[float] = None
        has_content = False

        try:
            async for chunk in adapter.probe(
                model=ping_model,
                max_tokens=self._config.probe_max_tokens,
                timeout=self._config.timeout,
            ):
                if first_token_time is None:
                    first_token_time = time.monotonic()
                if chunk.content:
                    has_content = True

            ttft_ms = ((first_token_time - start) * 1000) if first_token_time else 0.0

            result = ProbeResult(
                provider=name,
                health=True,
                ttft_ms=ttft_ms,
                valid_response=has_content,
                timestamp=time.time(),
            )

            # Update state and metrics
            await self._state.set_probe_result(name, result)
            card = await self._state.get_score_card(ping_model, name)
            card.update_probe(result)
            await self._state.update_score_card(ping_model, name, card)

            self._metrics.set_provider_health(name, True)
            self._metrics.observe_probe_ttft(name, ttft_ms / 1000.0)
            self._metrics.inc_probe_total(name, "success")

            logger.debug(f"Probe {name}: healthy, ttft={ttft_ms:.0f}ms")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            result = ProbeResult(
                provider=name,
                health=False,
                timestamp=time.time(),
            )
            await self._state.set_probe_result(name, result)
            card = await self._state.get_score_card(ping_model, name)
            card.update_probe(result)
            await self._state.update_score_card(ping_model, name, card)

            self._metrics.set_provider_health(name, False)
            self._metrics.inc_probe_total(name, "error")

            logger.warning(f"Probe {name}: failed - {e}")

            if self._alert_queue:
                from ..alert.channels.base import AlertEvent
                self._alert_queue.put_nowait(AlertEvent(
                    level="warning",
                    title="probe_failed",
                    message=f"Provider {name} probe failed: {e}",
                    provider=name,
                ))
