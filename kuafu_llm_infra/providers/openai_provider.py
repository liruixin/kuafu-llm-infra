"""
OpenAI SDK provider adapter.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from ..types import TokenUsage
from .base import BaseProvider, ChatResponse, StreamChunk, ToolCall, ToolCallFunction
from .registry import register_provider


class OpenAIProvider(BaseProvider):
    """Provider adapter for the OpenAI API (and compatible endpoints)."""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
        )

    @property
    def provider_type(self) -> str:
        return "openai"

    # 匹配 <think>...</think> 标签（MiniMax、DeepSeek 等模型会在内容中输出思考过程）
    _THINK_RE = re.compile(r"<think>[\s\S]*?</think>\s*", re.DOTALL)

    async def probe(
        self,
        model: str,
        *,
        max_tokens: int = 5,
        timeout: float = 10.0,
    ) -> AsyncIterator[StreamChunk]:
        """OpenAI 探测：原始流式请求，不做 <think> 剥离（探测只关心是否有响应）。"""
        coro = self._client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=max_tokens,
            stream=True,
        )
        if timeout is not None:
            stream = await asyncio.wait_for(coro, timeout=timeout)
        else:
            stream = await coro

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text = delta.content or ""
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if text or reasoning:
                yield StreamChunk(content=text or reasoning, raw=chunk)

    async def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        params: Dict[str, Any] = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stream=False,
            **kwargs,
        )
        if temperature is not None:
            params["temperature"] = temperature
        if tools is not None:
            params["tools"] = tools
        if tool_choice is not None:
            params["tool_choice"] = tool_choice

        coro = self._client.chat.completions.create(**params)
        if timeout is not None:
            resp = await asyncio.wait_for(coro, timeout=timeout)
        else:
            resp = await coro

        choice = resp.choices[0]
        usage = TokenUsage()
        if resp.usage:
            cached = 0
            if hasattr(resp.usage, "prompt_tokens_details") and resp.usage.prompt_tokens_details:
                cached = getattr(resp.usage.prompt_tokens_details, "cached_tokens", 0) or 0
            usage = TokenUsage(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
                total_tokens=resp.usage.total_tokens,
                cached_tokens=cached,
            )

        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    type=tc.type or "function",
                    function=ToolCallFunction(
                        name=tc.function.name or "",
                        arguments=tc.function.arguments or "",
                    ),
                )
                for tc in choice.message.tool_calls
            ]

        raw_content = choice.message.content or ""

        # 提取思考内容：两种格式
        # 1) 独立字段 reasoning_content（DeepSeek 风格）
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
        # 2) <think> 标签嵌在 content 里（MiniMax、开源模型风格）
        if not reasoning:
            think_matches = self._THINK_RE.findall(raw_content)
            if think_matches:
                reasoning = "\n".join(
                    m.removeprefix("<think>").removesuffix("</think>").strip()
                    for m in think_matches
                )
        clean_content = self._THINK_RE.sub("", raw_content).strip()

        return ChatResponse(
            content=clean_content,
            reasoning_content=reasoning,
            model=resp.model,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            tool_calls=tool_calls,
            raw=resp,
        )

    async def chat_stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        params: Dict[str, Any] = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        if temperature is not None:
            params["temperature"] = temperature
        if tools is not None:
            params["tools"] = tools
        if tool_choice is not None:
            params["tool_choice"] = tool_choice

        coro = self._client.chat.completions.create(**params)
        if timeout is not None:
            stream = await asyncio.wait_for(coro, timeout=timeout)
        else:
            stream = await coro

        # 流式剥离 <think>...</think>：缓冲思考内容，不输出给调用方
        in_think = False

        async for chunk in stream:
            usage = None
            if hasattr(chunk, "usage") and chunk.usage is not None:
                cached = 0
                if hasattr(chunk.usage, "prompt_tokens_details") and chunk.usage.prompt_tokens_details:
                    cached = getattr(chunk.usage.prompt_tokens_details, "cached_tokens", 0) or 0
                usage = TokenUsage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    total_tokens=chunk.usage.total_tokens,
                    cached_tokens=cached,
                )

            if not chunk.choices:
                if usage:
                    yield StreamChunk(content="", usage=usage, raw=chunk)
                continue

            delta = chunk.choices[0].delta
            text = delta.content or ""

            # 处理 reasoning_content 字段（DeepSeek 等模型通过独立字段返回思考内容）
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if not text and reasoning:
                is_thinking_frame = True
            else:
                is_thinking_frame = False

            # 处理 <think> 标签：跳过思考内容（MiniMax、开源模型等在 content 里嵌标签）
            if text:
                if in_think:
                    # 正在思考块内，检查是否遇到 </think>
                    if "</think>" in text:
                        # 思考结束，只保留 </think> 之后的内容
                        text = text.split("</think>", 1)[1]
                        in_think = False
                        if not text.strip():
                            text = ""
                    else:
                        text = ""
                        is_thinking_frame = True
                elif "<think>" in text:
                    # 进入思考块
                    before = text.split("<think>", 1)[0]
                    after_tag = text.split("<think>", 1)[1]
                    if "</think>" in after_tag:
                        # 同一 chunk 内闭合
                        text = before + after_tag.split("</think>", 1)[1]
                    else:
                        text = before
                        in_think = True
                        if not text:
                            is_thinking_frame = True
            elif in_think:
                # 原始 content 为空但处于思考阶段，也标记
                is_thinking_frame = True

            delta_tool_calls = None
            if hasattr(delta, "tool_calls") and delta.tool_calls:
                delta_tool_calls = [
                    ToolCall(
                        id=tc.id or "",
                        type=tc.type or "function",
                        function=ToolCallFunction(
                            name=tc.function.name if tc.function and tc.function.name else "",
                            arguments=tc.function.arguments if tc.function and tc.function.arguments else "",
                        ),
                        index=tc.index,
                    )
                    for tc in delta.tool_calls
                ]

            # 有实际内容、tool_calls、usage、finish_reason 或思考帧时 yield
            if text or delta_tool_calls or usage or chunk.choices[0].finish_reason or is_thinking_frame:
                yield StreamChunk(
                    content=text,
                    finish_reason=chunk.choices[0].finish_reason,
                    usage=usage,
                    tool_calls=delta_tool_calls,
                    raw=chunk,
                    thinking=is_thinking_frame,
                )


@register_provider("openai")
def _create_openai(
    api_key: str, base_url: str, extra_headers: dict | None = None,
) -> OpenAIProvider:
    return OpenAIProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)
