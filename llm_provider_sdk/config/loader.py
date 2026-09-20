"""配置加载：YAML 文件 / dict / LLM_STABILITY_CONFIG 环境变量指向的文件。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Union

import yaml

from .schema import LLMStabilityConfig


def load_config(source: Union[str, Path, Dict[str, Any], None] = None) -> LLMStabilityConfig:
    if source is None:
        source = os.environ.get("LLM_STABILITY_CONFIG")
        if not source:
            raise ValueError("No config source provided and LLM_STABILITY_CONFIG env var not set")

    if not isinstance(source, dict):
        with open(source, "r", encoding="utf-8") as f:
            source = yaml.safe_load(f)

    config = LLMStabilityConfig(**source["llm_stability"])
    config.validate_references()
    return config
