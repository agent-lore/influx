"""Tests for Tier3Extraction 500-char truncation (FR-ENR-5, AC-07-B)."""

from __future__ import annotations

from influx.schemas import Tier3Extraction


class TestTier3Truncation:
    """String elements > 500 chars are silently truncated to 500 on ingest."""

    def test_600_char_claim_truncated_to_500(self) -> None:
        """AC-07-B: a 600-char input claim becomes a 500-char stored claim."""
        long_claim = "x" * 600
        t = Tier3Extraction(claims=[long_claim])
        assert len(t.claims[0]) == 500

    def test_500_char_claim_unchanged(self) -> None:
        exact = "y" * 500
        t = Tier3Extraction(claims=[exact])
        assert len(t.claims[0]) == 500
        assert t.claims[0] == exact

    def test_499_char_claim_unchanged(self) -> None:
        short = "z" * 499
        t = Tier3Extraction(claims=[short])
        assert len(t.claims[0]) == 499

    def test_truncation_in_datasets(self) -> None:
        t = Tier3Extraction(claims=["c1"], datasets=["d" * 600])
        assert len(t.datasets[0]) == 500

    def test_truncation_in_builds_on(self) -> None:
        t = Tier3Extraction(claims=["c1"], builds_on=["b" * 600])
        assert len(t.builds_on[0]) == 500

    def test_truncation_in_open_questions(self) -> None:
        t = Tier3Extraction(claims=["c1"], open_questions=["q" * 600])
        assert len(t.open_questions[0]) == 500

    def test_truncation_in_potential_connections(self) -> None:
        t = Tier3Extraction(claims=["c1"], potential_connections=["p" * 600])
        assert len(t.potential_connections[0]) == 500

    def test_whitespace_trimmed_before_truncation(self) -> None:
        padded = "  " + "a" * 600 + "  "
        t = Tier3Extraction(claims=[padded])
        assert len(t.claims[0]) == 500
        assert t.claims[0] == "a" * 500

    def test_no_validation_error_on_oversize(self) -> None:
        """Truncation must not raise — it silently caps."""
        t = Tier3Extraction(
            claims=["x" * 1000],
            datasets=["d" * 1000],
            builds_on=["b" * 1000],
            open_questions=["q" * 1000],
            potential_connections=["p" * 1000],
        )
        assert all(
            len(item) == 500
            for lst in [
                t.claims,
                t.datasets,
                t.builds_on,
                t.open_questions,
                t.potential_connections,
            ]
            for item in lst
        )
