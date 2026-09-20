"""日志配置：库日志写到 log_dir 下的文件，不冒泡到宿主 app 的 root logger。"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from .types import trace_id_var

_INITIALIZED = False


class _TraceIdFilter(logging.Filter):
    """给每条日志补上当前请求的 trace_id。"""
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        return True


def setup_logging(log_dir: str = "kuafu-llm-infra-log", *, file_level: int = logging.DEBUG,
                  max_bytes: int = 50 * 1024 * 1024, backup_count: int = 5) -> None:
    """重复调用安全，首次生效。"""
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True
    os.makedirs(log_dir, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(trace_id)s] %(name)s - %(message)s")

    def file_handler(filename: str) -> RotatingFileHandler:
        handler = RotatingFileHandler(os.path.join(log_dir, filename), maxBytes=max_bytes,
                                      backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.setLevel(file_level)
        handler.addFilter(_TraceIdFilter())
        return handler

    lib_logger = logging.getLogger("kuafu_llm_infra")
    lib_logger.setLevel(file_level)
    lib_logger.propagate = False
    lib_logger.addHandler(file_handler("infra.log"))

    # 第三方 SDK / HTTP 库单独一个文件
    third_party = file_handler("third-party.log")
    for name in ("openai", "anthropic", "httpx", "httpcore"):
        lg = logging.getLogger(name)
        lg.setLevel(file_level)
        lg.addHandler(third_party)
        lg.propagate = False
