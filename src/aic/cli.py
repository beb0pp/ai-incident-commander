"""Command line entry point.

``aic demo`` is the thirty-second version of this project: it runs a real
incident through the real pipeline with the scripted model and prints the audit
trail. No credentials, no services, no docker.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from aic import __version__
from aic.bootstrap import build_container
from aic.config import LLMProvider, load_settings
from aic.infrastructure.observability import configure_logging
from aic.orchestration.state import InvestigationState
from aic.scenario import demo_incident, demo_signals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aic", description="AI Incident Commander")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="Run the bundled incident end to end.")
    demo.add_argument(
        "--provider",
        choices=[p.value for p in LLMProvider],
        help="Override AIC_LLM_PROVIDER for this run.",
    )

    init = sub.add_parser("init", help="Write a starter aic.yaml.")
    init.add_argument("--force", action="store_true", help="Overwrite an existing file.")

    doctor = sub.add_parser(
        "doctor", help="Check that every configured source is reachable and permitted."
    )
    doctor.add_argument("--manifest", type=Path, help="Path to aic.yaml.")

    sub.add_parser("serve", help="Run the HTTP API with uvicorn.")
    sub.add_parser("migrate", help="Apply pending database migrations.")

    args = parser.parse_args(argv)

    if args.command == "demo":
        return asyncio.run(_run_demo(provider=args.provider))
    if args.command == "init":
        return _init(force=args.force)
    if args.command == "doctor":
        return asyncio.run(_doctor(manifest_path=args.manifest))
    if args.command == "serve":
        return _serve()
    if args.command == "migrate":
        return asyncio.run(_migrate())
    return 1


def _init(*, force: bool) -> int:
    """Write a starter manifest next to wherever the user is standing."""
    from aic.manifest import EXAMPLE

    target = Path("aic.yaml")
    if target.exists() and not force:
        sys.stdout.write(f"{target} already exists. Pass --force to overwrite it.\n")
        return 1

    target.write_text(EXAMPLE, encoding="utf-8")
    sys.stdout.write(
        f"Wrote {target}.\n\n"
        "Next: uncomment the aws source, set your region, then run `aic doctor`.\n"
    )
    return 0


async def _doctor(*, manifest_path: Path | None) -> int:
    """Report on every source before an incident does it for you."""
    from aic.diagnostics import CheckStatus, missing_actions, run_checks
    from aic.manifest import find_manifest, load_manifest

    settings = load_settings(log_format="console")
    configure_logging(level="WARNING", json_output=False)

    resolved = manifest_path or settings.manifest_path or find_manifest()
    manifest = load_manifest(resolved)

    out = sys.stdout.write
    out(f"\nmanifest: {resolved or 'none found — using the simulated source'}\n")
    out(f"sources:  {', '.join(s.type for s in manifest.sources)}\n\n")

    checks = await run_checks(manifest)

    width = max((len(c.capability) for c in checks), default=10)
    symbols = {
        CheckStatus.OK: "  ok  ",
        CheckStatus.DENIED: " DENY ",
        CheckStatus.UNAVAILABLE: " FAIL ",
        CheckStatus.SKIPPED: " skip ",
    }
    current = ""
    for check in checks:
        if check.source != current:
            out(f"{check.source}\n")
            current = check.source
        out(f"  [{symbols[check.status]}] {check.capability:<{width}}  {check.detail}\n")

    needed = missing_actions(checks)
    if needed:
        out("\nAdd these IAM actions and run doctor again:\n")
        for action in needed:
            out(f"  - {action}\n")

    failed = [c for c in checks if not c.healthy]
    if failed:
        out(f"\n{len(failed)} check(s) failed.\n\n")
        return 1

    out("\nAll checks passed. Run `aic demo` or `aic serve`.\n\n")
    return 0


async def _run_demo(*, provider: str | None) -> int:
    overrides: dict[str, Any] = {"log_format": "console"}
    if provider:
        overrides["llm_provider"] = provider

    settings = load_settings(**overrides)
    configure_logging(level=settings.log_level, json_output=False)

    container = build_container(settings)
    incident = demo_incident()
    await container.repository.save(incident)
    state = await container.service.investigate(incident.id, demo_signals())

    _print_report(state)
    return 0 if state.plan is not None else 1


def _print_report(state: InvestigationState) -> None:
    out = sys.stdout.write
    out("\n" + "=" * 78 + "\n")
    out(f"INVESTIGATION {state.run_id}\n")
    out(f"Incident: {state.incident.title}\n")
    out(f"Status:   {state.incident.status}\n")
    out("=" * 78 + "\n")

    out(f"\nANOMALIES ({len(state.anomalies)})\n")
    for anomaly in state.anomalies:
        out(f"  [{anomaly.score:.2f}] {anomaly.service}: {anomaly.summary}\n")

    out(f"\nHYPOTHESES ({len(state.hypotheses)})\n")
    for hypothesis in state.hypotheses:
        out(f"  [{hypothesis.confidence:.2f}] {hypothesis.title}\n")
        out(f"        {hypothesis.reasoning}\n")

    out(f"\nINFRASTRUCTURE FINDINGS ({len(state.findings)})\n")
    for finding in state.findings:
        mark = "ok " if finding.healthy else "BAD"
        out(f"  {mark} {finding.resource}: {finding.summary}\n")

    out(f"\nRUNBOOKS ({len(state.runbooks)})\n")
    for runbook in state.runbooks:
        out(f"  [{runbook.score:.3f}] {runbook.title}\n")

    if state.plan is not None:
        out("\nPLAN\n")
        out(f"  {state.plan.summary}\n\n")
        for index, action in enumerate(state.plan.actions, start=1):
            gate = "NEEDS APPROVAL" if action.requires_approval else "auto-approved"
            out(f"  {index}. {action.title}  [{action.risk} / {gate}]\n")
            if action.command:
                out(f"     $ {action.command}\n")
            if action.rollback:
                out(f"     rollback: {action.rollback}\n")

    out("\nEXECUTION TRACE\n")
    for trace in state.traces:
        duration = f"{trace.duration_ms:.0f}ms" if trace.duration_ms is not None else "-"
        out(f"  {trace.name:<16} {trace.status:<10} {duration:>8}  attempts={trace.attempts}\n")

    if state.errors:
        out("\nERRORS\n")
        for error in state.errors:
            out(f"  - {error}\n")

    out(
        f"\nTokens: in={state.usage.input_tokens} out={state.usage.output_tokens}"
        f" cache_read={state.usage.cache_read_tokens}\n\n"
    )


def _serve() -> int:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "aic.api.app:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - containers bind all interfaces by design
        port=8000,
        log_config=None,
        reload=settings.env == "local",
    )
    return 0


async def _migrate() -> int:
    import asyncpg

    from aic.infrastructure.db.migrations import migrate

    settings = load_settings()
    configure_logging(level=settings.log_level, json_output=False)
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        applied = await migrate(pool)
    finally:
        await pool.close()

    sys.stdout.write(
        f"applied {len(applied)} migration(s): {applied}\n" if applied else "already up to date\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
