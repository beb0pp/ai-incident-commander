"""Turn a Pydantic model into a JSON Schema the structured-outputs API accepts.

Structured outputs support a deliberate subset of JSON Schema: no numeric or
string constraints, no recursion, and every object must set
``additionalProperties: false``. Pydantic happily emits the unsupported
keywords, so we strip them here and let Pydantic re-apply them client-side when
it validates the model's reply. That keeps the constraint declared exactly once,
in the domain model, while still sending the API something it will accept.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

#: Keywords the API rejects or ignores. Pydantic re-checks all of them locally.
UNSUPPORTED_KEYWORDS: frozenset[str] = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "default",
    }
)

#: ``format`` values the API understands. Anything else is dropped.
SUPPORTED_FORMATS: frozenset[str] = frozenset(
    {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "uri",
        "ipv4",
        "ipv6",
        "uuid",
    }
)


def to_strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return a structured-outputs-compatible schema for ``model``."""
    sanitized: dict[str, Any] = _sanitize(model.model_json_schema())
    return sanitized


def _sanitize(node: Any) -> Any:
    if isinstance(node, list):
        return [_sanitize(item) for item in node]
    if not isinstance(node, dict):
        return node

    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key in UNSUPPORTED_KEYWORDS:
            continue
        if key == "format" and value not in SUPPORTED_FORMATS:
            continue
        cleaned[key] = _sanitize(value)

    if cleaned.get("type") == "object" or "properties" in cleaned:
        cleaned.setdefault("additionalProperties", False)
        cleaned.setdefault("properties", {})

    return cleaned
