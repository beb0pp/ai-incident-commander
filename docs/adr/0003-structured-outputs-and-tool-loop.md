# ADR 0003 — Structured outputs everywhere; the tool loop stays ours

**Status:** Accepted · **Date:** 2026-08-13

## Context

Agents hand each other data, not prose. Two ways to get there: ask for JSON in
the prompt and parse defensively, or constrain the response with the API's
structured-outputs feature.

Separately, the Infrastructure Agent needs a tool-calling loop, and the Anthropic
SDK ships a tool runner that drives one.

## Decision

### Structured outputs, with the wire schema derived from the validator

Every agent response is a Pydantic model. `aic/llm/schema.py` derives the JSON
Schema sent to the API from that same model, and `model_validate_json` re-checks
the reply. A constraint like "confidence is between 0 and 1" is declared exactly
once, in the domain.

Structured outputs accept a subset of JSON Schema: no numeric or string
constraints, and every object must set `additionalProperties: false`. Rather than
weaken the domain model to fit the wire format, the sanitizer strips the
unsupported keywords on the way out and lets Pydantic re-apply them on the way
back. This is what the SDK's `messages.parse()` helper does internally; doing it
explicitly keeps `effort` and `format` on the same `output_config` call and makes
the behaviour inspectable and testable.

There is no "regex the JSON out of the prose" path anywhere in this codebase.

### A hand-written tool loop

`AnthropicLLMClient.tool_loop` implements request → `tool_use` → execute →
`tool_result` → repeat directly.

The loop is where the interesting behaviour lives: the per-call audit trail that
later becomes evidence, `pause_turn` resumption, an iteration budget whose
exhaustion is reported as a real error rather than silently truncating the
investigation, and the seam where an approval gate on a mutating tool would sit.
It is also the natural place to enforce that all results for one assistant turn
go back in a single user message.

Using the SDK runner would additionally take a dependency on a beta surface for
the most safety-relevant code path in the project.

## Consequences

**Accepted:** the sanitizer is a maintenance surface. If the API's supported
subset changes, `UNSUPPORTED_KEYWORDS` needs updating. It is tested directly, and
the failure mode is a loud 400 rather than silent data loss.

**Accepted:** roughly 60 lines of loop we maintain instead of one SDK call.

**Gained:** a malformed model response is a `ValidationError` at the boundary,
naming the field, instead of a `KeyError` three layers downstream.

**Gained:** the `LLMClient` protocol has exactly two methods, which is what makes
`ScriptedLLMClient` a complete and honest test double rather than a partial mock.
