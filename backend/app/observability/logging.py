"""Structured JSON logging with per-request trace correlation.

Every log line carries `trace_id` so an audit row, a metric spike and a log line
can be joined for one request. In development the renderer switches to a
human-readable console format.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from backend.app.config import settings

# Bound by RequestContextMiddleware, read by the log processor and the audit writer.
trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)
run_id_ctx: ContextVar[str | None] = ContextVar("run_id", default=None)

# Never let these reach a log sink, whatever the caller passes.
REDACTED_KEYS = {
    "password",
    "hashed_password",
    "token",
    "access_token",
    "authorization",
    "jwt_secret",
    "secret",
    "api_key",
}


def _inject_context(_logger: Any, _name: str, event_dict: dict) -> dict:
    for key, ctx in (("trace_id", trace_id_ctx), ("user_id", user_id_ctx), ("run_id", run_id_ctx)):
        value = ctx.get()
        if value is not None:
            event_dict.setdefault(key, value)
    return event_dict


def _redact_secrets(_logger: Any, _name: str, event_dict: dict) -> dict:
    for key in list(event_dict):
        if key.lower() in REDACTED_KEYS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    # Uvicorn's own access log duplicates our request middleware; silence it.
    logging.getLogger("uvicorn.access").disabled = True

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.is_production or settings.app_env == "docker"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_context,
            _redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
