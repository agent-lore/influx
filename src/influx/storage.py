"""Archive path construction and path-safety verification (FR-ST-1..3).

Builds archive filesystem paths and note-facing relative paths for
archived PDFs and other source documents.  Path-safety checks prevent
directory traversal attacks (AC-04-C).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from influx.errors import InfluxError
from influx.slugs import is_valid_slug

__all__ = [
    "ArchivePathError",
    "build_archive_path",
]


class ArchivePathError(InfluxError):
    """Raised when archive path construction fails safety checks."""


def _reject_unsafe_id(item_id: str) -> None:
    """Raise if *item_id* contains path-traversal or absolute-path components."""
    if item_id.startswith("/"):
        raise ArchivePathError(
            f"Item ID {item_id!r} is an absolute path"
        )
    parts = Path(item_id).parts
    if ".." in parts:
        raise ArchivePathError(
            f"Item ID {item_id!r} contains '..' traversal component"
        )


def build_archive_path(
    *,
    archive_root: Path,
    source: str,
    item_id: str,
    published_year: int,
    published_month: int,
    ext: str = ".pdf",
) -> tuple[Path, str]:
    """Build an archive filesystem path and note-facing relative path.

    Parameters
    ----------
    archive_root:
        Absolute path to the archive root directory.
    source:
        Source slug (e.g. ``"arxiv"``).  Must be a valid FR-ST-2 slug.
    item_id:
        Source-specific item identifier (e.g. an arXiv ID like
        ``"2601.12345"``).
    published_year:
        Year from the item's published date.
    published_month:
        Month from the item's published date (1..12).
    ext:
        File extension including the leading dot (default ``".pdf"``).

    Returns
    -------
    tuple[Path, str]
        ``(filesystem_path, note_relative_path)`` where
        *filesystem_path* is under *archive_root* and
        *note_relative_path* uses POSIX separators suitable for the
        ``## Archive`` section's ``path:`` line.

    Raises
    ------
    ArchivePathError
        If the source slug is invalid, or if the resolved filesystem
        path escapes *archive_root* (directory traversal).
    """
    if not is_valid_slug(source):
        raise ArchivePathError(
            f"Source {source!r} is not a valid FR-ST-2 slug"
        )

    # Reject traversal components in item_id before building the path
    # (AC-04-C: reject BEFORE any download is attempted).
    _reject_unsafe_id(item_id)

    year_str = str(published_year)
    month_str = f"{published_month:02d}"

    filename = f"{item_id}{ext}"

    rel_posix = str(
        PurePosixPath(source) / year_str / month_str / filename
    )

    fs_path = archive_root / source / year_str / month_str / filename
    resolved = fs_path.resolve()
    root_resolved = archive_root.resolve()

    if not resolved.is_relative_to(root_resolved):
        raise ArchivePathError(
            f"Archive path {fs_path} escapes archive_root "
            f"{archive_root} (resolved to {resolved})"
        )

    return fs_path, rel_posix
