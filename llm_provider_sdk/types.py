"""贯穿各层的核心数据结构。"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 当前请求的 trace_id，日志 formatter 直接取，不用层层传参
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """单次请求的 token 消耗。"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class RequestContext:
    """一次请求的全部参数，由 gateway 创建，engine 在选中 provider 后填充后三个字段。"""
    business_key: str
    messages: List[Dict[str, Any]]
    trace_id: str = ""            # 全链路追踪 id，日志里每行都带
    labels: Dict[str, str] = field(default_factory=dict)
    max_tokens: int = 4096
    temperature: Optional[float] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[str] = None
    extra_kwargs: Dict[str, Any] = field(default_factory=dict)

    # engine 选中 provider 后填充
    canonical_model: str = ""      # 配置里的模型名
    provider_name: str = ""        # adapter key，形如 "ppio:openai"
    actual_model_id: str = ""
    pool_name: str = ""            # 降级池里的候选名，请求结束后按它记录表现      # 该 provider 实际使用的模型 id
