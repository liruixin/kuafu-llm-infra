"""
日志配置工具。

业务侧只需 `setup_logging()` 一行即可完成全部配置，重复调用安全（内部幂等）。

将库产生的日志按职责分文件输出：
- request.log      — 请求链路（engine / stream_monitor / scorer / recorder / recording.*）
- probe.log        — 后台探测（health_checker / alert）
- third-party.log  — 第三方 SDK（openai / anthropic / httpx / httpcore）
- all.log          — 库内所有 logger 的 catch-all 兜底（不含 probe / third-party）

控制台只输出 INFO+ 的库日志，第三方库日志不打到控制台。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


_INITIALIZED = False


def setup_logging(
    log_dir: str = "kuafu-llm-infra-log",
    *,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    max_bytes: int = 50 * 1024 * 1024,  # 50MB
    backup_count: int = 5,
) -> None:
    """
    配置 kuafu-llm-infra 日志分文件输出。

    幂等：重复调用直接 return，避免 handler 叠加。

    Args:
        log_dir: 日志目录，相对工作目录或绝对路径。
        console_level: 控制台输出级别（默认 INFO）。
        file_level: 文件输出级别（默认 DEBUG）。
        max_bytes: 单个日志文件最大字节数。
        backup_count: 轮转保留文件数。
    """
    global _INITIALIZED
    if _INITIALIZED:
        return

    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    )

    # ------------------------------------------------------------------
    # 1. 控制台：只输出库的 INFO+，第三方库静默
    # ------------------------------------------------------------------
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(console_level)

    lib_root = logging.getLogger("kuafu_llm_infra")
    lib_root.setLevel(file_level)
    lib_root.addHandler(console)

    # ------------------------------------------------------------------
    # 2. all.log — 库内所有 logger 的兜底（不含 probe / third-party）
    #    新增模块无需改配置即可自动入文件。挂在 lib_root，
    #    probe / third-party 因 propagate=False 不会冒泡进来。
    # ------------------------------------------------------------------
    all_handler = RotatingFileHandler(
        os.path.join(log_dir, "all.log"),
        maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    all_handler.setFormatter(fmt)
    all_handler.setLevel(file_level)
    lib_root.addHandler(all_handler)

    # ------------------------------------------------------------------
    # 3. request.log — 请求链路（除冒泡到 all.log 外，单独聚焦请求路径）
    # ------------------------------------------------------------------
    request_handler = RotatingFileHandler(
        os.path.join(log_dir, "request.log"),
        maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    request_handler.setFormatter(fmt)
    request_handler.setLevel(file_level)

    for name in (
        "kuafu_llm_infra.engine",
        "kuafu_llm_infra.stream_monitor",
        "kuafu_llm_infra.scorer",
        "kuafu_llm_infra.recorder",
        "kuafu_llm_infra.recording.dispatcher",
        "kuafu_llm_infra.recording.clickhouse",
    ):
        lg = logging.getLogger(name)
        lg.addHandler(request_handler)

    # ------------------------------------------------------------------
    # 4. probe.log — 后台探测 + 探测告警（不打控制台，不进 all.log）
    # ------------------------------------------------------------------
    probe_handler = RotatingFileHandler(
        os.path.join(log_dir, "probe.log"),
        maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    probe_handler.setFormatter(fmt)
    probe_handler.setLevel(file_level)

    for name in (
        "kuafu_llm_infra.health_checker",
        "kuafu_llm_infra.alert",
    ):
        lg = logging.getLogger(name)
        lg.addHandler(probe_handler)
        lg.propagate = False  # 不冒泡到控制台 / all.log，只写自己文件

    # ------------------------------------------------------------------
    # 5. third-party.log — SDK / HTTP 库日志（不打控制台，不进 all.log）
    # ------------------------------------------------------------------
    tp_handler = RotatingFileHandler(
        os.path.join(log_dir, "third-party.log"),
        maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    tp_handler.setFormatter(fmt)
    tp_handler.setLevel(file_level)

    for name in ("openai", "anthropic", "httpx", "httpcore"):
        lg = logging.getLogger(name)
        lg.setLevel(file_level)
        lg.addHandler(tp_handler)
        lg.propagate = False  # 不冒泡到 root，避免打到控制台

    _INITIALIZED = True
