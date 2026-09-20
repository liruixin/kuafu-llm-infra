"""Provider 适配器：把四种协议统一成 BaseProvider 接口。"""

from typing import Dict, Optional, Type

from .base import BaseProvider, ChatResponse, StreamChunk, ToolCall, ToolCallFunction
from .openai_provider import OpenAIProvider
from .openai_responses_provider import OpenAIResponsesProvider
from .anthropic_provider import AnthropicProvider
from .google_provider import GoogleProvider

# endpoint 类型名（YAML 里 endpoints 的 key）→ 适配器类
PROVIDER_CLASSES: Dict[str, Type[BaseProvider]] = {
    "openai": OpenAIProvider,
    "openai_responses": OpenAIResponsesProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
}


def create_provider(provider_type: str, api_key: str, base_url: str,
                    extra_headers: Optional[Dict[str, str]] = None) -> BaseProvider:
    cls = PROVIDER_CLASSES.get(provider_type)
    if cls is None:
        raise ValueError(f"未知的 endpoint 类型 '{provider_type}'，可用: {sorted(PROVIDER_CLASSES)}")
    return cls(api_key=api_key, base_url=base_url, extra_headers=extra_headers)


__all__ = [
    "BaseProvider", "ChatResponse", "StreamChunk", "ToolCall", "ToolCallFunction",
    "PROVIDER_CLASSES", "create_provider",
]
