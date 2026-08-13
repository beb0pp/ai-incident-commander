"""The bundled incident scenario.

Sample data only — no model, no agents, nothing but domain objects. It is used by
the demo, by the tests, and by ``docs/examples/incident.json``, so the scenario a
reviewer reads about is the same one the test suite asserts against.

The incident: ``checkout-api`` starts returning 5xx shortly after a deploy. The
real cause is connection-pool exhaustion on Aurora, triggered by a pool-sizing
change in that deploy. Every individual signal points somewhere slightly
misleading — the ECS task restarts look like an application crash, the write
latency looks like a slow database — and only the correlation across all three
gets you to the deploy. That is deliberately the shape of incident where a
correlating agent earns its place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aic.domain.models import Incident, ServiceRef, Severity, Signal, SignalKind

#: Incident onset. Everything else is expressed relative to it.
T0 = datetime(2026, 8, 13, 13, 58, tzinfo=UTC)

CHECKOUT = ServiceRef(name="checkout-api", kind="service")
ORDERS_DB = ServiceRef(name="prod-aurora-orders", kind="database")


def demo_incident() -> Incident:
    return Incident(
        title="checkout-api returning 5xx on order submission",
        description=(
            "Error rate on POST /orders jumped from ~0.1% to ~18% at 14:02 UTC. "
            "Cart and catalog are unaffected. A deploy went out at 13:54."
        ),
        severity=Severity.SEV2,
        services=[CHECKOUT, ORDERS_DB],
    )


def demo_signals() -> list[Signal]:
    """Telemetry for the scenario, in the shape the platform ingests."""
    return [
        Signal(
            kind=SignalKind.EVENT,
            service=CHECKOUT,
            timestamp=T0 - timedelta(minutes=4),
            message="deployment completed: checkout-api:412 (pool size 5 -> 20)",
            labels={"source": "ci-pipeline"},
        ),
        Signal(
            kind=SignalKind.LOG,
            service=CHECKOUT,
            timestamp=T0 + timedelta(minutes=4),
            message="timeout acquiring connection from pool after 30000ms",
            labels={"level": "error", "count": "1284"},
        ),
        Signal(
            kind=SignalKind.METRIC,
            service=CHECKOUT,
            timestamp=T0 + timedelta(minutes=4),
            message="HTTPCode_Target_5XX_Count",
            value=412.0,
            labels={"threshold": "25"},
        ),
        Signal(
            kind=SignalKind.METRIC,
            service=CHECKOUT,
            timestamp=T0 + timedelta(minutes=5),
            message="RunningTaskCount below DesiredTaskCount",
            value=8.0,
            labels={"desired": "12"},
        ),
        Signal(
            kind=SignalKind.METRIC,
            service=ORDERS_DB,
            timestamp=T0 + timedelta(minutes=3),
            message="DatabaseConnections",
            value=199.0,
            labels={"max_connections": "200"},
        ),
        Signal(
            kind=SignalKind.METRIC,
            service=ORDERS_DB,
            timestamp=T0 + timedelta(minutes=4),
            message="WriteLatency",
            value=148.7,
            labels={"unit": "ms", "baseline": "12"},
        ),
        Signal(
            kind=SignalKind.LOG,
            service=ORDERS_DB,
            timestamp=T0 + timedelta(minutes=4),
            message=(
                "FATAL: remaining connection slots are reserved for "
                "superuser connections"
            ),
            labels={"level": "error"},
        ),
    ]
