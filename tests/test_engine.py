"""降级引擎主路径测试：用假 provider 驱动，不打真实网络。"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from llm_provider_sdk.config.schema import LLMStabilityConfig
from llm_provider_sdk.engine import AllProvidersExhausted, FallbackEngine
from llm_provider_sdk.pool import ModelPool
from llm_provider_sdk.providers.base import BaseProvider, StreamChunk, ToolCall, ToolCallFunction
from llm_provider_sdk.types import RequestContext, TokenUsage


class FakeProvider(BaseProvider):
    """按脚本吐帧的假 provider。frames 里的元素：str=正文帧，float=等待秒数，Exception=抛错。"""

    def __init__(self, frames: List[Any]) -> None:
        super().__init__("key")
        self.frames = frames
        self.calls = 0

    async def chat_stream(self, model: str, messages: List[Dict[str, Any]], **kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        for frame in self.frames:
            if isinstance(frame, Exception):
                raise frame
            if isinstance(frame, float):
                await asyncio.sleep(frame)
            elif isinstance(frame, str):
                yield StreamChunk(content=frame)
            elif isinstance(frame, StreamChunk):
                yield frame
        yield StreamChunk(finish_reason="stop", usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3))


def make_engine(primary: FakeProvider, fallback: Optional[FakeProvider] = None,
                per_request: float = 1.0, first_token: float = 5.0) -> FallbackEngine:
    config = LLMStabilityConfig(**{
        "providers": {
            "p1": {"api_key": "k", "endpoints": {"openai": {"base_url": "http://x"}}},
            "p2": {"api_key": "k", "endpoints": {"openai": {"base_url": "http://y"}}},
        },
        "models": {
            "m1": {"providers": [{"provider": "p1", "endpoint": "openai"}]},
            "m2": {"providers": [{"provider": "p2", "endpoint": "openai"}]},
        },
        "strategies": {
            # 权重悬殊，抽样顺序稳定是 m1 在前，测试才有确定结果
            "biz": {"pool": [{"model": "m1", "weight": 1000000.0}] + ([{"model": "m2"}] if fallback else []),
                    "timeout": {"per_request": per_request, "first_token": first_token}},
        },
    })
    adapters = {"p1:openai": primary}
    if fallback:
        adapters["p2:openai"] = fallback
    return FallbackEngine(config, adapters)


def ctx() -> RequestContext:
    return RequestContext(business_key="biz", messages=[{"role": "user", "content": "hi"}])


async def collect(stream: AsyncIterator[StreamChunk]) -> str:
    return "".join([c.content async for c in stream])


# ---------------------------------------------------------------- 非流式

async def test_chat_success():
    response = await make_engine(FakeProvider(["hel", "lo"])).execute_chat(ctx())
    assert response.content == "hello"
    assert response.usage.total_tokens == 3


async def test_chat_error_switches_to_fallback():
    primary, fallback = FakeProvider([RuntimeError("boom")]), FakeProvider(["ok"])
    assert (await make_engine(primary, fallback).execute_chat(ctx())).content == "ok"
    assert primary.calls == 1 and fallback.calls == 1


async def test_chat_timeout_switches_to_fallback():
    primary, fallback = FakeProvider([5.0, "late"]), FakeProvider(["ok"])
    assert (await make_engine(primary, fallback, per_request=0.2).execute_chat(ctx())).content == "ok"


async def test_chat_all_exhausted():
    with pytest.raises(AllProvidersExhausted):
        await make_engine(FakeProvider([RuntimeError("a")]), FakeProvider([RuntimeError("b")])).execute_chat(ctx())


async def test_chat_tool_calls_aggregated():
    frames = [
        StreamChunk(tool_calls=[ToolCall(id="c1", index=0, function=ToolCallFunction(name="f"))]),
        StreamChunk(tool_calls=[ToolCall(id="", index=0, function=ToolCallFunction(arguments='{"a":'))]),
        StreamChunk(tool_calls=[ToolCall(id="", index=0, function=ToolCallFunction(arguments='1}'))]),
    ]
    response = await make_engine(FakeProvider(frames)).execute_chat(ctx())
    assert response.tool_calls[0].function.name == "f"
    assert response.tool_calls[0].function.arguments == '{"a":1}'


async def test_first_token_timeout_switches_to_fallback():
    """首 token 太慢就立刻换下一个，不用等到整个请求超时。"""
    primary, fallback = FakeProvider([1.0, "late"]), FakeProvider(["ok"])
    engine = make_engine(primary, fallback, per_request=10.0, first_token=0.1)
    assert (await engine.execute_chat(ctx())).content == "ok"


async def test_pools_are_isolated_per_business_key():
    """同一个模型在两个 business_key 下各记各的，指标和权重互不影响。"""
    broken = FakeProvider([RuntimeError("boom")])
    config = LLMStabilityConfig(**{
        "providers": {"p1": {"api_key": "k", "endpoints": {"openai": {"base_url": "http://x"}}}},
        "models": {"m1": {"providers": [{"provider": "p1", "endpoint": "openai"}]}},
        "strategies": {"biz_a": {"pool": [{"model": "m1", "weight": 10.0}]},
                       "biz_b": {"pool": [{"model": "m1", "weight": 1.0}]}},
    })
    engine = FallbackEngine(config, {"p1:openai": broken})
    with pytest.raises(AllProvidersExhausted):
        await engine.execute_chat(RequestContext(business_key="biz_a", messages=[]))

    name, stats_a, _ = engine.pool_snapshot("biz_a")[0]
    assert stats_a.error_rate > 0 and stats_a.weight == 10.0
    assert engine.pool_snapshot("biz_b") == []        # biz_b 没跑过，池子还是空的


# ---------------------------------------------------------------- 降级池

def test_pool_prefers_high_weight():
    """权重高的明显更常被抽中。"""
    pool = ModelPool()
    pool.stats("high", 10.0)
    pool.stats("low", 1.0)
    first = [pool.order(["high", "low"], 2.0)[0] for _ in range(500)]
    assert first.count("high") > first.count("low") * 3


def test_pool_demotes_failing_model():
    """一直失败的候选，即使权重高也会被压到后面。"""
    pool = ModelPool()
    broken = pool.stats("broken", 10.0)
    good = pool.stats("good", 1.0)
    for _ in range(50):
        broken.record_result(failed=True)
        good.record_result(failed=False)
    first = [pool.order(["broken", "good"], 2.0)[0] for _ in range(500)]
    assert first.count("good") > first.count("broken") * 3


def test_pool_demotes_slow_model():
    """首 token 慢十倍的候选，命中概率明显低于快的。"""
    pool = ModelPool()
    pool.stats("slow", 1.0).record_first_token(20.0)
    pool.stats("fast", 1.0).record_first_token(1.0)
    first = [pool.order(["slow", "fast"], 2.0)[0] for _ in range(500)]
    assert first.count("fast") > first.count("slow") * 3


def test_pool_keeps_floor_score():
    """全都又慢又挂时保留权重 2% 的保底，不会彻底抽不中。"""
    pool = ModelPool()
    dead = pool.stats("dead", 4.0)
    for _ in range(200):
        dead.record_result(failed=True)
    assert dead.score(2.0) == pytest.approx(4.0 * 0.02)


# ---------------------------------------------------------------- 流式

async def test_stream_success():
    assert await collect(make_engine(FakeProvider(["a", "b"])).execute_chat_stream(ctx())) == "ab"


async def test_stream_error_before_content_switches():
    primary, fallback = FakeProvider([RuntimeError("boom")]), FakeProvider(["ok"])
    assert await collect(make_engine(primary, fallback).execute_chat_stream(ctx())) == "ok"


async def test_stream_error_after_content_does_not_switch():
    primary, fallback = FakeProvider(["partial", RuntimeError("mid")]), FakeProvider(["ok"])
    assert await collect(make_engine(primary, fallback).execute_chat_stream(ctx())) == "partial"
    assert fallback.calls == 0


async def test_stream_all_exhausted():
    with pytest.raises(AllProvidersExhausted):
        await collect(make_engine(FakeProvider([RuntimeError("a")]), FakeProvider([RuntimeError("b")])).execute_chat_stream(ctx()))


async def test_unknown_business_key():
    with pytest.raises(ValueError):
        await make_engine(FakeProvider([])).execute_chat(RequestContext(business_key="nope", messages=[]))
