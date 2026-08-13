"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aic import __version__
from aic.api.routes import incidents, system
from aic.bootstrap import Container, build_container
from aic.config import Settings, load_settings
from aic.domain.errors import (
    ApprovalRequiredError,
    DomainError,
    GuardrailViolationError,
    LLMError,
    NotFoundError,
)
from aic.infrastructure.observability import configure_logging, request_id_var
from aic.infrastructure.observability.logging import run_id_var

log = structlog.get_logger(__name__)

#: Domain error -> HTTP status. Anything not listed is a 500, which is correct:
#: an unmapped error is a bug, not a client mistake.
_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    NotFoundError: 404,
    ApprovalRequiredError: 409,
    GuardrailViolationError: 422,
    LLMError: 502,
}


def create_app(
    settings: Settings | None = None, *, container: Container | None = None
) -> FastAPI:
    """Build the application. ``container`` lets tests inject their own wiring."""
    resolved = settings or load_settings()
    configure_logging(
        level=resolved.log_level, json_output=resolved.log_format.value == "json"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or build_container(resolved)
        log.info("api.started", env=resolved.env, version=__version__)
        yield
        log.info("api.stopped")

    app = FastAPI(
        title="AI Incident Commander",
        version=__version__,
        summary=(
            "Multi-agent investigation and response for application "
            "and infrastructure incidents."
        ),
        lifespan=lifespan,
    )

    app.include_router(system.router)
    app.include_router(incidents.router)

    @app.middleware("http")
    async def bind_request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        """Give every request an id and put it on every log line it produces."""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        run_token = run_id_var.set(None)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
            run_id_var.reset(run_token)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        status_code = next(
            (code for kind, code in _STATUS_BY_ERROR.items() if isinstance(exc, kind)), 500
        )
        if status_code >= 500:
            log.error("api.unhandled_domain_error", kind=type(exc).__name__, error=str(exc))
        return JSONResponse(
            status_code=status_code,
            content={"detail": str(exc), "kind": type(exc).__name__},
        )

    return app


# Serve with:  uvicorn aic.api.app:create_app --factory

