"""Pydantic models for the LLM filter pipeline (FR-FLT-3).

``FilterResult`` and ``FilterResponse`` validate JSON-mode LLM output
with bounded score and tag-list constraints.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator

_log = logging.getLogger(__name__)

_TIER3_MAX_CHARS = 500
_FILTER_MAX_TAGS = 5

# Single source of truth for the upper bound on Tier-3 ``claims``,
# ``datasets`` and ``builds_on`` list lengths (FR-ENR-5, issue #81).
# Bumped from 10 → 30 after structured-output models routinely returned
# 14-39 items, breaking validation.  Both the schema's ``max_length`` and
# the constant-derived cap reminder appended to the rendered Tier-3
# prompt read from this value, so a future bump propagates automatically.
#
# Since issue #288 the cap is enforced by *truncation* rather than
# rejection, so its value is no longer load-bearing for whether an
# extraction survives — only for how much of its tail is kept.
TIER3_LIST_MAX = 30

# The same bound for the two narrative fields, which have always been
# held to a shorter list than the evidence fields.
TIER3_SHORT_LIST_MAX = 10

# Per-field list-length caps, keyed by Pydantic field name.  Consulted by
# the ``mode="before"`` validator via ``ValidationInfo.field_name``.
_TIER3_FIELD_CAPS: dict[str, int] = {
    "claims": TIER3_LIST_MAX,
    "datasets": TIER3_LIST_MAX,
    "builds_on": TIER3_LIST_MAX,
    "open_questions": TIER3_SHORT_LIST_MAX,
    "potential_connections": TIER3_SHORT_LIST_MAX,
}


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


def _cap_list_length(values: list[Any], *, field_name: str) -> list[Any]:
    """Truncate *values* to the cap for *field_name* (FR-ENR-5, issue #288).

    Over-length lists used to fail ``max_length`` and take the whole
    extraction down with them.  Because ``validate`` is a counted repair
    stage, three such failures applied ``influx:tier3-terminal`` and the
    deep extraction was lost for good — observed in production on
    ``9eee59f3`` ("Context-Aware RL for Agentic and Multimodal LLMs"),
    whose model output was otherwise entirely well-formed.

    Truncating instead follows the precedent already set for element
    *content* by ``_trim_and_truncate``: over-long input is bounded, not
    rejected.  The prompt asks for items "prioritised by relevance", so
    the discarded tail is the least valuable part of the list.

    Raising the cap again is not a fix — it has been raised twice (#81,
    #186) and the largest observed overflow since is 79 items against a
    prompt that already states the limit.
    """
    cap = _TIER3_FIELD_CAPS.get(field_name)
    if cap is None or len(values) <= cap:
        return values
    _log.info(
        "tier3 list truncated field=%s returned=%d cap=%d dropped=%d",
        field_name,
        len(values),
        cap,
        len(values) - cap,
    )
    return values[:cap]


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
    - ``claims`` must have at least 1 element; an empty extraction is a
      real failure and still rejects (AC-07-C).
    - ``claims``, ``datasets`` and ``builds_on`` are **truncated** to
      ``TIER3_LIST_MAX`` items on ingest; ``open_questions`` and
      ``potential_connections`` to ``TIER3_SHORT_LIST_MAX`` (issue #288).
      Over-length lists used to fail validation and discard the entire
      extraction — see ``_cap_list_length`` for why that was worse than
      losing the tail.
    - All string elements are trimmed and truncated to 500 characters on ingest.
    - Empty/whitespace-only elements fail validation.

    The declared ``max_length`` bounds are therefore unreachable in
    normal operation.  They are kept deliberately, as a post-condition
    on the truncation step and as the documented shape of the model: if
    capping ever regresses, validation fails loudly instead of silently
    admitting unbounded lists.  ``maxItems`` is stripped from the
    outbound OpenAI strict schema by ``_harden_for_openai_strict``, so
    keeping them costs nothing at the API boundary.
    """

    claims: list[str] = Field(min_length=1, max_length=TIER3_LIST_MAX)
    datasets: list[str] = Field(default_factory=list, max_length=TIER3_LIST_MAX)
    builds_on: list[str] = Field(default_factory=list, max_length=TIER3_LIST_MAX)
    open_questions: list[str] = Field(
        default_factory=list, max_length=TIER3_SHORT_LIST_MAX
    )
    potential_connections: list[str] = Field(
        default_factory=list, max_length=TIER3_SHORT_LIST_MAX
    )

    @field_validator(
        "claims",
        "datasets",
        "builds_on",
        "open_questions",
        "potential_connections",
        mode="before",
    )
    @classmethod
    def trim_and_truncate(cls, v: object, info: ValidationInfo) -> object:
        """Cap list length, then trim and truncate each element.

        Length capping runs *first* so a malformed item in the discarded
        tail cannot fail an extraction we are keeping.  Non-list input is
        passed through untouched for Pydantic to reject, so the failure
        stays a ``ValidationError`` rather than a ``TypeError`` escaping
        the ``LCMAError``-only callers (staging incident 2026-05-01).
        """
        if not isinstance(v, list):
            return v
        capped = _cap_list_length(v, field_name=info.field_name or "")
        return _trim_and_truncate(capped)

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
