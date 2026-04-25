"""Unit tests for dedup-query composition (AC-05-B golden cases)."""

from __future__ import annotations

import pytest

from influx.dedup import compose_dedup_query, first_sentence


class TestFirstSentence:
    """AC-05-B golden cases for first_sentence extraction."""

    def test_mr_smith_abbreviation(self) -> None:
        """Abbreviation 'Mr.' is NOT a sentence end; 'home.' IS."""
        text = "Mr. Smith went home. He was tired."
        assert first_sentence(text) == "Mr. Smith went home"

    def test_question_mark_at_eof_is_sentence_end(self) -> None:
        """A '?' followed by EOF IS a sentence end."""
        text = "Is this paper relevant?"
        assert first_sentence(text) == "Is this paper relevant"

    def test_exclamation_at_eof_is_sentence_end(self) -> None:
        """A '!' followed by EOF IS a sentence end."""
        text = "This is amazing!"
        assert first_sentence(text) == "This is amazing"

    def test_period_at_eof_is_sentence_end(self) -> None:
        """A '.' followed by EOF IS a sentence end."""
        text = "Neural networks converge."
        assert first_sentence(text) == "Neural networks converge"

    def test_no_terminator_returns_whole_string(self) -> None:
        """No sentence terminator → entire string returned."""
        text = "A text with no ending punctuation"
        assert first_sentence(text) == text

    def test_no_terminator_capped_at_200(self) -> None:
        """No terminator → capped at 200 characters."""
        text = "a" * 250
        result = first_sentence(text)
        assert len(result) == 200
        assert result == "a" * 200

    def test_trimmed_whitespace(self) -> None:
        """Leading/trailing whitespace is trimmed."""
        text = "  Hello world.  "
        assert first_sentence(text) == "Hello world"

    def test_eg_abbreviation_skipped(self) -> None:
        """'e.g.' is not treated as a sentence end."""
        text = "Use e.g. Python or Rust. They are great."
        assert first_sentence(text) == "Use e.g. Python or Rust"

    def test_multiple_abbreviations(self) -> None:
        """Multiple abbreviations before the real sentence end."""
        text = "Dr. J. Smith published results. The paper was accepted."
        assert first_sentence(text) == "Dr. J. Smith published results"

    def test_exclamation_mid_sentence(self) -> None:
        """Exclamation mark followed by space is a sentence end."""
        text = "What a discovery! More work is needed."
        assert first_sentence(text) == "What a discovery"


class TestComposeDedupQuery:
    """Tests for compose_dedup_query (FR-MCP-3)."""

    def test_no_abstract_returns_title_only(self) -> None:
        """AC-05-B: no abstract → title only."""
        result = compose_dedup_query("Attention Is All You Need")
        assert result == "Attention Is All You Need"

    def test_none_abstract_returns_title_only(self) -> None:
        """None abstract → title only."""
        result = compose_dedup_query("Attention Is All You Need", None)
        assert result == "Attention Is All You Need"

    def test_empty_abstract_returns_title_only(self) -> None:
        """Empty-string abstract → title only."""
        result = compose_dedup_query("Attention Is All You Need", "")
        assert result == "Attention Is All You Need"

    def test_whitespace_abstract_returns_title_only(self) -> None:
        """Whitespace-only abstract → title only."""
        result = compose_dedup_query("Attention Is All You Need", "   ")
        assert result == "Attention Is All You Need"

    def test_with_abstract(self) -> None:
        """AC-05-B: title + first sentence of abstract."""
        result = compose_dedup_query(
            "Attention Is All You Need",
            "Mr. Smith went home. He was tired.",
        )
        assert result == "Attention Is All You Need Mr. Smith went home"

    def test_abstract_question_eof(self) -> None:
        """AC-05-B: abstract ending in '?' at EOF."""
        result = compose_dedup_query(
            "Some Title",
            "Is this paper relevant?",
        )
        assert result == "Some Title Is this paper relevant"

    def test_abstract_no_terminator(self) -> None:
        """AC-05-B: abstract with no terminator → full abstract capped."""
        abstract = "a" * 250
        result = compose_dedup_query("Title", abstract)
        assert result == "Title " + "a" * 200

    def test_empty_title_raises(self) -> None:
        """Empty title raises ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            compose_dedup_query("")

    def test_whitespace_title_raises(self) -> None:
        """Whitespace-only title raises ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            compose_dedup_query("   ")

    def test_title_stripped(self) -> None:
        """Title whitespace is stripped before composition."""
        result = compose_dedup_query("  My Title  ")
        assert result == "My Title"

    def test_query_always_nonempty(self) -> None:
        """Returned query is always non-empty for valid input."""
        result = compose_dedup_query("X")
        assert result
        assert len(result) > 0
