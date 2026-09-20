"""
对外入口：OpenAI 兼容的 client.chat.completions.create()，内部按配置做提供商降级。

    client = create_client("llm_stability.yaml")
    response = await client.chat.completions.create(
        business_key="chat", messages=[{"role": "user", "content": "hi"}],
    )
    print(response.choices[0].message.content)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from .config.loader import load_config
from .config.schema import LLMStabilityConfig, adapter_key
from .config.store import RedisConfigStore
from .engine import FallbackEngine
from .providers import create_provider
from .providers.base import BaseProvider, ChatResponse, StreamChunk, ToolCall
from .types import RequestContext, trace_id_var

logger = logging.getLogger("llm_provider_sdk.gateway")


# ============================================================================
# OpenAI 兼容的响应包装
# ============================================================================

class _Message:
    def __init__(self, content: str, tool_calls: Optional[List[ToolCall]], reasoning_content: str) -> None:
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _Choice:
    def __init__(self, message: _Message, finish_reason: Optional[str]) -> None:
        self.index = 0
        self.message = message
        self.finish_reason = finish_reason


class _Delta:
    def __init__(self, content: Optional[str], tool_calls: Optional[List[ToolCall]]) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _StreamChoice:
    def __init__(self, delta: _Delta, finish_reason: Optional[str]) -> None:
        self.index = 0
        self.delta = delta
        self.finish_reason = finish_reason


class _CompletionResponse:
    """对应 OpenAI 的 ChatCompletion：choices[0].message.{content,tool_calls,reasoning_content}。"""
    def __init__(self, r: ChatResponse, ctx: RequestContext, trace_id: str = "") -> None:
        self.choices = [_Choice(_Message(r.content, r.tool_calls, r.reasoning_content), r.finish_reason)]
        self.model = r.model                          # 厂商实际的模型 id
        self.model_name = ctx.canonical_model         # 配置 strategies.pool 里的模型名
        self.provider_name = ctx.provider_name        # 实际走的 provider，形如 "openai-next:openai"
        self.usage = r.usage
        self.raw = r.raw
        self.trace_id = trace_id


class _StreamChunkWrapper:
    """对应 OpenAI 的 ChatCompletionChunk：choices[0].delta.{content,tool_calls}。"""
    def __init__(self, c: StreamChunk) -> None:
        self.choices = [_StreamChoice(_Delta(c.content or None, c.tool_calls), c.finish_reason)]
        self.usage = c.usage
        self.raw = c.raw
        self.thinking = c.thinking
        self.reasoning_content = c.reasoning_content


class _StreamWrapper:
    def __init__(self, aiter: AsyncIterator[StreamChunk], ctx: RequestContext, trace_id: str = "") -> None:
        self._aiter = aiter
        self._ctx = ctx
        self.trace_id = trace_id

    @property
    def model(self) -> str:
        """厂商实际的模型 id，收到第一帧后才有值。"""
        return self._ctx.actual_model_id

    @property
    def model_name(self) -> str:
        """配置 strategies.pool 里的模型名，收到第一帧后才有值。"""
        return self._ctx.canonical_model

    @property
    def provider_name(self) -> str:
        """实际走的 provider，收到第一帧后才有值。"""
        return self._ctx.provider_name

    def __aiter__(self):
        return self

    async def __anext__(self) -> _StreamChunkWrapper:
        return _StreamChunkWrapper(await self._aiter.__anext__())


class _Completions:
    """模拟 client.chat.completions。"""

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def create(
        self,
        *,
        messages: List[Dict[str, Any]],
        stream: bool = False,
        business_key: str = "default",
        labels: Optional[Dict[str, str]] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        trace_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Union[_CompletionResponse, _StreamWrapper]:
        if not self._client._config_loaded:
            raise RuntimeError("Config not loaded from Redis yet")
        trace_id = trace_id or uuid.uuid4().hex[:12]   # 调用方可自带，便于和业务日志串起来
        trace_id_var.set(trace_id)
        ctx = RequestContext(
            business_key=business_key, messages=messages, labels=labels or {},
            max_tokens=max_tokens, temperature=temperature,
            tools=tools, tool_choice=tool_choice, extra_kwargs=kwargs, trace_id=trace_id,
        )
        logger.info(f"请求进入 business_key={business_key} stream={stream} "
                    f"消息数={len(messages)} 工具数={len(tools or [])} labels={ctx.labels}")
        if stream:
            return _StreamWrapper(self._client._engine.execute_chat_stream(ctx), ctx, trace_id)
        return _CompletionResponse(await self._client._engine.execute_chat(ctx), ctx, trace_id)


class _Chat:
    def __init__(self, client: LLMClient) -> None:
        self.completions = _Completions(client)


# ============================================================================
# LLMClient
# ============================================================================

class LLMClient:

    def __init__(self, config: LLMStabilityConfig, *, store: Optional[RedisConfigStore] = None) -> None:
        self._config = config
        self._store = store                      # 有则从 Redis 拉配置
        self._config_loaded = store is None      # Redis 模式下首次拉到配置前为 False
        self._last_config_raw: Optional[str] = None
        self._adapters: Dict[str, BaseProvider] = {}
        self._engine = FallbackEngine(config, self._adapters)
        self._pull_task: Optional[asyncio.Task] = None
        self.chat = _Chat(self)
        if self._config_loaded:
            self._apply_config(config)

    def start(self) -> None:
        if self._store and self._pull_task is None:
            self._pull_task = asyncio.create_task(self._config_pull_loop())

    async def shutdown(self) -> None:
        if self._pull_task:
            self._pull_task.cancel()
            self._pull_task = None

    async def push_config(self, new_config: Union[Dict[str, Any], LLMStabilityConfig]) -> None:
        """写入新配置到 Redis 并在本实例立即生效，其他实例下个 pull 周期生效。"""
        if isinstance(new_config, dict):
            new_config = LLMStabilityConfig(**new_config["llm_stability"])
        new_config.validate_references()
        config_json = json.dumps({"llm_stability": new_config.model_dump()}, ensure_ascii=False)
        if self._store:
            await self._store.save(config_json)
        self._last_config_raw = config_json
        self._apply_config(new_config)

    async def _config_pull_loop(self, interval: float = 10.0) -> None:
        while True:
            try:
                raw = await self._store.load()
                if raw and raw != self._last_config_raw:
                    data = json.loads(raw)
                    new_config = LLMStabilityConfig(**data["llm_stability"])
                    new_config.validate_references()
                    self._apply_config(new_config)
                    self._last_config_raw = raw
                    self._config_loaded = True
                    logger.info("Config loaded from Redis")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Config pull failed: {e}")
            await asyncio.sleep(interval)

    def _apply_config(self, config: LLMStabilityConfig) -> None:
        """应用配置：base_url / api_key 变了的适配器重建，没变的复用。"""
        self._config = config
        wanted: Dict[str, tuple] = {}
        for name, provider_cfg in config.providers.items():
            if provider_cfg.enabled:
                for endpoint_type, endpoint_cfg in provider_cfg.endpoints.items():
                    if endpoint_cfg.base_url:
                        wanted[adapter_key(name, endpoint_type)] = (endpoint_type, provider_cfg.api_key, endpoint_cfg.base_url)
        for key in list(self._adapters):
            if key not in wanted:
                del self._adapters[key]
        for key, (endpoint_type, api_key, base_url) in wanted.items():
            old = self._adapters.get(key)
            if old is None or (old._api_key, old._base_url) != (api_key, base_url):
                self._adapters[key] = create_provider(endpoint_type, api_key, base_url,
                                                      extra_headers={"Authorization": f"Bearer {api_key}"})
        self._engine.update_config(config, self._adapters)
        logger.info(f"Configuration applied: adapters={sorted(self._adapters)}")


def create_client(
    source: Union[str, Path, Dict[str, Any], None] = None,
    *,
    redis_url: Optional[str] = None,
    key_prefix: str = "kuafu_llm_infra:",
) -> LLMClient:
    """
    本地配置：create_client("llm_stability.yaml")
    Redis 配置：create_client(redis_url="redis://...")，立即返回，配置在后台加载，加载前请求抛 RuntimeError。
    """
    if redis_url:
        client = LLMClient(LLMStabilityConfig(), store=RedisConfigStore(redis_url, key_prefix))
    else:
        client = LLMClient(load_config(source))
    client.start()
    return client
