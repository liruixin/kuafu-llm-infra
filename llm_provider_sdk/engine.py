"""降级引擎：从池子里按概率挑模型，首 token 太慢或报错就换下一个。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Dict, List

from .config.schema import LLMStabilityConfig, StrategyConfig, adapter_key
from .pool import ModelPool, ModelStats
from .providers.base import BaseProvider, ChatResponse, StreamChunk, aggregate_chunks
from .types import RequestContext, trace_id_var

logger = logging.getLogger("llm_provider_sdk.engine")


class AllProvidersExhausted(Exception):
    """池子里所有候选都失败了。"""


class FallbackEngine:

    def __init__(self, config: LLMStabilityConfig, adapters: Dict[str, BaseProvider]) -> None:
        self._config = config
        self._adapters = adapters
        self._pools: Dict[str, ModelPool] = {}   # business_key → 各自独立的池子，指标互不影响

    def update_config(self, config: LLMStabilityConfig, adapters: Dict[str, BaseProvider]) -> None:
        self._config = config
        self._adapters = adapters

    def _pool_for(self, business_key: str) -> ModelPool:
        """取这个 business_key 的池子，没有就新建。"""
        pool = self._pools.get(business_key)
        if pool is None:
            pool = self._pools[business_key] = ModelPool()
        return pool

    async def execute_chat(self, ctx: RequestContext) -> ChatResponse:
        """非流式：按池子顺序尝试，成功即返回，任何报错换下一个。"""
        trace_id_var.set(ctx.trace_id)
        began = time.monotonic()
        strategy = self._strategy(ctx)
        failures: List[str] = []

        for adapter in self._candidates(strategy, ctx, failures):
            start = time.monotonic()
            stats = self._pool_for(ctx.business_key).stats(ctx.pool_name)
            try:
                response = await asyncio.wait_for(
                    aggregate_chunks(self._stream(adapter, ctx, strategy, start, stats), ctx.actual_model_id),
                    timeout=strategy.timeout.per_request,
                )
                stats.record_result(failed=False)
                logger.info(f"[{ctx.business_key}] {ctx.pool_name} 成功 "
                            f"首token={stats.first_token_seconds:.1f}s 耗时={time.monotonic() - start:.0f}s "
                            f"总耗时={time.monotonic() - began:.0f}s tokens={response.usage.total_tokens}")
                return response
            except Exception as e:
                stats.record_result(failed=True)
                failures.append(f"{ctx.pool_name} [{type(e).__name__}] {e}")
                logger.warning(f"[{ctx.business_key}] {ctx.pool_name} 失败 "
                               f"耗时={time.monotonic() - start:.0f}s [{type(e).__name__}] {e}")

        logger.error(f"[{ctx.business_key}] 全部失败 总耗时={time.monotonic() - began:.0f}s")
        raise AllProvidersExhausted(f"[{ctx.business_key}] 全部失败: " + " | ".join(failures))

    async def execute_chat_stream(self, ctx: RequestContext) -> AsyncIterator[StreamChunk]:
        """流式：首帧前报错换下一个，已输出内容后报错不再切换。"""
        trace_id_var.set(ctx.trace_id)
        began = time.monotonic()
        strategy = self._strategy(ctx)
        failures: List[str] = []

        for adapter in self._candidates(strategy, ctx, failures):
            start = time.monotonic()
            stats = self._pool_for(ctx.business_key).stats(ctx.pool_name)
            sent_any = False
            try:
                async for chunk in self._stream(adapter, ctx, strategy, start, stats):
                    sent_any = sent_any or bool(chunk.content or chunk.tool_calls)
                    yield chunk
                stats.record_result(failed=False)
                logger.info(f"[{ctx.business_key}] {ctx.pool_name} 成功 "
                            f"首token={stats.first_token_seconds:.1f}s 耗时={time.monotonic() - start:.0f}s "
                            f"总耗时={time.monotonic() - began:.0f}s")
                return
            except Exception as e:
                stats.record_result(failed=True)
                failures.append(f"{ctx.pool_name} [{type(e).__name__}] {e}")
                logger.warning(f"[{ctx.business_key}] {ctx.pool_name} 失败 "
                               f"耗时={time.monotonic() - start:.0f}s [{type(e).__name__}] {e}")
                if sent_any:
                    return

        logger.error(f"[{ctx.business_key}] 全部失败 总耗时={time.monotonic() - began:.0f}s")
        raise AllProvidersExhausted(f"[{ctx.business_key}] 全部失败: " + " | ".join(failures))

    async def _stream(self, adapter: BaseProvider, ctx: RequestContext,
                      strategy: StrategyConfig, started_at: float, stats: ModelStats) -> AsyncIterator[StreamChunk]:
        """包一层：首帧等太久就当这个模型挂了，同时记下首 token 耗时。"""
        iterator = adapter.chat_stream(ctx.actual_model_id, ctx.messages, **self._request_kwargs(ctx)).__aiter__()
        try:
            first = await asyncio.wait_for(iterator.__anext__(), timeout=strategy.timeout.first_token)
        except StopAsyncIteration:
            return
        stats.record_first_token(time.monotonic() - started_at)
        yield first
        async for chunk in iterator:
            yield chunk

    def _strategy(self, ctx: RequestContext) -> StrategyConfig:
        strategy = self._config.strategies.get(ctx.business_key)
        if strategy is None:
            raise ValueError(f"Unknown business_key '{ctx.business_key}'")
        return strategy

    def _candidates(self, strategy: StrategyConfig, ctx: RequestContext, failures: List[str]):
        """按池子抽出的顺序 yield adapter 并填充 ctx，整条链只走同一种协议。"""
        pool = self._pool_for(ctx.business_key)
        options: Dict[str, tuple] = {}   # 候选名 → (模型名, 配置条目, adapter key)
        locked_endpoint = ""             # 第一个可用候选的协议，换协议历史消息传不过去
        for candidate in strategy.pool:
            model = candidate.model
            for entry in self._config.get_model_providers(model):
                key = adapter_key(entry.provider, entry.endpoint)
                if self._adapters.get(key) is None:
                    continue
                name = f"{key}/{entry.model_id or model}"
                if not locked_endpoint:
                    locked_endpoint = entry.endpoint
                elif entry.endpoint != locked_endpoint:
                    skip = f"{name} 跳过：协议 {entry.endpoint} 与 {locked_endpoint} 不同"
                    failures.append(skip)
                    logger.info(f"[{ctx.business_key}] {skip}")
                    continue
                options[name] = (model, entry, key)
                pool.stats(name, candidate.weight).weight = candidate.weight   # 配置改了权重要跟着变

        fast = strategy.timeout.fast_first_token
        order = pool.order(list(options), fast)
        logger.info(f"[{ctx.business_key}] 池子顺序: {' → '.join(order)}")
        for name in order:
            model, entry, key = options[name]
            ctx.canonical_model = model
            ctx.provider_name = key
            ctx.actual_model_id = entry.model_id or model
            ctx.pool_name = name
            yield self._adapters[key]

    def pool_snapshot(self, business_key: str) -> List[tuple]:
        """当前池子里每个候选的表现和命中概率，排查用。"""
        strategy = self._config.strategies.get(business_key)
        fast = strategy.timeout.fast_first_token if strategy else 2.0
        return self._pool_for(business_key).snapshot(fast)

    @staticmethod
    def _request_kwargs(ctx: RequestContext) -> Dict:
        return dict(max_tokens=ctx.max_tokens, temperature=ctx.temperature,
                    tools=ctx.tools, tool_choice=ctx.tool_choice, **ctx.extra_kwargs)
