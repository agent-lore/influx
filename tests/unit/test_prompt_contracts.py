"""Tests for the shipped tier1_enrich and tier3_extract prompt contracts.

US-004: validate that the shipped prompt bodies declare exactly the required
template variables and that ``load_prompt`` / ``validate_prompt_variables``
accept them (and reject tampered variants).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from influx.errors import PromptValidationError
from influx.prompts import load_prompt, validate_prompt_variables

# Path to the canonical example config shipped with the project.
_EXAMPLE_TOML = Path(__file__).resolve().parents[2] / "influx.example.toml"


@pytest.fixture()
def tier1_enrich_text() -> str:
    """Load the shipped tier1_enrich prompt from influx.example.toml."""
    with _EXAMPLE_TOML.open("rb") as fh:
        raw = tomllib.load(fh)
    return raw["prompts"]["tier1_enrich"]["text"]


@pytest.fixture()
def tier3_extract_text() -> str:
    """Load the shipped tier3_extract prompt from influx.example.toml."""
    with _EXAMPLE_TOML.open("rb") as fh:
        raw = tomllib.load(fh)
    return raw["prompts"]["tier3_extract"]["text"]


# ── Tier 1 enrich prompt contract ────────────────────────────────────


class TestTier1EnrichPromptContract:
    """Shipped tier1_enrich uses exactly {title}, {abstract},
    {profile_summary}."""

    def test_load_prompt_returns_text(self, tier1_enrich_text: str) -> None:
        result = load_prompt(text=tier1_enrich_text)
        assert result == tier1_enrich_text

    def test_shipped_prompt_passes_validation(self, tier1_enrich_text: str) -> None:
        validate_prompt_variables("tier1_enrich", tier1_enrich_text)

    def test_tampered_extra_variable_rejected(self, tier1_enrich_text: str) -> None:
        tampered = tier1_enrich_text + " {extra_var}"
        with pytest.raises(PromptValidationError, match="unknown variable"):
            validate_prompt_variables("tier1_enrich", tampered)

    def test_tampered_missing_variable_rejected(self, tier1_enrich_text: str) -> None:
        tampered = tier1_enrich_text.replace("{profile_summary}", "hardcoded")
        with pytest.raises(PromptValidationError, match="missing required variable"):
            validate_prompt_variables("tier1_enrich", tampered)


# ── Tier 3 extract prompt contract ───────────────────────────────────


class TestTier3ExtractPromptContract:
    """Shipped tier3_extract uses exactly {title}, {full_text}."""

    def test_load_prompt_returns_text(self, tier3_extract_text: str) -> None:
        result = load_prompt(text=tier3_extract_text)
        assert result == tier3_extract_text

    def test_shipped_prompt_passes_validation(self, tier3_extract_text: str) -> None:
        validate_prompt_variables("tier3_extract", tier3_extract_text)

    def test_tampered_extra_variable_rejected(self, tier3_extract_text: str) -> None:
        tampered = tier3_extract_text + " {sneaky}"
        with pytest.raises(PromptValidationError, match="unknown variable"):
            validate_prompt_variables("tier3_extract", tampered)

    def test_tampered_missing_variable_rejected(self, tier3_extract_text: str) -> None:
        tampered = tier3_extract_text.replace("{full_text}", "hardcoded")
        with pytest.raises(PromptValidationError, match="missing required variable"):
            validate_prompt_variables("tier3_extract", tampered)
