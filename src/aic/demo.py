"""The scripted model behind ``AIC_LLM_PROVIDER=fake``.

Every handler here parses the prompt it was actually given and answers from it,
so the demo output cites real signal and anomaly ids rather than invented ones.
That matters: a demo that fabricates its own evidence would hide exactly the
class of bug (unsourced output) that the real pipeline is built to catch.

This module is also the composition point between :mod:`aic.llm.fake` and the
agent response schemas, which is why it lives outside both.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from aic.agents.action import ActionDraft, ActionPlanDraft
from aic.agents.diagnostic import DiagnosticOutput, EvidenceDraft, HypothesisDraft
from aic.agents.infrastructure import FindingDraft, InfrastructureOutput
from aic.agents.monitoring import AnomalyDraft, MonitoringOutput
from aic.agents.runbook import RunbookSelection, SelectedRunbook
from aic.domain.models import RiskLevel, Severity, SignalKind
from aic.llm.base import ToolSpec
from aic.llm.fake import ScriptedLLMClient

_SERVICE_HEADING = re.compile(r"^## (?P<service>[\w.-]+) \(\d+ signals\)$", re.MULTILINE)
_SIGNAL_LINE = re.compile(r"^- \[(?P<id>[0-9a-f]+)\] .*?(?P<kind>log|metric|event|trace):", re.M)
_BRACKET_ID = re.compile(r"\[([0-9a-f]{8,})\]")
_TOOL_HEADING = re.compile(r"^### (?P<tool>\w+)\((?P<args>.*?)\) -> (?P<status>OK|ERROR)$", re.M)
_RUNBOOK_HEADING = re.compile(r"^### (?P<doc>\S+) — ", re.MULTILINE)


# -- monitoring --------------------------------------------------------------


def monitoring_handler(prompt: str) -> MonitoringOutput:
    """Group the prompt's signals by service, one anomaly per service."""
    sections = _split_service_sections(prompt)
    anomalies: list[AnomalyDraft] = []

    for service, block in sections.items():
        matches = list(_SIGNAL_LINE.finditer(block))
        if not matches:
            continue
        signal_ids = [m.group("id") for m in matches]
        kinds = {m.group("kind") for m in matches}
        kind = SignalKind.METRIC if "metric" in kinds else SignalKind(next(iter(kinds)))
        anomalies.append(
            AnomalyDraft(
                service_name=service,
                summary=(
                    f"{service} is emitting {len(signal_ids)} correlated "
                    f"{'/'.join(sorted(kinds))} signals in a tight window"
                ),
                kind=kind,
                severity_hint=Severity.SEV2 if len(signal_ids) >= 3 else Severity.SEV3,
                score=min(0.95, 0.5 + 0.1 * len(signal_ids)),
                signal_ids=signal_ids,
            )
        )

    anomalies.sort(key=lambda a: a.score, reverse=True)
    return MonitoringOutput(anomalies=anomalies)


def _split_service_sections(prompt: str) -> dict[str, str]:
    headings = list(_SERVICE_HEADING.finditer(prompt))
    sections: dict[str, str] = {}
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(prompt)
        sections[match.group("service")] = prompt[match.end() : end]
    return sections


# -- diagnostic --------------------------------------------------------------


def diagnostic_handler(prompt: str) -> DiagnosticOutput:
    """Two competing hypotheses, both sourced from ids present in the prompt."""
    ids = _BRACKET_ID.findall(prompt)
    primary = ids[0] if ids else "unknown"
    secondary = ids[1] if len(ids) > 1 else primary
    services = sorted(set(_SERVICE_HEADING.findall(prompt))) or ["checkout-api"]

    return DiagnosticOutput(
        hypotheses=[
            HypothesisDraft(
                title="Database connection pool exhaustion following the latest deploy",
                reasoning=(
                    "The error onset lines up with a release, and the failures are "
                    "connection-acquisition timeouts rather than application errors. "
                    "A pool sizing change multiplied across tasks would produce exactly "
                    "this shape: the app is healthy, it just cannot get a connection."
                ),
                confidence=0.72,
                suspected_services=services[:2],
                evidence=[
                    EvidenceDraft(
                        source="anomaly",
                        reference=primary,
                        detail="error burst begins minutes after the deployment timestamp",
                    )
                ],
            ),
            HypothesisDraft(
                title="Downstream dependency saturation causing cascading timeouts",
                reasoning=(
                    "An alternative reading: the database is a victim rather than the "
                    "cause, and a slow downstream call is holding connections open. "
                    "Distinguishing the two needs the current connection count and the "
                    "write latency, which the infrastructure tools can supply."
                ),
                confidence=0.38,
                suspected_services=services[:1],
                evidence=[
                    EvidenceDraft(
                        source="anomaly",
                        reference=secondary,
                        detail="latency rises before the error rate does",
                    )
                ],
            ),
        ]
    )


# -- infrastructure ----------------------------------------------------------


def infrastructure_planner(
    tools: Sequence[ToolSpec], prompt: str
) -> list[tuple[str, dict[str, Any]]]:
    """Broad first (alarms, deploys), then drill into the named resources."""
    available = {tool.name for tool in tools}
    plan: list[tuple[str, dict[str, Any]]] = []

    for name in ("list_active_alarms", "list_recent_deployments"):
        if name in available:
            plan.append((name, {}))

    for service in sorted(set(_SERVICE_HEADING.findall(prompt))) or ["checkout-api"]:
        if "describe_ecs_service" in available:
            plan.append(("describe_ecs_service", {"service": service}))

    if "describe_rds_instance" in available:
        plan.append(("describe_rds_instance", {"identifier": "prod-aurora-orders"}))
    if "describe_cache_cluster" in available:
        plan.append(("describe_cache_cluster", {"cluster_id": "prod-redis-sessions"}))

    return plan


def infrastructure_handler(prompt: str) -> InfrastructureOutput:
    """Convert the tool transcript into findings, judging health from the payload."""
    findings: list[FindingDraft] = []
    headings = list(_TOOL_HEADING.finditer(prompt))

    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(prompt)
        payload = prompt[match.end() : end]
        tool = match.group("tool")
        resource = _resource_from_args(match.group("args")) or tool
        errored = match.group("status") == "ERROR"
        unhealthy = errored or _looks_unhealthy(payload)
        findings.append(
            FindingDraft(
                tool=tool,
                resource=resource,
                summary=_summarize(tool, payload, unhealthy=unhealthy, errored=errored),
                healthy=not unhealthy,
            )
        )

    return InfrastructureOutput(findings=findings)


def _resource_from_args(args: str) -> str | None:
    match = re.search(r"'(?:service|identifier|cluster_id|queue_name)':\s*'([^']+)'", args)
    return match.group(1) if match else None


def _looks_unhealthy(payload: str) -> bool:
    if '"state": "ALARM"' in payload or "'state': 'ALARM'" in payload:
        return True
    running = re.search(r'"runningCount":\s*(\d+)', payload)
    desired = re.search(r'"desiredCount":\s*(\d+)', payload)
    if running and desired and int(running.group(1)) < int(desired.group(1)):
        return True
    current = re.search(r'"currentConnections":\s*(\d+)', payload)
    maximum = re.search(r'"maxConnections":\s*(\d+)', payload)
    return bool(
        current and maximum and int(current.group(1)) >= int(maximum.group(1)) * 0.95
    )


def _summarize(tool: str, payload: str, *, unhealthy: bool, errored: bool) -> str:
    if errored:
        return f"{tool} could not be read; this resource is an unverified gap"
    if not unhealthy:
        return f"{tool} reports this resource within normal parameters"
    if "currentConnections" in payload:
        return "connection count is at or above 95% of max_connections"
    if "runningCount" in payload:
        return "running task count is below desired; tasks are not staying up"
    return "one or more alarms are firing for this resource"


# -- runbook -----------------------------------------------------------------


def runbook_handler(prompt: str) -> RunbookSelection:
    """Keep the connection-pool and deployment procedures, drop the rest."""
    keywords = ("connection", "pool", "deploy", "rollback", "database")
    selected: list[SelectedRunbook] = []

    headings = list(_RUNBOOK_HEADING.finditer(prompt))
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(prompt)
        body = prompt[match.start() : end].lower()
        if any(keyword in body for keyword in keywords):
            selected.append(
                SelectedRunbook(
                    document_id=match.group("doc"),
                    applies_because="addresses the connection-exhaustion failure mode directly",
                )
            )

    return RunbookSelection(selected=selected)


# -- action ------------------------------------------------------------------


def action_handler(prompt: str) -> ActionPlanDraft:
    """A plan that exercises both guardrail paths: auto-approved and gated."""
    ids = _BRACKET_ID.findall(prompt)
    cited = ids[:2] or ["n/a"]

    return ActionPlanDraft(
        summary=(
            "checkout-api is failing on database connection acquisition after the "
            "most recent deploy raised the per-task pool size. The pool change "
            "multiplied by the task count now exceeds the Aurora connection ceiling. "
            "Confirm the connection count, then roll the pool size back."
        ),
        actions=[
            ActionDraft(
                title="Confirm the Aurora connection ceiling is the binding constraint",
                description=(
                    "Read the current connection count against max_connections before "
                    "changing anything, so the rollback is justified by data."
                ),
                command="aws rds describe-db-instances --db-instance-identifier prod-aurora-orders",
                target_service="prod-aurora-orders",
                # Correctly declared: read-only, and it will stay read-only.
                declared_risk=RiskLevel.READ_ONLY,
                rationale="Establishes the fact the rest of the plan depends on.",
                rollback="None required; this action reads only.",
                evidence_refs=cited,
            ),
            ActionDraft(
                title="Roll checkout-api back to the previous task definition",
                description=(
                    "Revert to checkout-api:411, which carries the previous pool size. "
                    "This restores capacity without touching the database."
                ),
                command=(
                    "aws ecs update-service --cluster prod-cluster --service checkout-api "
                    "--task-definition checkout-api:411"
                ),
                target_service="checkout-api",
                # Understated on purpose: the policy layer reclassifies this to
                # MEDIUM from the command text and routes it to a human.
                declared_risk=RiskLevel.LOW,
                rationale=(
                    "Smallest reversible change that addresses the leading hypothesis."
                ),
                rollback="Re-deploy checkout-api:412 once the pool sizing is corrected.",
                evidence_refs=cited,
            ),
        ],
    )


# -- wiring ------------------------------------------------------------------


def build_demo_llm() -> ScriptedLLMClient:
    """A scripted client wired to every agent schema in the pipeline."""
    client = ScriptedLLMClient(tool_planner=infrastructure_planner)
    client.register(MonitoringOutput, monitoring_handler)
    client.register(DiagnosticOutput, diagnostic_handler)
    client.register(InfrastructureOutput, infrastructure_handler)
    client.register(RunbookSelection, runbook_handler)
    client.register(ActionPlanDraft, action_handler)
    return client
