"""
降级决策引擎

纯编排逻辑：解析策略 → 构建模型链 → 评分排序 → 逐个尝试 → 委托 StreamMonitor / RequestRecorder。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from ..types import RequestContext, TokenUsage
from ..config.schema import (
    StrategyConfig,
    LLMStabilityConfig,
)
from ..providers.base import BaseProvider, ChatResponse, StreamChunk
from ..state.backend import StateBackend
from ..metrics.collector import MetricsCollector, NoopCollector
from ..metrics import registry as m
from ..alert.dispatcher import AlertDispatcher
from .scorer import Scorer
from .recorder import RequestRecorder
from .stream_monitor import StreamMonitor, StrategyTriggered
from .strategies.base import StrategyEvent, StrategyAction

logger = logging.getLogger("kuafu_llm_infra.engine")


class AllProvidersExhausted(Exception):
    """所有提供商（含降级）均已耗尽。"""
    pass


class FallbackEngine:
    """降级引擎：负责提供商选择、重试编排和异常处理。"""

    def __init__(
        self,
        config: LLMStabilityConfig,
        adapters: Dict[str, BaseProvider],
        scorer: Scorer,
        state: StateBackend,
        metrics: Optional[MetricsCollector] = None,
        alert_dispatcher: Optional[AlertDispatcher] = None,
    ) -> None:
        self._config = config
        self._adapters = adapters
        self._scorer = scorer
        self._state = state
        self._metrics = metrics or NoopCollector()
        self._recorder = RequestRecorder(scorer, self._metrics, alert_dispatcher)
        self._stream_monitor = StreamMonitor(self._metrics, self._recorder)

    def update_config(
        self,
        config: LLMStabilityConfig,
        adapters: Dict[str, BaseProvider],
    ) -> None:
        """热更新配置（由 gateway 调用）。"""
        self._config = config
        self._adapters = adapters

    # ------------------------------------------------------------------
    # 非流式调用
    # ------------------------------------------------------------------

    async def execute_chat(
        self,
        business_key: str,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """非流式请求，带降级重试。"""

        # 1. 构建请求上下文
        ctx = RequestContext(
            business_key=business_key,
            messages=messages,
            labels=labels or {},
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            extra_kwargs=kwargs,
        )

        # 2. 根据 business_key 查找策略配置
        strategy_cfg = self._resolve_strategy(ctx)

        # 3. 构建模型链：[主模型, 降级模型1, 降级模型2, ...]
        chain = self._build_model_chain(strategy_cfg)

        last_error: Optional[Exception] = None

        # 4. 按模型链顺序依次尝试
        for canonical_model in chain:

            # 5. 获取该模型下配置的所有提供商
            entries = self._config.get_model_providers(canonical_model)
            # 6. 按复合评分排序（健康×优先级×速度×成功率×稳定性），不健康的会被排除
            ranked = await self._scorer.rank_providers(canonical_model, entries)

            # 7. 逐个尝试排名靠前的提供商
            for sp in ranked:
                adapter = self._adapters.get(sp.provider_name)
                if not adapter:
                    continue

                # 填充上下文：当前使用的模型和提供商
                ctx.canonical_model = canonical_model
                ctx.provider_name = sp.provider_name
                ctx.actual_model_id = self._config.resolve_model_id(
                    canonical_model, sp.provider_name,
                )

                timeout = strategy_cfg.timeout.total
                start = time.monotonic()

                try:
                    # 8. 发起请求，设置总超时
                    response = await asyncio.wait_for(
                        adapter.chat(
                            model=ctx.actual_model_id,
                            messages=messages,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            timeout=timeout,
                            tools=tools,
                            tool_choice=tool_choice,
                            **kwargs,
                        ),
                        timeout=timeout,
                    )

                    duration = time.monotonic() - start

                    # 8. 空响应视为失败，触发切换
                    if not response.content.strip() and not response.tool_calls:
                        raise StrategyTriggered(StrategyEvent(
                            strategy="empty_response",
                            action=StrategyAction.SWITCH,
                            provider=sp.provider_name,
                            model=canonical_model,
                            detail={"elapsed": duration},
                        ))

                    # 9. 成功：记录指标，返回结果
                    await self._recorder.record_success(
                        ctx,
                        duration=duration,
                        usage=response.usage,
                    )
                    return response

                except asyncio.TimeoutError:
                    # 超时：记录失败，尝试下一个提供商
                    duration = time.monotonic() - start
                    logger.warning(
                        f"[engine] {sp.provider_name} timeout after {duration:.1f}s "
                        f"for {canonical_model}"
                    )
                    await self._recorder.record_failure(
                        ctx, "total_timeout", f"timeout after {duration:.1f}s",
                    )
                    last_error = TimeoutError(f"Provider {sp.provider_name} timed out")

                except StrategyTriggered as e:
                    # 策略触发（如空响应）：记录失败，尝试下一个提供商
                    logger.warning(
                        f"[engine] {sp.provider_name} strategy triggered: "
                        f"{e.event.strategy}"
                    )
                    await self._recorder.record_failure(
                        ctx, e.event.strategy, str(e.event.detail),
                    )
                    last_error = e

                except Exception as e:
                    # 其他异常：记录失败，尝试下一个提供商
                    logger.error(f"[engine] {sp.provider_name} error: {e}")
                    await self._recorder.record_failure(ctx, "error", str(e))
                    last_error = e

        # 10. 所有提供商耗尽，发送告警并抛异常
        self._recorder.send_alert(
            "critical", "all_providers_exhausted",
            f"All providers exhausted for business_key={ctx.business_key}",
        )
        raise AllProvidersExhausted(
            f"No provider available for {ctx.business_key}. Last error: {last_error}"
        )

    # ------------------------------------------------------------------
    # 流式调用
    # ------------------------------------------------------------------

    async def execute_chat_stream(
        self,
        business_key: str,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """流式请求，带降级重试。"""

        # 1. 构建请求上下文
        ctx = RequestContext(
            business_key=business_key,
            messages=messages,
            labels=labels or {},
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            extra_kwargs=kwargs,
        )

        # 2. 根据 business_key 查找策略配置
        strategy_cfg = self._resolve_strategy(ctx)

        # 3. 构建模型链：[主模型, 降级模型1, 降级模型2, ...]
        chain = self._build_model_chain(strategy_cfg)

        last_error: Optional[Exception] = None

        # 4. 按模型链顺序依次尝试
        for canonical_model in chain:

            # 5. 获取该模型下配置的所有提供商
            entries = self._config.get_model_providers(canonical_model)
            # 6. 按复合评分排序（健康×优先级×速度×成功率×稳定性），不健康的会被排除
            ranked = await self._scorer.rank_providers(canonical_model, entries)

            # 7. 逐个尝试排名靠前的提供商
            for sp in ranked:
                adapter = self._adapters.get(sp.provider_name)
                if not adapter:
                    continue

                # 填充上下文：当前使用的模型和提供商
                ctx.canonical_model = canonical_model
                ctx.provider_name = sp.provider_name
                ctx.actual_model_id = self._config.resolve_model_id(
                    canonical_model, sp.provider_name,
                )

                try:
                    # 8. 通过 StreamMonitor 包装流，实时检测异常（TTFT 超时、空帧、慢速等）
                    chunk_count = 0
                    async for chunk in self._stream_monitor.monitored_stream(
                        adapter, strategy_cfg, ctx,
                    ):
                        chunk_count += 1
                        yield chunk

                    # 9. 流正常结束且有内容，直接返回
                    if chunk_count > 0:
                        return

                except StrategyTriggered as e:
                    if e.event.action == StrategyAction.SWITCH:
                        # Phase 1（首 Token 前）：还没给用户返回内容，可以切换提供商
                        logger.warning(
                            f"[engine] {sp.provider_name} stream switch: "
                            f"{e.event.strategy}"
                        )
                        await self._recorder.record_failure(
                            ctx, e.event.strategy, str(e.event.detail),
                        )
                        last_error = e
                    else:
                        # Phase 2（内容流中）：已有内容返回给用户，不能切换，仅记录
                        logger.warning(
                            f"[engine] {sp.provider_name} stream record: "
                            f"{e.event.strategy}"
                        )
                        await self._scorer.record_failure(
                            canonical_model, sp.provider_name, e.event.strategy,
                        )
                        return

                except Exception as e:
                    # 其他异常：记录失败，尝试下一个提供商
                    logger.error(f"[engine] {sp.provider_name} stream error: {e}")
                    await self._recorder.record_failure(ctx, "error", str(e))
                    last_error = e

        # 10. 所有提供商耗尽，发送告警并抛异常
        self._recorder.send_alert(
            "critical", "all_providers_exhausted",
            f"All providers exhausted for business_key={ctx.business_key}",
        )
        raise AllProvidersExhausted(
            f"No provider available for {ctx.business_key}. Last error: {last_error}"
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _resolve_strategy(self, ctx: RequestContext) -> StrategyConfig:
        """根据 business_key 查找策略配置；未命中则用 model 构建默认策略。"""
        if ctx.business_key in self._config.strategies:
            return self._config.strategies[ctx.business_key]

        if ctx.model:
            return StrategyConfig(
                primary=ctx.model,
            )

        raise ValueError(
            f"Unknown business_key '{ctx.business_key}' and no model specified"
        )

    @staticmethod
    def _build_model_chain(strategy_cfg: StrategyConfig) -> List[str]:
        """拼接模型链：主模型在前，降级模型按顺序跟在后面。"""
        chain = [strategy_cfg.primary]
        chain.extend(strategy_cfg.fallback)
        return chain
