"""
Google Gemini SDK provider adapter.

使用 google-genai SDK 调用 Gemini 模型。
"""

from __future__ import annotations

import asyncio
import json
import uuid
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from google import genai
from google.genai import types

from ..types import TokenUsage
from .base import BaseProvider, ChatResponse, StreamChunk, ToolCall, ToolCallFunction
from .registry import register_provider

logger = logging.getLogger("kuafu_llm_infra.providers.google")

# finish_reason 映射
_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "MALFORMED_FUNCTION_CALL": "tool_calls",
}


class GoogleProvider(BaseProvider):
    """Google Gemini provider adapter."""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        # AgentWorld's existing UI exposes ``google`` but not ``vertexai``.
        # Treat the Vertex API host as an explicit compatibility marker.  It
        # must *not* be forwarded as SDK base_url: Vertex Express uses the
        # SDK's global publishers/google route when authenticated by API key.
        if "aiplatform.googleapis.com" in (base_url or ""):
            http_options: Dict[str, Any] = {"api_version": "v1"}
            if extra_headers:
                http_options["headers"] = extra_headers
            self._client = genai.Client(
                vertexai=True,
                api_key=api_key,
                http_options=types.HttpOptions(**http_options),
            )
            return

        # ``google-genai`` transports endpoint overrides and request headers
        # through HttpOptions (rather than Client's top-level constructor).
        # Keeping this conditional preserves the SDK defaults for the normal
        # Gemini Developer API path, while allowing an approved API gateway or
        # proxy endpoint to use the same provider abstraction as OpenAI and
        # Anthropic.
        client_params: Dict[str, Any] = {"api_key": api_key}
        http_options: Dict[str, Any] = {}
        if base_url:
            http_options["base_url"] = base_url
        if extra_headers:
            http_options["headers"] = extra_headers
        if http_options:
            client_params["http_options"] = types.HttpOptions(**http_options)
        self._client = genai.Client(**client_params)

    @property
    def provider_type(self) -> str:
        return "google"

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------

    async def probe(
        self,
        model: str,
        *,
        max_tokens: int = 5,
        timeout: float = 10.0,
    ) -> AsyncIterator[StreamChunk]:
        """Gemini 探测：最小化流式请求。"""
        config = types.GenerateContentConfig(max_output_tokens=max_tokens)
        contents = [types.Content(
            role="user",
            parts=[types.Part(text="hi")],
        )]

        coro = self._client.aio.models.generate_content_stream(
            model=model, contents=contents, config=config,
        )
        stream = await asyncio.wait_for(coro, timeout=timeout)

        async for chunk in stream:
            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            if not candidate.content or not candidate.content.parts:
                continue
            for part in candidate.content.parts:
                if part.text:
                    yield StreamChunk(content=part.text, raw=chunk)

    # ------------------------------------------------------------------
    # 消息格式转换
    # ------------------------------------------------------------------

    @staticmethod
    def _text_content(content: Any) -> str:
        """Normalize OpenAI text content into Gemini's string-only Part.text.

        AgentWorld may send a normal text turn as an OpenAI content-block
        list, e.g. ``[{"type": "text", "text": "hello"}]``. Gemini's
        ``Part.text`` accepts a string only, so preserve text blocks and
        ignore non-text blocks that this provider cannot represent.
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)

        text_parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        return "\n".join(text_parts)

    @staticmethod
    def _convert_messages(
        messages: List[Dict[str, Any]],
    ) -> tuple[Optional[str], List[types.Content]]:
        """将 OpenAI 格式消息转换为 Gemini 格式。

        返回 (system_instruction, contents)。
        tool 角色的连续消息合并为一条 user Content（Gemini 规范）。
        """
        system_parts: List[str] = []
        contents: List[types.Content] = []

        # 先构建 tool_call_id → function_name 映射
        id_to_name: Dict[str, str] = {}
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    tc_id = tc.get("id", "")
                    name = func.get("name", "")
                    if tc_id and name:
                        id_to_name[tc_id] = name

        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")

            if role == "system":
                system_parts.append(GoogleProvider._text_content(msg.get("content")))
                i += 1

            elif role == "user":
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=GoogleProvider._text_content(msg.get("content")))],
                ))
                i += 1

            elif role == "assistant":
                parts: List[types.Part] = []
                content = GoogleProvider._text_content(msg.get("content"))
                if content:
                    parts.append(types.Part(text=content))

                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    args_str = func.get("arguments", "{}")
                    try:
                        args = json.loads(args_str)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    part_kwargs: Dict[str, Any] = {
                        "function_call": types.FunctionCall(
                            id=tc.get("id", ""),
                            name=func.get("name", ""),
                            args=args,
                        ),
                    }
                    # Gemini 3 / 2.5 thinking 模型要求回传 thought_signature，
                    # 业务侧把上一轮拿到的 signature 放在 tool_call dict 顶层即可。
                    sig = tc.get("thought_signature")
                    if sig is not None:
                        part_kwargs["thought_signature"] = sig
                    parts.append(types.Part(**part_kwargs))

                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                i += 1

            elif role == "tool":
                # 连续 tool 消息合并为一条 user Content
                tool_parts: List[types.Part] = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    t = messages[i]
                    tc_id = t.get("tool_call_id", "")
                    func_name = id_to_name.get(tc_id, "unknown")
                    raw_content = t.get("content", "")
                    try:
                        response_body = json.loads(raw_content)
                        if not isinstance(response_body, dict):
                            response_body = {"result": response_body}
                    except (json.JSONDecodeError, TypeError):
                        response_body = {"result": raw_content}

                    tool_parts.append(types.Part(
                        function_response=types.FunctionResponse(
                            id=tc_id,
                            name=func_name,
                            response=response_body,
                        ),
                    ))
                    i += 1

                contents.append(types.Content(role="user", parts=tool_parts))

            else:
                i += 1

        system = "\n\n".join(system_parts) if system_parts else None
        return system, contents

    @staticmethod
    def _convert_tools(
        tools: List[Dict[str, Any]],
    ) -> List[types.Tool]:
        """OpenAI 格式工具 → Gemini 格式。

        OpenAI: {"type": "function", "function": {"name": ..., "parameters": ...}}
        Gemini: types.FunctionDeclaration(name=..., parameters=...)
        """
        declarations = []
        for tool in tools:
            func = tool.get("function", {})
            declarations.append(types.FunctionDeclaration(
                name=func.get("name", ""),
                description=func.get("description", ""),
                parameters=func.get("parameters", {}),
            ))
        return [types.Tool(function_declarations=declarations)]

    @staticmethod
    def _convert_tool_choice(tool_choice: str) -> types.ToolConfig:
        """Convert OpenAI-format tool_choice to Gemini ToolConfig.

        OpenAI -> Gemini mapping:
        - ``"auto"`` -> ``AUTO``
        - ``"none"`` -> ``NONE``
        - ``"required"`` -> ``ANY``
        """
        mode_map = {
            "auto": "AUTO",
            "none": "NONE",
            "required": "ANY",
        }
        mode = mode_map.get(tool_choice, "AUTO")
        return types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=mode),
        )

    @staticmethod
    def _build_config(
        *,
        max_tokens: int,
        system: Optional[str],
        temperature: Optional[float],
        tools: Optional[List[Dict[str, Any]]],
        tool_choice: Optional[str],
        options: Dict[str, Any],
    ) -> types.GenerateContentConfig:
        """Build a Gemini request config from the unified API arguments.

        The public gateway deliberately has a small common surface.  Gemini
        capabilities which have no OpenAI equivalent (for example
        ``thinking_config``, ``response_mime_type`` or ``top_p``) are still
        useful and are passed through unchanged via ``**kwargs``.
        """
        config_params: Dict[str, Any] = dict(options)
        config_params["max_output_tokens"] = max_tokens
        if system is not None:
            config_params["system_instruction"] = system
        if temperature is not None:
            config_params["temperature"] = temperature
        if tools is not None:
            config_params["tools"] = GoogleProvider._convert_tools(tools)
        if tool_choice is not None:
            config_params["tool_config"] = GoogleProvider._convert_tool_choice(tool_choice)
        return types.GenerateContentConfig(**config_params)

    @staticmethod
    def _extract_usage(usage_meta) -> TokenUsage:
        """从 usage_metadata 提取 TokenUsage。"""
        if not usage_meta:
            return TokenUsage()
        prompt = getattr(usage_meta, "prompt_token_count", 0) or 0
        completion = getattr(usage_meta, "candidates_token_count", 0) or 0
        total = getattr(usage_meta, "total_token_count", 0) or 0
        cached = getattr(usage_meta, "cached_content_token_count", 0) or 0
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cached_tokens=cached,
        )

    @staticmethod
    def _map_finish_reason(candidate) -> str:
        """将 Gemini finish_reason 映射为标准字符串。"""
        fr = getattr(candidate, "finish_reason", None)
        if fr is None:
            return "stop"
        fr_str = str(fr).split(".")[-1]
        return _FINISH_REASON_MAP.get(fr_str, "stop")

    # ------------------------------------------------------------------
    # 非流式
    # ------------------------------------------------------------------

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
        system, contents = self._convert_messages(messages)

        config = self._build_config(
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            options=kwargs,
        )

        coro = self._client.aio.models.generate_content(
            model=model, contents=contents, config=config,
        )
        if timeout is not None:
            resp = await asyncio.wait_for(coro, timeout=timeout)
        else:
            resp = await coro

        if not resp.candidates:
            raise ValueError("Gemini returned empty candidate list")

        candidate = resp.candidates[0]
        content = ""
        reasoning = ""
        tool_calls: List[ToolCall] = []

        if candidate.content and candidate.content.parts:
            for part in candidate.content.parts:
                if part.text:
                    if getattr(part, "thought", False):
                        reasoning += part.text
                    else:
                        content += part.text
                if part.function_call:
                    fc = part.function_call
                    tool_calls.append(ToolCall(
                        id=getattr(fc, "id", None) or str(uuid.uuid4()),
                        type="function",
                        function=ToolCallFunction(
                            name=fc.name,
                            arguments=json.dumps(
                                dict(fc.args) if fc.args else {},
                            ),
                        ),
                        thought_signature=getattr(part, "thought_signature", None),
                    ))

        usage = self._extract_usage(resp.usage_metadata)
        finish_reason = self._map_finish_reason(candidate)

        return ChatResponse(
            content=content,
            reasoning_content=reasoning,
            model=model,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            raw=resp,
        )

    # ------------------------------------------------------------------
    # 流式
    # ------------------------------------------------------------------

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
        system, contents = self._convert_messages(messages)

        config = self._build_config(
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            options=kwargs,
        )

        coro = self._client.aio.models.generate_content_stream(
            model=model, contents=contents, config=config,
        )
        if timeout is not None:
            stream = await asyncio.wait_for(coro, timeout=timeout)
        else:
            stream = await coro

        async for chunk in stream:
            if not chunk.candidates:
                # 最后一个 chunk 可能只有 usage_metadata
                if chunk.usage_metadata:
                    yield StreamChunk(
                        content="",
                        usage=self._extract_usage(chunk.usage_metadata),
                        raw=chunk,
                    )
                continue

            candidate = chunk.candidates[0]
            usage = self._extract_usage(chunk.usage_metadata)

            # Gemini can send a terminal chunk with a finish reason but no
            # content parts.  Emit it before handling content so callers do
            # not miss ``length``/``content_filter`` completion states.
            if candidate.finish_reason:
                yield StreamChunk(
                    content="",
                    finish_reason=self._map_finish_reason(candidate),
                    usage=usage if usage.total_tokens > 0 else None,
                    raw=chunk,
                )

            if not candidate.content or not candidate.content.parts:
                continue

            for part in candidate.content.parts:
                if part.text:
                    is_thinking = bool(getattr(part, "thought", False))
                    yield StreamChunk(
                        content=part.text,
                        thinking=is_thinking,
                        reasoning_content=part.text if is_thinking else "",
                        raw=chunk,
                    )

                if part.function_call:
                    fc = part.function_call
                    tc_id = getattr(fc, "id", None) or str(uuid.uuid4())
                    yield StreamChunk(
                        content="",
                        tool_calls=[ToolCall(
                            id=tc_id,
                            type="function",
                            function=ToolCallFunction(
                                name=fc.name,
                                arguments=json.dumps(
                                    dict(fc.args) if fc.args else {},
                                ),
                            ),
                            thought_signature=getattr(part, "thought_signature", None),
                        )],
                        raw=chunk,
                    )


class VertexAIProvider(GoogleProvider):
    """Vertex AI Gemini adapter using the official Google Gen AI SDK.

    Standard Vertex deployments authenticate with Application Default
    Credentials (ADC). Vertex AI Express may instead supply an API key.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        import os

        client_params: Dict[str, Any] = {"vertexai": True}
        if api_key:
            client_params["api_key"] = api_key

        # Vertex Express API keys use the global endpoint and do not need a
        # project/location. Standard Vertex uses ADC plus these variables.
        if not api_key:
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            location = os.environ.get("GOOGLE_CLOUD_LOCATION")
            if project:
                client_params["project"] = project
            if location:
                client_params["location"] = location

        # Vertex uses the stable API. The SDK determines its regional endpoint
        # from project/location; base_url is only for an approved custom proxy.
        http_options: Dict[str, Any] = {"api_version": "v1"}
        if base_url:
            http_options["base_url"] = base_url
        if extra_headers:
            http_options["headers"] = extra_headers
        client_params["http_options"] = types.HttpOptions(**http_options)
        self._client = genai.Client(**client_params)

    @property
    def provider_type(self) -> str:
        return "vertexai"


@register_provider("google")
def _create_google(
    api_key: str, base_url: str, extra_headers: dict | None = None,
) -> GoogleProvider:
    return GoogleProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)


@register_provider("vertexai")
def _create_vertexai(
    api_key: str, base_url: str, extra_headers: dict | None = None,
) -> VertexAIProvider:
    return VertexAIProvider(api_key=api_key, base_url=base_url, extra_headers=extra_headers)
