"""
Background health checker and speed prober.

Periodically sends minimal requests to each (model, provider) pair
to determine health status and measure TTFT. Results are written
to the state backend and exposed via metrics.

健康判定逻辑（基于 ScoreCard 共享计数，多实例安全）：
- 连续探测失败次数 >= failure_threshold → 标记不可用 (health=False) 并触发告警
- 标记不可用后，连续探测成功次数 >= recovery_threshold → 恢复可用 (health=True)
- 单次探测失败不会改变健康状态
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
        """探测单个 (model, provider)。

        使用 ScoreCard 中的共享计数（probe_consecutive_failures / probe_consecutive_successes）
        判断是否达到阈值，确保多实例部署下计数一致：
        - 连续失败 >= failure_threshold → 标记不可用并告警
        - 不可用状态下连续成功 >= recovery_threshold → 恢复可用
        """
        adapter = self._adapters.get(provider_name)
        if not adapter:
            return

        hc = self._config.health_check
        start = time.monotonic()
        first_token_time: Optional[float] = None
        has_content = False

        try:
            logger.debug(f"Probe start: ({canonical_model}, {provider_name}, max_tokens={hc.probe_max_tokens}) ")
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

            # 无实际内容视为探测失败（可能是空响应、内容被过滤等）
            if not has_content:
                if first_token_time is not None:
                    reason = "empty_content (stream received but no visible content after processing)"
                else:
                    reason = "empty_response (no chunks received)"
                raise ValueError(reason)

            # ---- 探测成功 ----
            # 先写入 ProbeResult（health=True），update_probe 会自动更新共享计数
            result = ProbeResult(
                provider=provider_name,
                model=canonical_model,
                health=True,
                ttft_ms=ttft_ms,
                valid_response=True,
                timestamp=time.time(),
            )
            card = await self._state.get_score_card(canonical_model, provider_name)
            was_unhealthy = not card.health
            card.update_probe(result)

            # 根据共享计数判断是否恢复
            if was_unhealthy:
                if card.probe_consecutive_successes >= hc.recovery_threshold:
                    card.health = True
                    logger.info(
                        f"Probe ({canonical_model}, {provider_name}): "
                        f"连续成功 {card.probe_consecutive_successes} 次，恢复可用"
                    )
                else:
                    # 还在恢复中，保持不可用状态
                    card.health = False
                    logger.info(
                        f"Probe ({canonical_model}, {provider_name}): "
                        f"探测成功，恢复中 "
                        f"({card.probe_consecutive_successes}/{hc.recovery_threshold})"
                    )
            else:
                card.health = True

            await self._state.set_probe_result(canonical_model, provider_name, result)
            await self._state.update_score_card(canonical_model, provider_name, card)

            self._metrics.set(
                m.PROVIDER_HEALTH, 1.0 if card.health else 0.0,
                provider=provider_name,
            )
            self._metrics.observe(m.PROBE_TTFT, ttft_ms / 1000.0, provider=provider_name)
            self._metrics.inc(m.PROBE_TOTAL, provider=provider_name, status="success")

            logger.debug(
                f"Probe done: ({canonical_model}, {provider_name}) "
                f"healthy={card.health}, ttft={ttft_ms:.0f}ms"
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # ---- 探测失败 ----
            # 写入 ProbeResult（health=False），update_probe 会自动更新共享计数
            result = ProbeResult(
                provider=provider_name,
                model=canonical_model,
                health=False,
                timestamp=time.time(),
            )
            card = await self._state.get_score_card(canonical_model, provider_name)
            was_healthy = card.health
            card.update_probe(result)

            # 根据共享计数判断是否标记不可用
            if card.probe_consecutive_failures >= hc.failure_threshold:
                card.health = False
            # 未达阈值则保持当前 health 不变（card.health 未被覆盖）

            await self._state.set_probe_result(canonical_model, provider_name, result)
            await self._state.update_score_card(canonical_model, provider_name, card)

            self._metrics.set(
                m.PROVIDER_HEALTH, 1.0 if card.health else 0.0,
                provider=provider_name,
            )
            self._metrics.inc(m.PROBE_TOTAL, provider=provider_name, status="error")

            # 探测失败日志：输出异常类型 + 详情
            error_detail = str(e) or repr(e) or "(no detail)"
            extra_parts = []
            for attr in ("status_code", "status", "code", "response"):
                val = getattr(e, attr, None)
                if val is not None:
                    extra_parts.append(f"{attr}={val}")
            if extra_parts:
                error_detail = f"{error_detail} ({', '.join(extra_parts)})"

            if card.probe_consecutive_failures < hc.failure_threshold:
                # 未达阈值，仅记录日志
                logger.warning(
                    f"Probe ({canonical_model}, {provider_name}): "
                    f"失败 ({card.probe_consecutive_failures}/{hc.failure_threshold}) "
                    f"- [{type(e).__name__}] {error_detail}"
                )
            else:
                # 达到阈值，记录日志 + 发送告警（仅在状态从可用变为不可用时）
                logger.error(
                    f"Probe ({canonical_model}, {provider_name}): "
                    f"连续失败 {card.probe_consecutive_failures} 次，标记不可用 "
                    f"- [{type(e).__name__}] {error_detail}"
                )
                if was_healthy and self._alert_dispatcher:
                    self._alert_dispatcher.dispatch(AlertEvent(
                        level="critical",
                        title="提供商标记不可用",
                        message=(
                            f"({canonical_model}, {provider_name}) 连续探测失败 "
                            f"{card.probe_consecutive_failures} 次，已标记为不可用。"
                            f"最近错误: [{type(e).__name__}] {error_detail}"
                        ),
                        provider=provider_name,
                        model=canonical_model,
                    ))
