"""
降级决策引擎

纯编排逻辑：解析策略 → 构建模型链 → 评分排序 → 逐个尝试 → 委托 StreamMonitor / RequestRecorder。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

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


def _label_tag(labels: Dict[str, str]) -> str:
    """将 labels 格式化为日志标签串，如 '[app_id=xxx, module=yyy]'。"""
    if not labels:
        return ""
    parts = ", ".join(f"{k}={v}" for k, v in labels.items())
    return f" [{parts}]"


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
    # 候选遍历（统一管理 deadline + 模型链 + 提供商排序）
    # ------------------------------------------------------------------

    async def _iter_candidates(
        self,
        strategy_cfg: StrategyConfig,
        chain: List[str],
        ctx: RequestContext,
        attempt_counter: List[int],
    ) -> AsyncIterator[Tuple[BaseProvider, float]]:
        """
        遍历模型链中所有候选提供商，yield (adapter, timeout)。

        - 按模型链顺序 → 每个模型的提供商按评分排序
        - 自动管理 deadline，剩余预算不足时终止
        - 自动填充 ctx 的 canonical_model / provider_name / actual_model_id
        - attempt_counter: 单元素列表，用于在调用方和生成器之间共享尝试计数
        """
        tag = _label_tag(ctx.labels)
        deadline = time.monotonic() + strategy_cfg.timeout.total
        per_request = strategy_cfg.timeout.per_request

        for idx, canonical_model in enumerate(chain):
            # 检查链路剩余预算
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    f"[{ctx.business_key}]{tag} 链路总超时已到期，"
                    f"剩余模型未尝试: {chain[idx:]}"
                )
                return

            is_fallback = idx > 0
            if is_fallback:
                logger.info(
                    f"[{ctx.business_key}]{tag} >>> 降级到模型: "
                    f"{canonical_model} (剩余 {remaining:.1f}s)"
                )

            # 获取该模型下配置的所有提供商（含排除信息）
            entries = self._config.get_model_providers(canonical_model)
            rank_result = await self._scorer.rank_providers(canonical_model, entries)

            # 日志：被排除的提供商（unhealthy / cooldown）
            if rank_result.excluded:
                excluded_desc = ", ".join(
                    f"{sp.provider_name}({sp.reason})"
                    for sp in rank_result.excluded
                )
                logger.info(
                    f"[{ctx.business_key}]{tag} "
                    f"模型 {canonical_model} 排除: {excluded_desc}"
                )

            if not rank_result.ranked:
                logger.warning(
                    f"[{ctx.business_key}]{tag} "
                    f"模型 {canonical_model} 无可用提供商"
                )
                continue

            for sp in rank_result.ranked:
                # 每次尝试前重新检查剩余预算
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        f"[{ctx.business_key}]{tag} 链路总超时已到期，"
                        f"模型 {canonical_model} 剩余提供商未尝试"
                    )
                    return

                adapter = self._adapters.get(sp.provider_name)
                if not adapter:
                    continue

                # 填充上下文：当前使用的模型和提供商
                ctx.canonical_model = canonical_model
                ctx.provider_name = sp.provider_name
                ctx.actual_model_id = self._config.resolve_model_id(
                    canonical_model, sp.provider_name,
                )

                # 单次超时 = min(配置的单次超时, 链路剩余预算)
                timeout = min(per_request, remaining)
                attempt_counter[0] += 1
                yield adapter, timeout

    # ------------------------------------------------------------------
    # 非流式调用
    # ------------------------------------------------------------------

    async def execute_chat(
        self,
        business_key: str,
        messages: List[Dict[str, Any]],
        *,
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
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            extra_kwargs=kwargs,
        )

        tag = _label_tag(ctx.labels)

        # 2. 根据 business_key 查找策略配置
        strategy_cfg = self._resolve_strategy(ctx)

        # 3. 构建模型链：[主模型, 降级模型1, 降级模型2, ...]
        chain = self._build_model_chain(strategy_cfg)

        logger.info(
            f"[{business_key}]{tag} 请求开始 | "
            f"模型链: {' → '.join(chain)} | "
            f"超时: {strategy_cfg.timeout.per_request}s/次, "
            f"{strategy_cfg.timeout.total}s/总"
        )

        chain_start = time.monotonic()
        last_error: Optional[Exception] = None
        attempt = [0]  # 使用列表在生成器和调用方之间共享计数

        # 4. 按模型链顺序依次尝试，deadline 自动管理总超时
        async for adapter, timeout in self._iter_candidates(strategy_cfg, chain, ctx, attempt):
            start = time.monotonic()

            try:
                # 5. 发起请求，设置单次超时（受 deadline 约束）
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

                # 6. 空响应视为失败，触发切换
                if not response.content.strip() and not response.tool_calls:
                    raise StrategyTriggered(StrategyEvent(
                        strategy="empty_response",
                        action=StrategyAction.SWITCH,
                        provider=ctx.provider_name,
                        model=ctx.canonical_model,
                        detail={"elapsed": duration},
                    ))

                # 7. 成功：记录指标，返回结果
                await self._recorder.record_success(
                    ctx,
                    duration=duration,
                    usage=response.usage,
                )
                total_duration = time.monotonic() - chain_start
                logger.info(
                    f"[{business_key}]{tag} "
                    f"#{attempt[0]} {ctx.provider_name} → 成功 | "
                    f"{duration:.2f}s, {response.usage.total_tokens}tokens"
                    + (f", 总耗时={total_duration:.2f}s" if attempt[0] > 1 else "")
                )
                return response

            except asyncio.TimeoutError:
                duration = time.monotonic() - start
                logger.warning(
                    f"[{business_key}]{tag} "
                    f"#{attempt[0]} {ctx.provider_name} → 超时 ({duration:.1f}s)"
                )
                await self._recorder.record_failure(
                    ctx, "total_timeout", f"timeout after {duration:.1f}s",
                )
                last_error = TimeoutError(f"Provider {ctx.provider_name} timed out")

            except StrategyTriggered as e:
                duration = time.monotonic() - start
                logger.warning(
                    f"[{business_key}]{tag} "
                    f"#{attempt[0]} {ctx.provider_name} → "
                    f"{e.event.strategy} ({duration:.2f}s)"
                )
                await self._recorder.record_failure(
                    ctx, e.event.strategy, str(e.event.detail),
                )
                last_error = e

            except Exception as e:
                duration = time.monotonic() - start
                logger.warning(
                    f"[{business_key}]{tag} "
                    f"#{attempt[0]} {ctx.provider_name} → "
                    f"[{type(e).__name__}] {e} ({duration:.2f}s)"
                )
                await self._recorder.record_failure(ctx, "error", str(e))
                last_error = e

        # 8. 所有提供商耗尽或 deadline 到期，发送告警并抛异常
        total_duration = time.monotonic() - chain_start
        logger.error(
            f"[{business_key}]{tag} 全部耗尽 | "
            f"{attempt[0]}次尝试, 总耗时={total_duration:.2f}s, "
            f"最后错误={last_error}"
        )
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
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            extra_kwargs=kwargs,
        )

        tag = _label_tag(ctx.labels)

        # 2. 根据 business_key 查找策略配置
        strategy_cfg = self._resolve_strategy(ctx)

        # 3. 构建模型链
        chain = self._build_model_chain(strategy_cfg)

        logger.info(
            f"[{business_key}]{tag} 流式请求开始 | "
            f"模型链: {' → '.join(chain)} | "
            f"超时: ttft={strategy_cfg.timeout.ttft}s, "
            f"gap={strategy_cfg.timeout.chunk_gap}s, "
            f"{strategy_cfg.timeout.per_request}s/次, "
            f"{strategy_cfg.timeout.total}s/总"
        )

        chain_start = time.monotonic()
        last_error: Optional[Exception] = None
        attempt = [0]

        # 4. 按模型链顺序依次尝试，deadline 自动管理总超时
        async for adapter, timeout in self._iter_candidates(strategy_cfg, chain, ctx, attempt):
            start = time.monotonic()
            # 计算传给 StreamMonitor 的 SDK 超时：min(ttft + 5, 剩余预算)
            stream_timeout = min(strategy_cfg.timeout.ttft + 5, timeout)

            try:
                # 5. 通过 StreamMonitor 包装流，实时检测异常（TTFT 超时、空帧、慢速等）
                chunk_count = 0
                async for chunk in self._stream_monitor.monitored_stream(
                    adapter, strategy_cfg, ctx,
                    timeout=stream_timeout,
                ):
                    chunk_count += 1
                    yield chunk

                # 6. 流正常结束且有内容，直接返回
                if chunk_count > 0:
                    duration = time.monotonic() - start
                    total_duration = time.monotonic() - chain_start
                    logger.info(
                        f"[{business_key}]{tag} "
                        f"#{attempt[0]} {ctx.provider_name} → 成功 | "
                        f"{duration:.2f}s, {chunk_count}chunks"
                        + (f", 总耗时={total_duration:.2f}s" if attempt[0] > 1 else "")
                    )
                    return

            except StrategyTriggered as e:
                duration = time.monotonic() - start
                if e.event.action == StrategyAction.SWITCH:
                    # Phase 1（首 Token 前）：还没给用户返回内容，可以切换提供商
                    logger.warning(
                        f"[{business_key}]{tag} "
                        f"#{attempt[0]} {ctx.provider_name} → "
                        f"{e.event.strategy} ({duration:.2f}s)"
                    )
                    await self._recorder.record_failure(
                        ctx, e.event.strategy, str(e.event.detail),
                    )
                    last_error = e
                else:
                    # Phase 2（内容流中）：已有内容返回给用户，不能切换，仅记录
                    logger.warning(
                        f"[{business_key}]{tag} "
                        f"#{attempt[0]} {ctx.provider_name} → "
                        f"流中异常({e.event.strategy}, 不切换) ({duration:.2f}s)"
                    )
                    await self._scorer.record_failure(
                        ctx.canonical_model, ctx.provider_name, e.event.strategy,
                    )
                    return

            except Exception as e:
                duration = time.monotonic() - start
                logger.warning(
                    f"[{business_key}]{tag} "
                    f"#{attempt[0]} {ctx.provider_name} → "
                    f"[{type(e).__name__}] {e} ({duration:.2f}s)"
                )
                await self._recorder.record_failure(ctx, "error", str(e))
                last_error = e

        # 7. 所有提供商耗尽或 deadline 到期，发送告警并抛异常
        total_duration = time.monotonic() - chain_start
        logger.error(
            f"[{business_key}]{tag} 全部耗尽 | "
            f"{attempt[0]}次尝试, 总耗时={total_duration:.2f}s, "
            f"最后错误={last_error}"
        )
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
        """根据 business_key 查找策略配置。"""
        if ctx.business_key in self._config.strategies:
            return self._config.strategies[ctx.business_key]

        raise ValueError(
            f"Unknown business_key '{ctx.business_key}'"
        )

    @staticmethod
    def _build_model_chain(strategy_cfg: StrategyConfig) -> List[str]:
        """拼接模型链：主模型在前，降级模型按顺序跟在后面。"""
        chain = [strategy_cfg.primary]
        chain.extend(strategy_cfg.fallback)
        return chain
