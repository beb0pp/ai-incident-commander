"""Structured logging.

JSON in every environment except a developer's terminal. Every log line carries
the ``run_id``, so one investigation's trace can be pulled out of a shared log
stream with a single filter — which is the whole point of giving runs an id.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

#: Set once per request/run; merged into every log line emitted underneath it.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)


def _inject_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    request_id = request_id_var.get()
    if request_id and "request_id" not in event_dict:
        event_dict["request_id"] = request_id
    run_id = run_id_var.get()
    if run_id and "run_id" not in event_dict:
        event_dict["run_id"] = run_id
    return event_dict


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog and route the stdlib logger through it."""
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    # Routing through stdlib logging (rather than PrintLogger) is what makes
    # ``add_logger_name`` work and what lets library logs share our handler.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            _inject_context,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )
    # uvicorn duplicates access logs in its own format; let ours be the record.
    logging.getLogger("uvicorn.access").handlers.clear()
