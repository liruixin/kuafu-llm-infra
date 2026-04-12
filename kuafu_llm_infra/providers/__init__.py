"""
Providers - LLM SDK adapters.

Wraps OpenAI, Anthropic, and other LLM SDKs behind a
unified async interface. Provider types are self-registered
via the ``@register_provider`` decorator.
"""

from .base import BaseProvider, ChatMessage, ChatResponse, StreamChunk
from .registry import create_provider, register_provider, registered_types

# Import provider modules to trigger self-registration.
# Each module's @register_provider decorator runs at import time.
from . import openai_provider as _openai  # noqa: F401
from . import anthropic_provider as _anthropic  # noqa: F401
from . import google_provider as _google  # noqa: F401

__all__ = [
    "BaseProvider",
    "ChatMessage",
    "ChatResponse",
    "StreamChunk",
    "create_provider",
    "register_provider",
    "registered_types",
]
