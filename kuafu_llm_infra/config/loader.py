"""
Configuration loader.

Loads LLMStabilityConfig from multiple sources:
- YAML file
- Python dict
- Environment variable pointing to a file
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml
from pydantic import ValidationError

from .schema import LLMStabilityConfig

logger = logging.getLogger("kuafu_llm_infra.config")

_ENV_VAR = "LLM_STABILITY_CONFIG"


def load_config(
    source: Union[str, Path, Dict[str, Any], None] = None,
) -> LLMStabilityConfig:
    """
    Load configuration from the given source.

    Args:
        source: One of:
            - Path to a YAML file (str or Path)
            - A Python dict with config data
            - None → reads from LLM_STABILITY_CONFIG env var

    Returns:
        Validated LLMStabilityConfig instance.
    """
    if source is None:
        env_path = os.environ.get(_ENV_VAR)
        if env_path:
            source = env_path
        else:
            raise ValueError(
                f"No config source provided and {_ENV_VAR} env var not set"
            )

    if isinstance(source, dict):
        return _parse_dict(source)

    # File path
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    return _parse_file(path)


def _parse_file(path: Path) -> LLMStabilityConfig:
    """Parse a YAML config file."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a YAML mapping, got {type(raw)}")

    # Support both top-level and nested under 'llm_stability'
    if "llm_stability" in raw:
        raw = raw["llm_stability"]

    return _parse_dict(raw)


def _parse_dict(data: Dict[str, Any]) -> LLMStabilityConfig:
    """Parse a config dict into a validated LLMStabilityConfig."""
    try:
        config = LLMStabilityConfig(**data)
        config.validate_references()
        return config
    except ValidationError as e:
        logger.error(f"Config validation failed:\n{e}")
        raise
