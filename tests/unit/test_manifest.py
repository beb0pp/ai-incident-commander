"""The declarative manifest.

Adoption is supposed to be "write a YAML file". These tests cover the part of
that promise that can go wrong quietly: a manifest that resolves paths against
the wrong directory, or one that widens the safety ceiling without saying so.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aic.domain.models import RiskLevel
from aic.manifest import (
    AwsSource,
    Manifest,
    SimulatedSource,
    find_manifest,
    load_manifest,
)


def write(tmp_path: Path, body: str, name: str = "aic.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestDefaults:
    def test_discovering_nothing_falls_back_to_the_simulated_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'Nothing configured yet' is a state, not a crash on first run."""
        monkeypatch.chdir(tmp_path)
        manifest = load_manifest()

        assert len(manifest.sources) == 1
        assert isinstance(manifest.sources[0], SimulatedSource)

    def test_a_named_manifest_that_is_missing_is_a_loud_error(self, tmp_path: Path) -> None:
        """Quietly investigating a fixture when someone asked for production
        would be the worst possible way to handle a typo."""
        with pytest.raises(FileNotFoundError, match="aic init"):
            load_manifest(tmp_path / "typo.yaml")

    def test_empty_file_is_treated_as_no_configuration(self, tmp_path: Path) -> None:
        manifest = load_manifest(write(tmp_path, "\n# nothing here\n"))
        assert [s.type for s in manifest.sources] == ["simulated"]

    def test_default_ceiling_is_read_only(self) -> None:
        assert Manifest().effective_ceiling is RiskLevel.READ_ONLY


class TestParsing:
    def test_aws_source_is_parsed(self, tmp_path: Path) -> None:
        manifest = load_manifest(
            write(
                tmp_path,
                """
                sources:
                  - type: aws
                    region: sa-east-1
                    profile: prophub-readonly
                    role_arn: arn:aws:iam::123456789012:role/reader
                """,
            )
        )
        source = manifest.sources[0]
        assert isinstance(source, AwsSource)
        assert source.region == "sa-east-1"
        assert source.profile == "prophub-readonly"
        assert source.role_arn.endswith("role/reader")

    def test_unknown_source_type_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            load_manifest(write(tmp_path, "sources:\n  - type: gcp\n    region: x\n"))

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        """A typo should fail loudly rather than be silently ignored."""
        with pytest.raises(ValidationError):
            load_manifest(
                write(tmp_path, "sources:\n  - type: aws\n    regoin: sa-east-1\n")
            )

    def test_a_non_mapping_document_is_a_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must contain a YAML mapping"):
            load_manifest(write(tmp_path, "- just\n- a\n- list\n"))


class TestRunbookPaths:
    def test_relative_paths_resolve_against_the_manifest_not_the_cwd(
        self, tmp_path: Path
    ) -> None:
        """Otherwise `aic doctor` works from the repo root and the service does not."""
        (tmp_path / "ops").mkdir()
        manifest = load_manifest(
            write(tmp_path, "runbooks:\n  - path: ./ops\n")
        )
        assert manifest.runbooks[0].path == tmp_path / "ops"

    def test_absolute_paths_are_left_alone(self, tmp_path: Path) -> None:
        absolute = (tmp_path / "elsewhere").resolve()
        manifest = load_manifest(write(tmp_path, f"runbooks:\n  - path: {absolute}\n"))
        assert manifest.runbooks[0].path == absolute


class TestSafetyCeiling:
    def test_a_source_may_not_declare_high(self, tmp_path: Path) -> None:
        """Configuration can declare a ceiling; it cannot abolish one."""
        with pytest.raises(ValidationError, match="defeats the approval model"):
            load_manifest(
                write(tmp_path, "sources:\n  - type: simulated\n    max_risk: high\n")
            )

    def test_medium_is_allowed_and_becomes_the_effective_ceiling(
        self, tmp_path: Path
    ) -> None:
        manifest = load_manifest(
            write(
                tmp_path,
                "sources:\n"
                "  - type: simulated\n"
                "  - type: aws\n"
                "    region: sa-east-1\n"
                "    max_risk: medium\n",
            )
        )
        assert manifest.effective_ceiling is RiskLevel.MEDIUM


class TestDiscovery:
    def test_finds_a_manifest_in_a_parent_directory(self, tmp_path: Path) -> None:
        write(tmp_path, "sources:\n  - type: simulated\n")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_manifest(nested) == tmp_path / "aic.yaml"

    def test_yml_extension_is_accepted(self, tmp_path: Path) -> None:
        write(tmp_path, "sources:\n  - type: simulated\n", name="aic.yml")
        assert find_manifest(tmp_path) == tmp_path / "aic.yml"

    def test_returns_none_when_there_is_nothing_to_find(self, tmp_path: Path) -> None:
        assert find_manifest(tmp_path) is None
