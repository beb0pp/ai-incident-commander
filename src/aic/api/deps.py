"""FastAPI dependency wiring.

The container is built once at startup and stored on ``app.state``; these
dependencies just read it. Tests override ``get_container`` to inject a container
built with in-memory adapters and a scripted model.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from aic.bootstrap import Container
from aic.service import IncidentService


def get_container(request: Request) -> Container:
    return request.app.state.container  # type: ignore[no-any-return]


def get_service(container: Annotated[Container, Depends(get_container)]) -> IncidentService:
    return container.service


ContainerDep = Annotated[Container, Depends(get_container)]
ServiceDep = Annotated[IncidentService, Depends(get_service)]
