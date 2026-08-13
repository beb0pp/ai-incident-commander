"""Prometheus metrics.

Deliberately few, and each one answers a question an operator of *this* platform
would actually ask: is it working, how long does an investigation take, how much
is it costing in tokens, and how often are guardrails firing.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

REGISTRY = CollectorRegistry()

investigations_total = Counter(
    "aic_investigations_total",
    "Investigations completed, by terminal incident status.",
    labelnames=("status",),
    registry=REGISTRY,
)

investigation_duration_seconds = Histogram(
    "aic_investigation_duration_seconds",
    "Wall-clock duration of a full investigation.",
    buckets=(1, 5, 10, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)

agent_runs_total = Counter(
    "aic_agent_runs_total",
    "Agent executions, by agent and outcome.",
    labelnames=("agent", "outcome"),
    registry=REGISTRY,
)

agent_duration_seconds = Histogram(
    "aic_agent_duration_seconds",
    "Per-agent execution time.",
    labelnames=("agent",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
    registry=REGISTRY,
)

tool_calls_total = Counter(
    "aic_tool_calls_total",
    "Tool invocations, by tool and outcome.",
    labelnames=("tool", "outcome"),
    registry=REGISTRY,
)

llm_tokens_total = Counter(
    "aic_llm_tokens_total",
    "Tokens consumed, by direction.",
    labelnames=("direction",),
    registry=REGISTRY,
)

guardrail_events_total = Counter(
    "aic_guardrail_events_total",
    "Guardrail decisions, by kind (denied, reclassified, approval_required).",
    labelnames=("kind",),
    registry=REGISTRY,
)

approvals_total = Counter(
    "aic_approvals_total",
    "Human approval decisions recorded.",
    labelnames=("outcome",),
    registry=REGISTRY,
)


def render() -> bytes:
    """Serialize the registry in Prometheus text exposition format."""
    return generate_latest(REGISTRY)
