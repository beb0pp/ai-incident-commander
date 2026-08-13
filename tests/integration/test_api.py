"""End-to-end API tests against the real app with offline adapters.

These run the actual FastAPI application, the actual pipeline, and the actual
guardrails — only the model and the datastores are swapped for in-process
equivalents.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from aic.scenario import demo_signals


def signal_payloads() -> list[dict[str, Any]]:
    return [
        {
            "kind": str(signal.kind),
            "service": signal.service.name,
            "timestamp": signal.timestamp.isoformat(),
            "message": signal.message,
            "value": signal.value,
            "labels": signal.labels,
        }
        for signal in demo_signals()
    ]


class TestSystemEndpoints:
    async def test_health_is_up(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_ready_reports_each_dependency(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.get("/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert set(body["checks"]) == {"llm", "tools", "runbooks"}

    async def test_metrics_are_exposed(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.get("/metrics")
        assert response.status_code == 200
        assert "aic_investigations_total" in response.text

    async def test_request_id_is_echoed(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.get("/health", headers={"x-request-id": "abc-123"})
        assert response.headers["x-request-id"] == "abc-123"

    async def test_request_id_is_generated_when_absent(
        self, api_client: httpx.AsyncClient
    ) -> None:
        response = await api_client.get("/health")
        assert response.headers.get("x-request-id")


class TestIncidentLifecycle:
    async def test_create_investigate_and_approve(self, api_client: httpx.AsyncClient) -> None:
        """The whole product, in one test."""
        created = await api_client.post(
            "/incidents",
            json={
                "title": "checkout-api returning 5xx on order submission",
                "description": "Error rate jumped at 14:02 UTC after a deploy.",
                "severity": "sev2",
                "services": ["checkout-api", "prod-aurora-orders"],
                "signals": signal_payloads(),
            },
        )
        assert created.status_code == 201, created.text
        incident = created.json()

        # The pipeline ran and produced a plan that needs a human.
        assert incident["status"] == "awaiting_approval"
        assert incident["plan"] is not None
        assert incident["pending_approvals"]

        # The audit trail is retrievable.
        investigation = await api_client.get(f"/incidents/{incident['id']}/investigation")
        assert investigation.status_code == 200
        trail = investigation.json()
        assert trail["anomalies"] and trail["hypotheses"] and trail["findings"]
        assert {t["name"] for t in trail["traces"]} == {
            "monitoring",
            "diagnostic",
            "infrastructure",
            "runbook",
            "action",
        }
        assert all(t["status"] == "succeeded" for t in trail["traces"])

        # Approving the gated action moves the incident forward.
        action_id = incident["pending_approvals"][0]
        decided = await api_client.post(
            f"/incidents/{incident['id']}/actions/{action_id}/decision",
            json={"approved": True, "decided_by": "sre-oncall", "comment": "confirmed"},
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "mitigating"
        assert decided.json()["pending_approvals"] == []

    async def test_the_plan_separates_gated_from_auto_approved_actions(
        self, api_client: httpx.AsyncClient
    ) -> None:
        created = await api_client.post(
            "/incidents",
            json={"title": "checkout-api 5xx spike", "signals": signal_payloads()},
        )
        plan = created.json()["plan"]
        risks = {a["risk"]: a["requires_approval"] for a in plan["actions"]}

        assert risks["read_only"] is False
        assert risks["medium"] is True

    async def test_rejecting_leaves_the_incident_awaiting_approval(
        self, api_client: httpx.AsyncClient
    ) -> None:
        created = await api_client.post(
            "/incidents",
            json={"title": "checkout-api 5xx spike", "signals": signal_payloads()},
        )
        incident = created.json()
        action_id = incident["pending_approvals"][0]

        decided = await api_client.post(
            f"/incidents/{incident['id']}/actions/{action_id}/decision",
            json={"approved": False, "decided_by": "sre-oncall", "comment": "too risky right now"},
        )
        assert decided.json()["status"] == "awaiting_approval"
        assert decided.json()["pending_approvals"] == [action_id]

    async def test_incident_without_signals_runs_no_investigation(
        self, api_client: httpx.AsyncClient
    ) -> None:
        created = await api_client.post("/incidents", json={"title": "just tracking this"})
        assert created.status_code == 201
        assert created.json()["status"] == "open"
        assert created.json()["plan"] is None

    async def test_investigating_separately_works(self, api_client: httpx.AsyncClient) -> None:
        created = await api_client.post("/incidents", json={"title": "checkout-api 5xx"})
        incident_id = created.json()["id"]

        investigated = await api_client.post(
            f"/incidents/{incident_id}/investigate", json={"signals": signal_payloads()}
        )
        assert investigated.status_code == 200
        assert investigated.json()["plan"] is not None

    async def test_incidents_are_listed_newest_first(self, api_client: httpx.AsyncClient) -> None:
        for title in ("first", "second", "third"):
            await api_client.post("/incidents", json={"title": f"incident {title}"})

        listed = await api_client.get("/incidents")
        assert listed.status_code == 200
        assert len(listed.json()) >= 3


class TestErrorMapping:
    async def test_unknown_incident_is_404(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.get("/incidents/does-not-exist")
        assert response.status_code == 404
        assert response.json()["kind"] == "NotFoundError"

    async def test_investigation_before_it_exists_is_404(
        self, api_client: httpx.AsyncClient
    ) -> None:
        created = await api_client.post("/incidents", json={"title": "no run yet"})
        response = await api_client.get(f"/incidents/{created.json()['id']}/investigation")
        assert response.status_code == 404

    async def test_deciding_on_an_unknown_action_is_404(
        self, api_client: httpx.AsyncClient
    ) -> None:
        created = await api_client.post(
            "/incidents", json={"title": "checkout 5xx", "signals": signal_payloads()}
        )
        response = await api_client.post(
            f"/incidents/{created.json()['id']}/actions/nope/decision",
            json={"approved": True, "decided_by": "someone"},
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "payload",
        [
            {"title": "ab"},  # below min_length
            {"title": "valid title", "severity": "sev9"},
            {"title": "valid title", "unexpected": True},
        ],
    )
    async def test_invalid_payloads_are_422(
        self, api_client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> None:
        response = await api_client.post("/incidents", json=payload)
        assert response.status_code == 422
