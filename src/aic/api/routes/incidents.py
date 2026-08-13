"""Incident endpoints: create, investigate, inspect, approve."""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from aic.api.deps import ServiceDep
from aic.api.schemas import (
    CreateIncidentRequest,
    DecisionRequest,
    IncidentResponse,
    InvestigateRequest,
    InvestigationResponse,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.post("", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED)
async def create_incident(payload: CreateIncidentRequest, service: ServiceDep) -> IncidentResponse:
    """Open an incident, and investigate immediately if signals were supplied.

    Investigating inline keeps the demo path a single call. A production
    deployment would enqueue the investigation and return ``202`` — see the
    roadmap in the README.
    """
    incident = await service.create_incident(
        title=payload.title,
        description=payload.description,
        severity=payload.severity,
        services=payload.service_refs(),
    )

    if payload.signals:
        await service.investigate(
            incident.id, [signal.to_domain() for signal in payload.signals]
        )
        incident = await service.get_incident(incident.id)

    return IncidentResponse.from_domain(incident)


@router.get("", response_model=list[IncidentResponse])
async def list_incidents(
    service: ServiceDep, limit: int = Query(default=50, ge=1, le=200)
) -> list[IncidentResponse]:
    incidents = await service.list_incidents(limit=limit)
    return [IncidentResponse.from_domain(incident) for incident in incidents]


@router.get("/{incident_id}", response_model=IncidentResponse)
async def get_incident(incident_id: str, service: ServiceDep) -> IncidentResponse:
    return IncidentResponse.from_domain(await service.get_incident(incident_id))


@router.post("/{incident_id}/investigate", response_model=InvestigationResponse)
async def investigate(
    incident_id: str, payload: InvestigateRequest, service: ServiceDep
) -> InvestigationResponse:
    """Run the agent pipeline over a batch of signals."""
    state = await service.investigate(
        incident_id, [signal.to_domain() for signal in payload.signals]
    )
    return InvestigationResponse.from_state(state)


@router.get("/{incident_id}/investigation", response_model=InvestigationResponse)
async def latest_investigation(incident_id: str, service: ServiceDep) -> InvestigationResponse:
    """The most recent investigation for this incident, with its full audit trail."""
    return InvestigationResponse.from_state(await service.latest_investigation(incident_id))


@router.post("/{incident_id}/actions/{action_id}/decision", response_model=IncidentResponse)
async def decide(
    incident_id: str, action_id: str, payload: DecisionRequest, service: ServiceDep
) -> IncidentResponse:
    """Record the human decision on a proposed action.

    This is the Human-in-the-Loop gate. Nothing in the platform executes an
    action, and an action carrying no approval cannot be executed even by a
    caller that tries.
    """
    incident, _ = await service.decide(
        incident_id=incident_id,
        action_id=action_id,
        approved=payload.approved,
        decided_by=payload.decided_by,
        comment=payload.comment,
    )
    return IncidentResponse.from_domain(incident)
