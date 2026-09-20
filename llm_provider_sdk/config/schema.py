"""配置结构。"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class EndpointConfig(BaseModel):
    base_url: str = ""


class ProviderConfig(BaseModel):
    """一个提供商的凭证，可暴露多个协议端点（openai / openai_responses / anthropic / google）。"""
    api_key: str = ""
    endpoints: Dict[str, EndpointConfig] = Field(default_factory=dict)
    enabled: bool = True


def adapter_key(provider: str, endpoint: str) -> str:
    """适配器 key，形如 'ppio:openai'。"""
    return f"{provider}:{endpoint}"


class ModelProviderEntry(BaseModel):
    """模型下的一个提供商条目。"""
    provider: str
    endpoint: str
    model_id: Optional[str] = None   # 该提供商实际的模型 id，None 则同模型名


class ModelConfig(BaseModel):
    providers: List[ModelProviderEntry] = Field(default_factory=list)


class TimeoutConfig(BaseModel):
    per_request: float = 60.0        # 单次请求总超时（秒）
    first_token: float = 5.0         # 首 token 超过这么久就换下一个（秒）
    fast_first_token: float = 2.0    # 首 token 快过这么久算满分，用来算命中概率（秒）


class PoolEntry(BaseModel):
    """降级池里的一个候选模型。"""
    model: str
    weight: float = 1.0              # 越大越想命中


class StrategyConfig(BaseModel):
    """business_key 级策略：一个降级池。"""
    pool: List[PoolEntry] = Field(default_factory=list)
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)


class LLMStabilityConfig(BaseModel):
    providers: Dict[str, ProviderConfig] = Field(default_factory=dict)
    models: Dict[str, ModelConfig] = Field(default_factory=dict)
    strategies: Dict[str, StrategyConfig] = Field(default_factory=dict)

    def get_model_providers(self, model: str) -> List[ModelProviderEntry]:
        model_cfg = self.models.get(model)
        return model_cfg.providers if model_cfg else []

    def validate_references(self) -> None:
        """检查 models → providers/endpoints、strategies → models 的引用是否都存在。"""
        errors: List[str] = []
        for model_name, model_cfg in self.models.items():
            for entry in model_cfg.providers:
                provider = self.providers.get(entry.provider)
                if provider is None:
                    errors.append(f"Model '{model_name}' references unknown provider '{entry.provider}'")
                elif entry.endpoint not in provider.endpoints:
                    errors.append(f"Model '{model_name}' references unknown endpoint '{entry.endpoint}' on '{entry.provider}'")
        for key, strategy in self.strategies.items():
            for candidate in strategy.pool:
                if candidate.model not in self.models:
                    errors.append(f"Strategy '{key}' references unknown model '{candidate.model}'")
        if errors:
            raise ValueError("Config reference validation failed:\n  - " + "\n  - ".join(errors))
