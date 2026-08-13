"""The structured-output schema sanitizer.

Structured outputs accept a subset of JSON Schema. The sanitizer strips what
the API rejects on the way out; Pydantic re-applies it on the way back. Both
halves of that contract are asserted here.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aic.domain.models import ActionPlan, ProposedAction, Signal
from aic.llm.schema import UNSUPPORTED_KEYWORDS, to_strict_json_schema


class TestSchemaSanitizer:
    class Constrained(BaseModel):
        model_config = ConfigDict(extra="forbid")

        score: float = Field(ge=0.0, le=1.0)
        name: str = Field(min_length=1, max_length=50, default="x")
        tags: list[str] = Field(default_factory=list, max_length=5)

    def test_unsupported_constraints_are_stripped(self) -> None:
        schema = to_strict_json_schema(self.Constrained)
        rendered = repr(schema)
        for keyword in UNSUPPORTED_KEYWORDS:
            assert f"'{keyword}'" not in rendered, keyword

    def test_objects_forbid_additional_properties(self) -> None:
        schema = to_strict_json_schema(self.Constrained)
        assert schema["additionalProperties"] is False

    def test_field_names_survive(self) -> None:
        schema = to_strict_json_schema(self.Constrained)
        assert set(schema["properties"]) == {"score", "name", "tags"}

    def test_nested_models_are_sanitized_too(self) -> None:
        class Outer(BaseModel):
            model_config = ConfigDict(extra="forbid")

            inner: TestSchemaSanitizer.Constrained

        rendered = repr(to_strict_json_schema(Outer))
        assert "'minimum'" not in rendered
        assert "'maximum'" not in rendered

    def test_constraints_are_still_enforced_client_side(self) -> None:
        """Stripping them from the wire schema must not weaken validation."""
        with pytest.raises(ValidationError):
            self.Constrained.model_validate({"score": 2.0})

    def test_domain_models_produce_valid_schemas(self) -> None:
        for model in (ProposedAction, ActionPlan, Signal):
            schema = to_strict_json_schema(model)
            assert schema["type"] == "object"
            assert schema["additionalProperties"] is False
