"""
日志配置工具。

将库产生的日志按职责分文件输出到 ``log_dir`` 下，**完全隔离**宿主 app：
- infra.log       — 兜底：gateway / providers / metrics / state / config 等
- request.log     — 请求链路：engine / stream_monitor / scorer / recorder
- probe.log       — 后台探测与告警：health_checker / alert
- third-party.log — 第三方 SDK：openai / anthropic / httpx / httpcore

不写控制台、不冒泡到 root logger，调用一次 ``setup_logging()`` 即可。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_INITIALIZED = False


def setup_logging(
    log_dir: str = "kuafu-llm-infra-log",
    *,
    file_level: int = logging.DEBUG,
    max_bytes: int = 50 * 1024 * 1024,  # 50MB
    backup_count: int = 5,
) -> None:
    """
    把 kuafu-llm-infra 的所有日志引流到 ``log_dir`` 下的分文件。

    重复调用安全（首次生效，后续 no-op）。不会添加控制台 handler，
    不会冒泡到 root logger，宿主 app 的日志系统完全不受影响。

    Args:
        log_dir: 日志目录，相对工作目录。默认 ``./kuafu-llm-infra-log``。
        file_level: 文件输出级别（默认 DEBUG）。
        max_bytes: 单个日志文件最大字节数。
        backup_count: 轮转保留文件数。
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    def _file_handler(filename: str) -> RotatingFileHandler:
        h = RotatingFileHandler(
            os.path.join(log_dir, filename),
            maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
        )
        h.setFormatter(fmt)
        h.setLevel(file_level)
        return h

    # 顶层 logger：兜底写 infra.log，阻断冒泡到 root。
    lib_root = logging.getLogger("kuafu_llm_infra")
    lib_root.setLevel(file_level)
    lib_root.propagate = False
    lib_root.addHandler(_file_handler("infra.log"))

    # request.log — 请求链路
    request_handler = _file_handler("request.log")
    for name in (
        "kuafu_llm_infra.engine",
        "kuafu_llm_infra.stream_monitor",
        "kuafu_llm_infra.scorer",
        "kuafu_llm_infra.recorder",
    ):
        lg = logging.getLogger(name)
        lg.addHandler(request_handler)
        lg.propagate = False  # 避免同一条日志同时写到 infra.log

    # probe.log — 后台探测与告警
    probe_handler = _file_handler("probe.log")
    for name in (
        "kuafu_llm_infra.health_checker",
        "kuafu_llm_infra.alert",
    ):
        lg = logging.getLogger(name)
        lg.addHandler(probe_handler)
        lg.propagate = False

    # third-party.log — SDK / HTTP 库
    tp_handler = _file_handler("third-party.log")
    for name in ("openai", "anthropic", "httpx", "httpcore"):
        lg = logging.getLogger(name)
        lg.setLevel(file_level)
        lg.addHandler(tp_handler)
        lg.propagate = False
