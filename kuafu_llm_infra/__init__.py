"""
kuafu-llm-infra: LLM Infrastructure Layer for Model Stability

Provides unified LLM calling interface with built-in:
- Multi-provider fallback & degradation
- Real-time health monitoring & speed probing
- Metrics collection (Prometheus / in-memory)
- Alerting (Feishu / Webhook / custom channels)
- Configurable via YAML / dict / environment variable

Quick start::

    from kuafu_llm_infra import create_client

    client = create_client("llm_stability.yaml")

    # OpenAI-compatible streaming
    stream = await client.chat.completions.create(
        business_key="my_task",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )
    async for chunk in stream:
        print(chunk.content, end="")

    # Non-streaming
    resp = await client.chat.completions.create(
        business_key="my_task",
        messages=[{"role": "user", "content": "hello"}],
    )
    print(resp.content)
"""

__version__ = "0.2.3"

from .gateway import LLMClient, create_client
from .types import TokenUsage, RequestContext
from .logging_setup import setup_logging

__all__ = [
    "LLMClient",
    "create_client",
    "TokenUsage",
    "RequestContext",
    "setup_logging",
]
