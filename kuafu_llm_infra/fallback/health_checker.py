"""
Background health checker and speed prober.

Periodically sends minimal requests to each (model, provider) pair
to determine health status and measure TTFT. Results are written
to the state backend and exposed via metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from ..config.schema import LLMStabilityConfig, adapter_key
from ..providers.base import BaseProvider
from ..state.backend import StateBackend, ProbeResult
from ..metrics.collector import MetricsCollector, NoopCollector
from ..metrics import registry as m
from ..alert.dispatcher import AlertDispatcher
from ..alert.channels.base import AlertEvent

logger = logging.getLogger("kuafu_llm_infra.health_checker")


class HealthChecker:
    """Background health checker — probes per (model, provider)."""

    def __init__(
        self,
        config: LLMStabilityConfig,
        adapters: Dict[str, BaseProvider],
        state: StateBackend,
        metrics: Optional[MetricsCollector] = None,
        alert_dispatcher: Optional[AlertDispatcher] = None,
    ) -> None:
        self._config = config
        self._adapters = adapters
        self._state = state
        self._metrics = metrics or NoopCollector()
        self._alert_dispatcher = alert_dispatcher
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._probe_loop())
            logger.info("Health checker started")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Health checker stopped")

    def update_config(
        self,
        config: LLMStabilityConfig,
        adapters: Dict[str, BaseProvider],
    ) -> None:
        """Apply new configuration (called by gateway on hot-reload)."""
        self._config = config
        self._adapters = adapters

    def _collect_probe_targets(self) -> List[tuple]:
        """
        Collect all (canonical_model, adapter_key, actual_model_id) tuples
        that need probing from the models config.
        """
        targets = []
        for canonical_model, model_cfg in self._config.models.items():
            for entry in model_cfg.providers:
                if not entry.probe:
                    continue
                provider_cfg = self._config.providers.get(entry.provider)
                if not provider_cfg or not provider_cfg.enabled:
                    continue
                key = adapter_key(entry.provider, entry.endpoint)
                if key not in self._adapters:
                    continue
                actual_model_id = entry.model_id or canonical_model
                targets.append((canonical_model, key, actual_model_id))
        return targets

    async def _probe_loop(self) -> None:
        """Main loop: probe each (model, provider) in staggered sequence."""
        hc = self._config.health_check
        while True:
            try:
                targets = self._collect_probe_targets()
                for canonical_model, provider_name, actual_model_id in targets:
                    acquired = await self._state.try_acquire_probe_lock(
                        canonical_model, provider_name, hc.interval,
                    )
                    if not acquired:
                        logger.debug(
                            f"Probe lock not acquired for ({canonical_model}, {provider_name})"
                        )
                        continue

                    asyncio.create_task(
                        self._probe_one(canonical_model, provider_name, actual_model_id)
                    )
                    await asyncio.sleep(hc.stagger_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Probe loop error: {e}", exc_info=True)

            await asyncio.sleep(hc.interval)

    async def _probe_one(
        self,
        canonical_model: str,
        provider_name: str,
        actual_model_id: str,
    ) -> None:
        """Probe a single (model, provider) pair."""
        adapter = self._adapters.get(provider_name)
        if not adapter:
            return

        hc = self._config.health_check
        start = time.monotonic()
        first_token_time: Optional[float] = None
        has_content = False

        try:
            async for chunk in adapter.probe(
                model=actual_model_id,
                max_tokens=hc.probe_max_tokens,
                timeout=hc.timeout,
            ):
                if first_token_time is None:
                    first_token_time = time.monotonic()
                if chunk.content:
                    has_content = True

            ttft_ms = ((first_token_time - start) * 1000) if first_token_time else 0.0

            result = ProbeResult(
                provider=provider_name,
                model=canonical_model,
                health=True,
                ttft_ms=ttft_ms,
                valid_response=has_content,
                timestamp=time.time(),
            )

            await self._state.set_probe_result(canonical_model, provider_name, result)
            card = await self._state.get_score_card(canonical_model, provider_name)
            card.update_probe(result)
            await self._state.update_score_card(canonical_model, provider_name, card)

            self._metrics.set(m.PROVIDER_HEALTH, 1.0, provider=provider_name)
            self._metrics.observe(m.PROBE_TTFT, ttft_ms / 1000.0, provider=provider_name)
            self._metrics.inc(m.PROBE_TOTAL, provider=provider_name, status="success")

            logger.debug(
                f"Probe ({canonical_model}, {provider_name}): "
                f"healthy, ttft={ttft_ms:.0f}ms"
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            result = ProbeResult(
                provider=provider_name,
                model=canonical_model,
                health=False,
                timestamp=time.time(),
            )
            await self._state.set_probe_result(canonical_model, provider_name, result)
            card = await self._state.get_score_card(canonical_model, provider_name)
            card.update_probe(result)
            await self._state.update_score_card(canonical_model, provider_name, card)

            self._metrics.set(m.PROVIDER_HEALTH, 0.0, provider=provider_name)
            self._metrics.inc(m.PROBE_TOTAL, provider=provider_name, status="error")

            # 探测失败日志：输出异常类型 + 详情，避免某些 SDK 异常 str() 为空
            error_detail = str(e) or repr(e) or "(no detail)"
            # 附加异常属性（HTTP status_code / response body 等），帮助定位问题
            extra_parts = []
            for attr in ("status_code", "status", "code", "response"):
                val = getattr(e, attr, None)
                if val is not None:
                    extra_parts.append(f"{attr}={val}")
            if extra_parts:
                error_detail = f"{error_detail} ({', '.join(extra_parts)})"
            logger.warning(
                f"Probe ({canonical_model}, {provider_name}): "
                f"failed - [{type(e).__name__}] {error_detail}"
            )

            if self._alert_dispatcher:
                self._alert_dispatcher.dispatch(AlertEvent(
                    level="warning",
                    title="probe_failed",
                    message=f"Probe failed for {canonical_model} @ {provider_name}: [{type(e).__name__}] {error_detail}",
                    provider=provider_name,
                    model=canonical_model,
                ))
