"""Unit tests for feedback ingestion helpers (FR-FB-1..3, AC-05-H)."""

from __future__ import annotations

from influx.feedback import format_negative_examples


class TestFormatNegativeExamples:
    """§6.3 negative-example formatting contract."""

    def test_three_titles_formatted(self) -> None:
        """AC-05-H: 3 rejection titles → 3 formatted lines."""
        titles = [
            "Attention Is All You Need",
            "BERT: Pre-training of Deep Bidirectional Transformers",
            "GPT-4 Technical Report",
        ]
        result = format_negative_examples(titles)
        expected = (
            '- "Attention Is All You Need" (rejected)\n'
            '- "BERT: Pre-training of Deep Bidirectional Transformers" (rejected)\n'
            '- "GPT-4 Technical Report" (rejected)'
        )
        assert result == expected

    def test_empty_list_returns_empty_string(self) -> None:
        assert format_negative_examples([]) == ""

    def test_title_truncation_at_max_chars(self) -> None:
        """Titles longer than max_title_chars are truncated."""
        long_title = "A" * 250
        result = format_negative_examples([long_title], max_title_chars=200)
        assert len(long_title[:200]) == 200
        assert result == f'- "{"A" * 200}" (rejected)'

    def test_title_at_max_chars_not_truncated(self) -> None:
        """Title exactly at max_title_chars is NOT truncated."""
        title = "B" * 200
        result = format_negative_examples([title], max_title_chars=200)
        assert result == f'- "{title}" (rejected)'

    def test_single_title(self) -> None:
        result = format_negative_examples(["Only One"])
        assert result == '- "Only One" (rejected)'

    def test_custom_max_title_chars(self) -> None:
        """Custom max_title_chars is honoured."""
        result = format_negative_examples(
            ["Short enough", "This is way too long for ten"],
            max_title_chars=10,
        )
        lines = result.split("\n")
        assert lines[0] == '- "Short enou" (rejected)'
        assert lines[1] == '- "This is wa" (rejected)'
