# ADR 0006 — A declarative manifest, with the safety ceiling as data

**Status:** Accepted · **Date:** 2026-08-14

## Context

Adopting the platform required writing Python: a `Tool` subclass per resource, an
adapter for the environment, and an edit to the composition root. That is a
framework, not a tool, and it puts a day of work between someone deciding to try
this and seeing it run against their own infrastructure.

The obvious fix — configuration instead of code — runs straight into the property
the project is built on. The guarantee that an investigation can read and never
write was structural precisely *because* it was hard-coded: `ToolRegistry` was
constructed with `RiskLevel.READ_ONLY` and refused anything above it. Making the
tool surface configurable is exactly how that guarantee gets negotiated away.

## Decision

An `aic.yaml` manifest describes what the installation is connected to: sources,
runbook locations, and **a risk ceiling per source**.

The ceiling is the interesting part. It is now data, but three things keep it
from being a loophole:

1. **The registry still enforces it, at construction time.** Configuration
   declares a ceiling; it does not bypass one. A source whose tools exceed its
   declared ceiling fails to build, exactly as before.
2. **`high` is not expressible.** The manifest validator rejects it, as do
   `Settings` and `ActionPolicy`. Three enforcement points for one rule is
   deliberate: each is a different way in, and a safety property with a single
   check is one bug away from being false.
3. **It is in version control.** A file that raises a ceiling shows up in review,
   which a constant buried in a composition root does not.

Two supporting decisions:

- **`aic doctor`.** Probes every source and reports per capability, translating
  an IAM denial into the action that would fix it. The difference between a tool
  that is easy to adopt and one that is not is usually what happens when it is
  misconfigured — a stack trace at the worst moment, or a sentence that says what
  to do. It exits non-zero, so it doubles as a deployment gate.
- **A named-but-missing manifest is an error; discovering nothing is not.**
  "Nothing configured yet" is a legitimate state and gets the fixture source. A
  path the caller *typed* and that does not exist is a typo, and quietly
  investigating a fixture environment when someone asked for their production
  account is the worst available outcome.

## Consequences

**Accepted:** the safety argument is now longer to make. "It is hard-coded" was a
one-line proof; "it is declared in a reviewed file and enforced in three places"
requires reading this ADR. The property is the same; the explanation costs more.

**Accepted:** a manifest is another format to version and migrate.

**Gained:** pointing this at a real AWS account is a YAML file and an IAM policy.
The `InfrastructureClient` protocol — six read-only methods — is the entire
contract a new source has to satisfy.

**Gained:** `aic doctor` turns the most common adoption failure, a half-configured
IAM policy, into a list of actions to paste.

## Revisit if

Sources need to be added at runtime rather than at boot, or an installation needs
several sources of the same type at once. Both push toward a registry keyed by
name, which the manifest shape can absorb but the current single-client
construction cannot.
