"""Tests for influx.prompts — prompt loading and template-variable validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from influx.errors import ConfigError, PromptValidationError
from influx.prompts import (
    REQUIRED_VARIABLES,
    extract_variables,
    load_prompt,
    validate_prompt_variables,
)

# ── extract_variables ─────────────────────────────────────────────────


class TestExtractVariables:
    def test_simple_variables(self) -> None:
        assert extract_variables("Hello {name}, your {item} is ready") == {
            "name",
            "item",
        }

    def test_no_variables(self) -> None:
        assert extract_variables("No variables here") == set()

    def test_escaped_braces_ignored(self) -> None:
        assert extract_variables("Use {{literal}} braces and {real}") == {"real"}

    def test_duplicate_variable(self) -> None:
        assert extract_variables("{x} and {x} again") == {"x"}

    def test_adjacent_variables(self) -> None:
        assert extract_variables("{a}{b}") == {"a", "b"}


# ── load_prompt ───────────────────────────────────────────────────────


class TestLoadPrompt:
    def test_inline_text(self) -> None:
        result = load_prompt(text="hello {world}")
        assert result == "hello {world}"

    def test_both_text_and_path_raises(self) -> None:
        with pytest.raises(ConfigError, match="both 'text' and 'path'"):
            load_prompt(text="hello", path="some/file.txt")

    def test_neither_text_nor_path_raises(self) -> None:
        with pytest.raises(ConfigError, match="neither 'text' nor 'path'"):
            load_prompt()

    def test_path_absolute(self, tmp_path: Path) -> None:
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("template {var}", encoding="utf-8")
        result = load_prompt(path=str(prompt_file))
        assert result == "template {var}"

    def test_path_relative_resolved_against_config_dir(self, tmp_path: Path) -> None:
        prompt_file = tmp_path / "prompts" / "filter.txt"
        prompt_file.parent.mkdir(parents=True)
        prompt_file.write_text("filter {profile_description}", encoding="utf-8")
        result = load_prompt(path="prompts/filter.txt", config_dir=tmp_path)
        assert result == "filter {profile_description}"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="Could not read prompt file"):
            load_prompt(path="nonexistent.txt", config_dir=tmp_path)


# ── validate_prompt_variables ─────────────────────────────────────────


class TestValidatePromptVariablesFilter:
    """Validation for the ``filter`` prompt key."""

    def test_valid_filter(self) -> None:
        template = (
            "Desc: {profile_description}, "
            "neg: {negative_examples}, "
            "min: {min_score_in_results}"
        )
        validate_prompt_variables("filter", template)

    def test_unknown_variable_raises(self) -> None:
        template = (
            "{profile_description} {negative_examples} {min_score_in_results} {extra}"
        )
        with pytest.raises(PromptValidationError, match="unknown variable.*extra"):
            validate_prompt_variables("filter", template)

    def test_missing_variable_raises(self) -> None:
        template = "{profile_description} {negative_examples}"
        with pytest.raises(
            PromptValidationError,
            match="missing required variable.*min_score_in_results",
        ):
            validate_prompt_variables("filter", template)


class TestValidatePromptVariablesTier1Enrich:
    """Validation for the ``tier1_enrich`` prompt key."""

    def test_valid_tier1_enrich(self) -> None:
        template = "{title} {abstract} {profile_summary}"
        validate_prompt_variables("tier1_enrich", template)

    def test_unknown_variable_raises(self) -> None:
        template = "{title} {abstract} {profile_summary} {bogus}"
        with pytest.raises(PromptValidationError, match="unknown variable.*bogus"):
            validate_prompt_variables("tier1_enrich", template)

    def test_missing_variable_raises(self) -> None:
        template = "{title}"
        with pytest.raises(PromptValidationError, match="missing required variable"):
            validate_prompt_variables("tier1_enrich", template)


class TestValidatePromptVariablesTier3Extract:
    """Validation for the ``tier3_extract`` prompt key."""

    def test_valid_tier3_extract(self) -> None:
        template = "{title} {full_text}"
        validate_prompt_variables("tier3_extract", template)

    def test_unknown_variable_raises(self) -> None:
        template = "{title} {full_text} {oops}"
        with pytest.raises(PromptValidationError, match="unknown variable.*oops"):
            validate_prompt_variables("tier3_extract", template)

    def test_missing_variable_raises(self) -> None:
        template = "{full_text}"
        with pytest.raises(
            PromptValidationError, match="missing required variable.*title"
        ):
            validate_prompt_variables("tier3_extract", template)


class TestValidateUnknownKey:
    def test_unknown_prompt_key_raises(self) -> None:
        with pytest.raises(ConfigError, match="Unknown prompt key"):
            validate_prompt_variables("nonexistent_key", "some text")


class TestRequiredVariablesMapping:
    """Ensure the REQUIRED_VARIABLES mapping matches the spec."""

    def test_filter_requires(self) -> None:
        assert REQUIRED_VARIABLES["filter"] == frozenset(
            {"profile_description", "negative_examples", "min_score_in_results"}
        )

    def test_tier1_enrich_requires(self) -> None:
        assert REQUIRED_VARIABLES["tier1_enrich"] == frozenset(
            {"title", "abstract", "profile_summary"}
        )

    def test_tier3_extract_requires(self) -> None:
        assert REQUIRED_VARIABLES["tier3_extract"] == frozenset({"title", "full_text"})
