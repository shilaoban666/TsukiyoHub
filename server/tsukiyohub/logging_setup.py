"""日志：统一格式、请求上下文（trace_id）、启动静音。

跟业务项目的差别只有一个：不带任何企业信息，日志字段只保留
``trace_id / path / user``。
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import sys
from collections.abc import Iterator
from typing import Any

from tsukiyohub.config import Settings

_CTX: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("tsukiyo_ctx", default={})
_QUIET: contextvars.ContextVar[bool] = contextvars.ContextVar("tsukiyo_quiet", default=False)


class _ContextFilter(logging.Filter):
    """把请求上下文拼进日志行。"""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _CTX.get()
        record.trace_id = ctx.get("trace_id", "-")
        record.path = ctx.get("path", "-")
        record.user = ctx.get("user", "-")
        return True


class _QuietFilter(logging.Filter):
    """启动静音期间只放过 WARNING 以上（结论仍会打印到控制台之外的日志文件）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _QUIET.get() or record.levelno >= logging.WARNING


def setup_logging(settings: Settings) -> None:
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.addFilter(_ContextFilter())
    console.addFilter(_QuietFilter())
    if settings.log_json:
        console.setFormatter(logging.Formatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s",'
            '"trace":"%(trace_id)s","msg":"%(message)s"}'
        ))
    else:
        console.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(trace_id)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
        ))
    root.addHandler(console)

    # 文件日志：完整记录（包括启动期的过程日志）
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(settings.logs_dir / "app.log", encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.addFilter(_ContextFilter())
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(trace_id)s] %(name)s: %(message)s"
        ))
        root.addHandler(file_handler)
    except OSError:  # pragma: no cover - 只读文件系统
        pass

    for noisy in ("httpx", "httpcore", "urllib3", "pymilvus", "sqlalchemy.engine", "jieba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def bind_request_context(**kwargs: Any) -> None:
    ctx = dict(_CTX.get())
    ctx.update({k: v for k, v in kwargs.items() if v is not None})
    _CTX.set(ctx)


def clear_request_context() -> None:
    _CTX.set({})


@contextlib.contextmanager
def quiet_startup() -> Iterator[None]:
    """启动期安静：装配过程只写文件日志，控制台只留结论。"""
    token = _QUIET.set(True)
    try:
        yield
    finally:
        _QUIET.reset(token)


__all__ = [
    "setup_logging", "get_logger", "bind_request_context", "clear_request_context", "quiet_startup",
]