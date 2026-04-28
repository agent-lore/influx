"""Tests for archive path construction and path-safety (US-008, AC-04-C)."""

from __future__ import annotations

from pathlib import Path

import pytest

from influx.storage import ArchivePathError, build_archive_path


class TestBuildArchivePath:
    """Positive tests for well-formed inputs."""

    def test_arxiv_produces_expected_paths(self, tmp_path: Path) -> None:
        """A well-formed arXiv ID produces the expected filesystem and
        POSIX relative paths (FR-ST-1)."""
        fs_path, rel = build_archive_path(
            archive_root=tmp_path,
            source="arxiv",
            item_id="2601.12345",
            published_year=2026,
            published_month=1,
        )
        assert fs_path == tmp_path / "arxiv" / "2026" / "01" / "2601.12345.pdf"
        assert rel == "arxiv/2026/01/2601.12345.pdf"

    def test_posix_separators_in_relative_path(self, tmp_path: Path) -> None:
        """The note-facing relative path always uses POSIX separators."""
        _, rel = build_archive_path(
            archive_root=tmp_path,
            source="arxiv",
            item_id="2601.99999",
            published_year=2025,
            published_month=12,
        )
        assert "/" in rel
        assert "\\" not in rel

    def test_month_zero_padded(self, tmp_path: Path) -> None:
        """Single-digit months are zero-padded to two digits."""
        _, rel = build_archive_path(
            archive_root=tmp_path,
            source="arxiv",
            item_id="2601.00001",
            published_year=2026,
            published_month=3,
        )
        assert "/03/" in rel

    def test_custom_extension(self, tmp_path: Path) -> None:
        """A custom file extension is respected."""
        fs_path, rel = build_archive_path(
            archive_root=tmp_path,
            source="arxiv",
            item_id="2601.12345",
            published_year=2026,
            published_month=1,
            ext=".html",
        )
        assert fs_path.suffix == ".html"
        assert rel.endswith(".html")

    def test_arxiv_id_with_version(self, tmp_path: Path) -> None:
        """arXiv IDs with version suffixes (e.g. 2601.12345v2) work."""
        fs_path, rel = build_archive_path(
            archive_root=tmp_path,
            source="arxiv",
            item_id="2601.12345v2",
            published_year=2026,
            published_month=1,
        )
        assert fs_path.name == "2601.12345v2.pdf"
        assert rel == "arxiv/2026/01/2601.12345v2.pdf"


class TestPathSafety:
    """AC-04-C: directory traversal and injection attacks are rejected."""

    def test_dotdot_traversal_rejected(self, tmp_path: Path) -> None:
        """An arXiv ID containing '..' is rejected BEFORE any download
        (AC-04-C)."""
        with pytest.raises(ArchivePathError):
            build_archive_path(
                archive_root=tmp_path,
                source="arxiv",
                item_id="../../etc/passwd",
                published_year=2026,
                published_month=1,
            )

    def test_absolute_path_injection_rejected(self, tmp_path: Path) -> None:
        """An arXiv ID that is an absolute path is rejected (AC-04-C)."""
        with pytest.raises(ArchivePathError):
            build_archive_path(
                archive_root=tmp_path,
                source="arxiv",
                item_id="/etc/passwd",
                published_year=2026,
                published_month=1,
            )

    def test_dotdot_in_source_rejected(self, tmp_path: Path) -> None:
        """A source containing '..' fails the slug check."""
        with pytest.raises(ArchivePathError):
            build_archive_path(
                archive_root=tmp_path,
                source="../evil",
                item_id="2601.12345",
                published_year=2026,
                published_month=1,
            )


class TestSlugValidation:
    """Source slug must be a valid FR-ST-2 slug (via slugs.py)."""

    def test_invalid_slug_uppercase_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ArchivePathError, match="not a valid FR-ST-2 slug"):
            build_archive_path(
                archive_root=tmp_path,
                source="ArXiv",
                item_id="2601.12345",
                published_year=2026,
                published_month=1,
            )

    def test_invalid_slug_spaces_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ArchivePathError, match="not a valid FR-ST-2 slug"):
            build_archive_path(
                archive_root=tmp_path,
                source="my source",
                item_id="2601.12345",
                published_year=2026,
                published_month=1,
            )

    def test_empty_slug_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ArchivePathError, match="not a valid FR-ST-2 slug"):
            build_archive_path(
                archive_root=tmp_path,
                source="",
                item_id="2601.12345",
                published_year=2026,
                published_month=1,
            )

    def test_valid_slug_accepted(self, tmp_path: Path) -> None:
        """A lowercase hyphenated slug is valid."""
        fs_path, _ = build_archive_path(
            archive_root=tmp_path,
            source="my-blog",
            item_id="post-42",
            published_year=2026,
            published_month=6,
        )
        assert "my-blog" in str(fs_path)
