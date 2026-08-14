"""The `aic.yaml` manifest: what this installation is connected to.

Adopting the platform should mean writing configuration, not writing a subclass.
This is the file that makes that true — sources, runbook locations, and the risk
ceiling each source is allowed to operate under.

The risk ceiling is declared **per source**, not globally, and that is the point
worth defending. Making the tool surface configurable would otherwise dissolve
the guarantee the whole design rests on: that an investigation can read and
never write. Here the ceiling is data instead of a hard-coded constant, the
registry still refuses anything above it at construction time, and a manifest
that tries to raise a source above `read_only` has to say so out loud in a file
that lives in version control and shows up in review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aic.domain.models import RiskLevel

DEFAULT_MANIFEST_NAMES = ("aic.yaml", "aic.yml")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SimulatedSource(_Section):
    """The bundled fixture environment. No credentials, no account, no cost."""

    type: Literal["simulated"] = "simulated"
    max_risk: RiskLevel = RiskLevel.READ_ONLY


class AwsSource(_Section):
    """A live AWS account, read through boto3."""

    type: Literal["aws"]
    region: str = Field(description="e.g. 'sa-east-1'.")
    profile: str | None = Field(
        default=None,
        description=(
            "Named profile from ~/.aws/config. Leave unset to use the default "
            "credential chain — which is what a task role or instance profile "
            "populates, and what you want in a deployment."
        ),
    )
    role_arn: str | None = Field(
        default=None,
        description="Role to assume. Set this when watching another account.",
    )
    max_risk: RiskLevel = RiskLevel.READ_ONLY
    max_attempts: int = Field(default=5, ge=1, le=10)


Source = Annotated[AwsSource | SimulatedSource, Field(discriminator="type")]


class RunbookLocation(_Section):
    """Where operational procedures live. Markdown, chunked on `##` headings."""

    path: Path


class Manifest(_Section):
    """The whole declarative configuration."""

    sources: list[Source] = Field(
        default_factory=lambda: cast("list[Source]", [SimulatedSource()])
    )
    runbooks: list[RunbookLocation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_unsafe_ceilings(self) -> Manifest:
        """`high` is not a ceiling anyone gets to configure.

        Enforced in three places now — `Settings`, `ActionPolicy`, and here.
        That is deliberate: each is a different way in, and a safety property
        with one enforcement point is a safety property with one bug between it
        and being false.
        """
        for source in self.sources:
            if source.max_risk is RiskLevel.HIGH:
                raise ValueError(
                    f"source {source.type!r} declares max_risk 'high'; a source that "
                    "can take destructive action unattended defeats the approval "
                    "model. The ceiling is 'medium' at most."
                )
        return self

    @property
    def effective_ceiling(self) -> RiskLevel:
        """The highest ceiling any source declares."""
        return max((s.max_risk for s in self.sources), key=lambda r: r.rank)


def find_manifest(start: Path | None = None) -> Path | None:
    """Look for `aic.yaml` in a directory, then upward toward the filesystem root."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for name in DEFAULT_MANIFEST_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_manifest(path: Path | str | None = None) -> Manifest:
    """Load the manifest.

    The two "not found" cases are deliberately different. Discovering nothing
    means "no environment configured yet", and the honest response is the
    fixture source rather than a stack trace on first run. But a path the caller
    *named* and that does not exist is a typo, and silently investigating a
    fixture environment when someone asked for their production account is the
    worst possible way to handle it.
    """
    if path is None:
        resolved = find_manifest()
        if resolved is None:
            return Manifest()
    else:
        resolved = Path(path)
        if not resolved.is_file():
            raise FileNotFoundError(
                f"no manifest at {resolved}. Run `aic init` to write a starter one, "
                "or omit the path to search upward from the working directory."
            )

    raw: Any = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if raw is None:
        return Manifest()
    if not isinstance(raw, dict):
        raise ValueError(f"{resolved} must contain a YAML mapping, got {type(raw).__name__}")

    manifest = Manifest.model_validate(raw)

    # Runbook paths are written relative to the manifest, not to the process's
    # working directory — otherwise `aic doctor` works and the service does not.
    base = resolved.parent
    manifest.runbooks = [
        RunbookLocation(path=location.path if location.path.is_absolute() else base / location.path)
        for location in manifest.runbooks
    ]
    return manifest


EXAMPLE = """\
# aic.yaml — what this installation is connected to.
#
# Every source declares its own risk ceiling. Investigation tools are read-only
# by construction: the registry refuses at startup to hold anything above the
# ceiling, so this file is the only place that decision is made, and it is a
# decision that shows up in code review.

sources:
  # The bundled fixture environment. Works with no credentials.
  - type: simulated

  # A live AWS account. Uncomment and adjust.
  # - type: aws
  #   region: sa-east-1
  #   # Omit `profile` in a deployment: the default credential chain picks up
  #   # the ECS task role or EC2 instance profile, with nothing to store.
  #   profile: prophub-readonly
  #   # Set `role_arn` when the account you watch is not the account you run in.
  #   # role_arn: arn:aws:iam::123456789012:role/incident-commander-readonly

runbooks:
  - path: ./docs/runbooks
"""
