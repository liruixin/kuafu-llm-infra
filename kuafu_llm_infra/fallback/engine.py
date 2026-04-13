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

    # 可重试的 HTTP 状态码：限流、网关错误等瞬时异常
    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503}

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
    # 重试判断
    # ------------------------------------------------------------------

    @classmethod
    def _should_retry(cls, error: Exception) -> bool:
        """判断是否应该重试当前提供商。

        只有瞬时可恢复的错误才重试：
        - HTTP 429（限流）、500/502/503（网关 / 服务端瞬时错误）
        - 网络连接错误（ConnectionError 等）

        不重试的情况：
        - 超时（提供商当前就是慢，重试大概率还是超时）
        - 策略触发（TTFT 超时、空响应、慢速等，属于引擎决策）
        - 认证失败（401/403，配置问题）
        - 内容过滤（确定性结果）
        """
        # 检查 HTTP 状态码（适用于 OpenAI/Anthropic/Google SDK 的异常）
        status_code = getattr(error, "status_code", None)
        if status_code is not None and status_code in cls._RETRYABLE_STATUS_CODES:
            return True

        # 网络连接类错误
        if isinstance(error, (ConnectionError, OSError)):
            return True

        return False

    @staticmethod
    def _is_rate_limit(error: Exception) -> bool:
        """判断是否为限流错误（429），限流需要短暂等待后再重试。"""
        return getattr(error, "status_code", None) == 429

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
        failure_details: List[str] = []  # 收集每次失败的摘要，用于告警

        # 4. 按模型链顺序依次尝试，deadline 自动管理总超时
        async for adapter, timeout in self._iter_candidates(strategy_cfg, chain, ctx, attempt):
            provider_retries = 0  # 当前提供商已重试次数

            while True:  # 重试循环：对同一个提供商进行重试
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

                    # 6. 空响应视为失败，触发切换（不重试）
                    if not response.content.strip() and not response.tool_calls:
                        raise StrategyTriggered(StrategyEvent(
                            strategy="empty_response",
                            action=StrategyAction.SWITCH,
                            provider=ctx.provider_name,
                            model=ctx.canonical_model,
                            detail={"elapsed": duration},
                        ))

                    # 7. 成功：记录指标，返回结果
                    tps = response.usage.completion_tokens / duration if duration > 0 else 0
                    logger.info(f"总token: {response.usage.completion_tokens}, 总耗时: {duration:.2f}s, tps: {tps}")
                    await self._recorder.record_success(
                        ctx,
                        duration=duration,
                        tps=tps,
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
                    # 超时：不重试，直接切换下一个提供商
                    duration = time.monotonic() - start
                    logger.warning(
                        f"[{business_key}]{tag} "
                        f"#{attempt[0]} {ctx.provider_name} → 超时 ({duration:.1f}s)"
                    )
                    await self._recorder.record_failure(
                        ctx, "total_timeout", f"timeout after {duration:.1f}s",
                    )
                    last_error = TimeoutError(f"Provider {ctx.provider_name} timed out")
                    failure_details.append(
                        f"#{attempt[0]} {ctx.provider_name} → 超时 ({duration:.1f}s)"
                    )
                    break  # 跳出重试循环，切换提供商

                except StrategyTriggered as e:
                    # 策略触发（空响应、TTFT 超时等）：不重试，直接切换
                    duration = time.monotonic() - start
                    detail_str = " | ".join(f"{k}={v}" for k, v in e.event.detail.items())
                    logger.warning(
                        f"[{business_key}]{tag} "
                        f"#{attempt[0]} {ctx.provider_name} → "
                        f"{e.event.strategy} ({duration:.2f}s) [{detail_str}]"
                    )
                    await self._recorder.record_failure(
                        ctx, e.event.strategy, str(e.event.detail),
                    )
                    last_error = e
                    failure_details.append(
                        f"#{attempt[0]} {ctx.provider_name} → {e.event.strategy} ({duration:.2f}s) [{detail_str}]"
                    )
                    break  # 跳出重试循环，切换提供商

                except Exception as e:
                    duration = time.monotonic() - start

                    # 判断是否可以重试当前提供商
                    if (self._should_retry(e)
                            and provider_retries < strategy_cfg.max_retries):
                        provider_retries += 1

                        # 重新计算剩余预算，超时则放弃重试
                        remaining = strategy_cfg.timeout.total - (time.monotonic() - chain_start)
                        if remaining <= 0:
                            last_error = e
                            failure_details.append(
                                f"#{attempt[0]} {ctx.provider_name} → [{type(e).__name__}] {e} ({duration:.2f}s)"
                            )
                            break

                        timeout = min(strategy_cfg.timeout.per_request, remaining)

                        # 限流（429）短暂等待后重试，其他错误立即重试
                        if self._is_rate_limit(e):
                            logger.info(
                                f"[{business_key}]{tag} "
                                f"#{attempt[0]} {ctx.provider_name} → "
                                f"限流, 等待1s后重试 "
                                f"({provider_retries}/{strategy_cfg.max_retries})"
                            )
                            await asyncio.sleep(1.0)
                        else:
                            logger.info(
                                f"[{business_key}]{tag} "
                                f"#{attempt[0]} {ctx.provider_name} → "
                                f"[{type(e).__name__}] {e}, 重试 "
                                f"({provider_retries}/{strategy_cfg.max_retries})"
                            )

                        continue  # 重试当前提供商

                    # 不可重试或重试次数已耗尽，切换下一个提供商
                    logger.warning(
                        f"[{business_key}]{tag} "
                        f"#{attempt[0]} {ctx.provider_name} → "
                        f"[{type(e).__name__}] {e} ({duration:.2f}s)"
                    )
                    await self._recorder.record_failure(ctx, "error", str(e))
                    last_error = e
                    failure_details.append(
                        f"#{attempt[0]} {ctx.provider_name} → [{type(e).__name__}] {e} ({duration:.2f}s)"
                    )
                    break  # 跳出重试循环，切换提供商

        # 8. 所有提供商耗尽或 deadline 到期，发送告警并抛异常
        total_duration = time.monotonic() - chain_start
        logger.error(
            f"[{business_key}]{tag} 全部耗尽 | "
            f"{attempt[0]}次尝试, 总耗时={total_duration:.2f}s, "
            f"最后错误={last_error}"
        )
        detail_block = ""
        if failure_details:
            detail_block = "\n\n失败明细:\n" + "\n".join(f"  {d}" for d in failure_details)
        self._recorder.send_alert(
            "critical", "所有提供商耗尽",
            f"模型链 {' → '.join(chain)} 全部失败，"
            f"共尝试 {attempt[0]} 次，总耗时 {total_duration:.2f}s。"
            f"最后错误: {last_error}{detail_block}",
            business_key=ctx.business_key,
            labels=ctx.labels,
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
        failure_details: List[str] = []  # 收集每次失败的摘要，用于告警

        # 4. 按模型链顺序依次尝试，deadline 自动管理总超时
        async for adapter, timeout in self._iter_candidates(strategy_cfg, chain, ctx, attempt):
            provider_retries = 0  # 当前提供商已重试次数

            while True:  # 重试循环：对同一个提供商进行重试
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

                    # 流结束但没有任何内容（空响应），不重试，直接切换
                    break

                except StrategyTriggered as e:
                    # 策略触发（TTFT 超时、空帧、慢速等）：不重试，根据阶段决定行为
                    duration = time.monotonic() - start
                    detail_str = " | ".join(f"{k}={v}" for k, v in e.event.detail.items())
                    if e.event.action == StrategyAction.SWITCH:
                        # Phase 1（首 Token 前）：还没给用户返回内容，可以切换提供商
                        logger.warning(
                            f"[{business_key}]{tag} "
                            f"#{attempt[0]} {ctx.provider_name} → "
                            f"{e.event.strategy} ({duration:.2f}s) [{detail_str}]"
                        )
                        await self._recorder.record_failure(
                            ctx, e.event.strategy, str(e.event.detail),
                        )
                        last_error = e
                        failure_details.append(
                            f"#{attempt[0]} {ctx.provider_name} → {e.event.strategy} ({duration:.2f}s) [{detail_str}]"
                        )
                    else:
                        # Phase 2（内容流中）：已有内容返回给用户，不能切换，仅记录
                        logger.warning(
                            f"[{business_key}]{tag} "
                            f"#{attempt[0]} {ctx.provider_name} → "
                            f"流中异常({e.event.strategy}, 不切换) ({duration:.2f}s) [{detail_str}]"
                        )
                        await self._scorer.record_failure(
                            ctx.canonical_model, ctx.provider_name, e.event.strategy,
                        )
                        return
                    break  # 跳出重试循环，切换提供商

                except Exception as e:
                    duration = time.monotonic() - start

                    # 流式重试前提：还没有向用户返回任何内容（chunk_count == 0）
                    # 一旦已经 yield 了内容，就不能重试，否则用户会收到重复数据
                    if (chunk_count == 0
                            and self._should_retry(e)
                            and provider_retries < strategy_cfg.max_retries):
                        provider_retries += 1

                        # 重新计算剩余预算，超时则放弃重试
                        remaining = strategy_cfg.timeout.total - (time.monotonic() - chain_start)
                        if remaining <= 0:
                            last_error = e
                            failure_details.append(
                                f"#{attempt[0]} {ctx.provider_name} → [{type(e).__name__}] {e} ({duration:.2f}s)"
                            )
                            break

                        timeout = min(strategy_cfg.timeout.per_request, remaining)

                        # 限流（429）短暂等待后重试，其他错误立即重试
                        if self._is_rate_limit(e):
                            logger.info(
                                f"[{business_key}]{tag} "
                                f"#{attempt[0]} {ctx.provider_name} → "
                                f"限流, 等待1s后重试 "
                                f"({provider_retries}/{strategy_cfg.max_retries})"
                            )
                            await asyncio.sleep(1.0)
                        else:
                            logger.info(
                                f"[{business_key}]{tag} "
                                f"#{attempt[0]} {ctx.provider_name} → "
                                f"[{type(e).__name__}] {e}, 重试 "
                                f"({provider_retries}/{strategy_cfg.max_retries})"
                            )

                        continue  # 重试当前提供商

                    # 不可重试 / 重试次数已耗尽 / 已有内容返回（chunk_count > 0）
                    if chunk_count > 0:
                        # 已有内容返回给用户，无法切换，仅记录异常
                        logger.warning(
                            f"[{business_key}]{tag} "
                            f"#{attempt[0]} {ctx.provider_name} → "
                            f"流中异常([{type(e).__name__}] {e}, 不切换) "
                            f"({duration:.2f}s)"
                        )
                        await self._recorder.record_failure(ctx, "error", str(e))
                        return

                    logger.warning(
                        f"[{business_key}]{tag} "
                        f"#{attempt[0]} {ctx.provider_name} → "
                        f"[{type(e).__name__}] {e} ({duration:.2f}s)"
                    )
                    await self._recorder.record_failure(ctx, "error", str(e))
                    last_error = e
                    failure_details.append(
                        f"#{attempt[0]} {ctx.provider_name} → [{type(e).__name__}] {e} ({duration:.2f}s)"
                    )
                    break  # 跳出重试循环，切换提供商

        # 7. 所有提供商耗尽或 deadline 到期，发送告警并抛异常
        total_duration = time.monotonic() - chain_start
        logger.error(
            f"[{business_key}]{tag} 全部耗尽 | "
            f"{attempt[0]}次尝试, 总耗时={total_duration:.2f}s, "
            f"最后错误={last_error}"
        )
        detail_block = ""
        if failure_details:
            detail_block = "\n\n失败明细:\n" + "\n".join(f"  {d}" for d in failure_details)
        self._recorder.send_alert(
            "critical", "所有提供商耗尽",
            f"模型链 {' → '.join(chain)} 全部失败，"
            f"共尝试 {attempt[0]} 次，总耗时 {total_duration:.2f}s。"
            f"最后错误: {last_error}{detail_block}",
            business_key=ctx.business_key,
            labels=ctx.labels,
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
