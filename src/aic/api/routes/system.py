"""Health, readiness, and metrics."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from aic import __version__
from aic.api.deps import ContainerDep
from aic.infrastructure.observability import metrics

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up. Deliberately does not touch dependencies."""
    return {"status": "ok", "version": __version__}


@router.get("/ready")
async def ready(container: ContainerDep, response: Response) -> dict[str, object]:
    """Readiness: the pieces this replica needs are actually usable."""
    checks: dict[str, bool] = {
        "llm": container.llm is not None,
        "tools": len(container.registry) > 0,
        "runbooks": bool(container.retriever.retrieve("database connection")),
    }
    if not all(checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": all(checks.values()), "checks": checks}


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")
