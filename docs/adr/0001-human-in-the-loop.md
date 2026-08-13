# ADR 0001 — The platform proposes; a human disposes

**Status:** Accepted · **Date:** 2026-08-13

## Context

An incident response agent that can act is far more useful than one that can
only advise — and far more dangerous. The failure modes are not hypothetical: a
model can misread a symptom as a cause, a retrieved runbook can be subtly wrong
for the situation at hand, and telemetry ingested from an outside system is
untrusted input that can carry instructions.

The naive mitigation is prompt-level: tell the model to be careful, to ask
before acting, to assess risk honestly. That mitigation fails exactly when it
matters, because it depends on the component that is wrong being right about
being wrong.

## Decision

Safety properties are enforced **structurally**, outside the model.

1. **No mutating tool exists in an investigation.** `ToolRegistry` takes a
   `max_risk` ceiling and raises at *registration* time. The investigation
   registry is built with `READ_ONLY`, so there is nothing to escalate to.

2. **Risk is recomputed, never accepted.** The Action Agent declares its own
   assessment of an action's blast radius. `ActionPolicy.classify` derives the
   real one from the command text and overrides it. Both values are retained so
   the disagreement stays visible.

3. **A denylist removes rather than gates.** Operations that are never
   legitimate incident mitigation are dropped from the plan, not queued for
   approval. A human should never be shown `DROP DATABASE` as an option to click
   through at 3am.

4. **`HIGH` is never auto-approvable.** The auto-approve ceiling is configurable
   up to `MEDIUM`. Setting it to `HIGH` fails validation in both `Settings` and
   `ActionPolicy`.

5. **The execution gate is re-checked at execution time.** `assert_executable`
   re-derives risk and demands a matching approval record. Holding a stale plan
   object is not a way around it.

## Consequences

**Accepted:** the platform cannot resolve an incident unattended. That is the
point, but it does cap the product's ceiling — mean-time-to-recovery still
includes a human round trip.

**Accepted:** the risk classifier is a pattern list, and pattern lists have gaps.
The default for an unmatched command is `HIGH`, so a gap fails toward asking a
human rather than away from it.

**Gained:** the safety argument does not depend on the model's behaviour, so it
survives a model swap, a prompt regression, and a prompt injection.

**Gained:** `aic_guardrail_events_total{kind="reclassified"}` turns "how often
does the model under-report blast radius" into a number on a dashboard.
