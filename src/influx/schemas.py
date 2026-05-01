"""Pydantic models for the LLM filter pipeline (FR-FLT-3).

``FilterResult`` and ``FilterResponse`` validate JSON-mode LLM output
with bounded score and tag-list constraints.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

_TIER3_MAX_CHARS = 500
_FILTER_MAX_TAGS = 5


def _trim_and_truncate(values: list[str]) -> list[str]:
    """Trim whitespace and truncate each element to 500 chars (FR-ENR-5).

    Raises ``ValueError`` when an element is not a string so Pydantic
    surfaces the failure as ``ValidationError`` rather than letting an
    ``AttributeError`` escape — the latter bypasses ``LCMAError``-only
    callers and aborts the whole run (staging incident 2026-05-01).
    """
    out: list[str] = []
    for v in values:
        if not isinstance(v, str):
            raise ValueError(
                f"List element must be a string, got {type(v).__name__}: {v!r:.100}"
            )
        out.append(v.strip()[:_TIER3_MAX_CHARS])
    return out


def _check_non_empty(values: list[str]) -> list[str]:
    """Reject empty/whitespace-only elements after trim (FR-ENR-5)."""
    for v in values:
        if not v:
            msg = "List elements must be non-empty after trimming"
            raise ValueError(msg)
    return values


class Tier3Extraction(BaseModel):
    """Tier-3 deep extraction output validated against FR-ENR-5 (PRD 07 §5.3).

    Constraints:
    - ``claims`` length must be in ``[1, 10]`` inclusive.
    - ``datasets``, ``builds_on``, ``open_questions``, ``potential_connections``
      lengths must be in ``[0, 10]`` inclusive.
    - All string elements are trimmed and truncated to 500 characters on ingest.
    - Empty/whitespace-only elements fail validation.
    """

    claims: list[str] = Field(min_length=1, max_length=10)
    datasets: list[str] = Field(default_factory=list, max_length=10)
    builds_on: list[str] = Field(default_factory=list, max_length=10)
    open_questions: list[str] = Field(default_factory=list, max_length=10)
    potential_connections: list[str] = Field(default_factory=list, max_length=10)

    @field_validator(
        "claims",
        "datasets",
        "builds_on",
        "open_questions",
        "potential_connections",
        mode="before",
    )
    @classmethod
    def trim_and_truncate(cls, v: list[str]) -> list[str]:
        """Trim whitespace and truncate to 500 chars per element."""
        return _trim_and_truncate(v)

    @field_validator(
        "claims",
        "datasets",
        "builds_on",
        "open_questions",
        "potential_connections",
        mode="after",
    )
    @classmethod
    def check_non_empty(cls, v: list[str]) -> list[str]:
        """Reject empty/whitespace-only elements."""
        return _check_non_empty(v)


class Tier1Enrichment(BaseModel):
    """Tier-1 enrichment output validated against FR-ENR-4 (PRD 07 §5.2).

    Constraints:
    - ``contributions`` length must be in ``[1, 6]`` inclusive.
    """

    contributions: list[str] = Field(min_length=1, max_length=6)
    method: str
    result: str
    relevance: str


class FilterResult(BaseModel):
    """One scored item from the LLM filter response (FR-FLT-3).

    Constraints:
    - ``score`` must be in ``[1, 10]`` inclusive.
    - ``tags`` list length must be in ``[0, 5]`` inclusive.
    """

    id: str
    score: int = Field(ge=1, le=10)
    tags: list[str] = Field(default_factory=list, max_length=_FILTER_MAX_TAGS)
    reason: str

    @field_validator("tags", mode="before")
    @classmethod
    def cap_tags(cls, v: object) -> object:
        """Keep LLM tag output within the documented filter contract."""
        if isinstance(v, list):
            return v[:_FILTER_MAX_TAGS]
        return v


class FilterResponse(BaseModel):
    """Top-level wrapper for a batch of filter results (FR-FLT-3)."""

    results: list[FilterResult]


# ── OpenAI structured-outputs response_format builder ────────────────


# OpenAI's structured-outputs ``json_schema`` mode rejects several
# JSON-Schema keywords that Pydantic generates (length / range bounds,
# patterns, defaults, etc.); per-element type enforcement is what we
# care about here, so we strip the rest.
_UNSUPPORTED_KEYWORDS: tuple[str, ...] = (
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "pattern",
    "format",
    "default",
    "examples",
    "title",
)


def _harden_for_openai_strict(node: Any) -> Any:
    """Mutate *node* in place so a Pydantic JSON Schema satisfies OpenAI's
    structured-outputs ``strict`` requirements.

    Strict mode requires every object schema to set
    ``additionalProperties: false`` and to list every property in
    ``required``; any unsupported keyword (length/range bounds, etc.)
    causes the API call to fail with a 400 before the model runs.

    The function descends into nested ``properties``, ``items``,
    ``$defs``/``definitions``, ``anyOf``/``oneOf``/``allOf``.
    """
    if not isinstance(node, dict):
        return node

    for key in _UNSUPPORTED_KEYWORDS:
        node.pop(key, None)

    node_type = node.get("type")
    if node_type == "object":
        node["additionalProperties"] = False
        props = node.get("properties")
        if isinstance(props, dict):
            node["required"] = list(props.keys())
            for child in props.values():
                _harden_for_openai_strict(child)
    elif node_type == "array":
        items = node.get("items")
        if items is not None:
            _harden_for_openai_strict(items)

    for combinator in ("anyOf", "oneOf", "allOf"):
        children = node.get(combinator)
        if isinstance(children, list):
            for child in children:
                _harden_for_openai_strict(child)

    for defs_key in ("$defs", "definitions"):
        defs = node.get(defs_key)
        if isinstance(defs, dict):
            for child in defs.values():
                _harden_for_openai_strict(child)

    return node


def openai_strict_response_format(
    schema_class: type[BaseModel],
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Return an OpenAI ``response_format`` dict pinning *schema_class*.

    When passed in a chat-completions request body alongside a model
    that supports structured outputs, this forces the model to emit
    JSON conforming exactly to the Pydantic schema — list-of-string
    fields cannot regress to list-of-dict, missing fields cannot be
    omitted, etc.  Out-of-shape responses are rejected by the API
    before the model finishes, surfaced as HTTP 400.

    *name* defaults to the Pydantic class name with the OpenAI-imposed
    32-character cap applied.
    """
    schema = schema_class.model_json_schema()
    _harden_for_openai_strict(schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": (name or schema_class.__name__)[:64],
            "strict": True,
            "schema": schema,
        },
    }
