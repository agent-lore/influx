"""Archive path construction, download, and path-safety verification.

Builds archive filesystem paths and note-facing relative paths for
archived PDFs and other source documents.  Path-safety checks prevent
directory traversal attacks (AC-04-C).  The download helper uses the
guarded HTTP client from PRD 02 (FR-ST-4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from influx.errors import InfluxError, NetworkError
from influx.http_client import ContentTypeFamily, guarded_fetch
from influx.slugs import is_valid_slug

__all__ = [
    "ArchivePathError",
    "ArchiveResult",
    "build_archive_path",
    "download_archive",
]

_log = logging.getLogger(__name__)


class ArchivePathError(InfluxError):
    """Raised when archive path construction fails safety checks."""


def _reject_unsafe_id(item_id: str) -> None:
    """Raise if *item_id* contains path-traversal or absolute-path components."""
    if item_id.startswith("/"):
        raise ArchivePathError(f"Item ID {item_id!r} is an absolute path")
    parts = Path(item_id).parts
    if ".." in parts:
        raise ArchivePathError(f"Item ID {item_id!r} contains '..' traversal component")


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
        raise ArchivePathError(f"Source {source!r} is not a valid FR-ST-2 slug")

    # Reject traversal components in item_id before building the path
    # (AC-04-C: reject BEFORE any download is attempted).
    _reject_unsafe_id(item_id)

    year_str = str(published_year)
    month_str = f"{published_month:02d}"

    filename = f"{item_id}{ext}"

    rel_posix = str(PurePosixPath(source) / year_str / month_str / filename)

    fs_path = archive_root / source / year_str / month_str / filename
    resolved = fs_path.resolve()
    root_resolved = archive_root.resolve()

    if not resolved.is_relative_to(root_resolved):
        raise ArchivePathError(
            f"Archive path {fs_path} escapes archive_root "
            f"{archive_root} (resolved to {resolved})"
        )

    return fs_path, rel_posix


# ── Archive download result ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """Outcome of an archive download attempt.

    On success, *ok* is ``True`` and *rel_posix_path* holds the
    note-facing relative path for the ``## Archive`` section.

    On failure, *ok* is ``False``, *rel_posix_path* is ``None``, and
    *error* describes what went wrong.  The caller should render the
    note with an empty ``## Archive`` body and tag it with
    ``influx:repair-needed`` + ``influx:archive-missing`` (FR-ST-4).
    """

    ok: bool
    rel_posix_path: str | None
    error: str


# ── Archive download ────────────────────────────────────────────────


def download_archive(
    *,
    url: str,
    archive_root: Path,
    source: str,
    item_id: str,
    published_year: int,
    published_month: int,
    ext: str = ".pdf",
    allow_private_ips: bool = False,
    max_download_bytes: int = 52_428_800,
    timeout_seconds: int = 30,
    expected_content_type: ContentTypeFamily = "pdf",
) -> ArchiveResult:
    """Download a file via the guarded HTTP client and archive it.

    Uses :func:`build_archive_path` for path construction and
    :func:`~influx.http_client.guarded_fetch` for the download with
    SSRF guard, oversize abort, and timeout enforcement.

    Returns an :class:`ArchiveResult` indicating success or failure.
    On any archive-step failure the result signals the caller to render
    the note with an empty ``## Archive`` body + failure tags (FR-ST-4).
    """
    # Path construction first — reject unsafe IDs before any download
    fs_path, rel_posix = build_archive_path(
        archive_root=archive_root,
        source=source,
        item_id=item_id,
        published_year=published_year,
        published_month=published_month,
        ext=ext,
    )

    try:
        result = guarded_fetch(
            url,
            allow_private_ips=allow_private_ips,
            max_download_bytes=max_download_bytes,
            timeout_seconds=timeout_seconds,
            expected_content_type=expected_content_type,
        )
    except NetworkError as exc:
        _log.warning(
            "Archive download failed for %s: [%s] %s",
            url,
            exc.kind,
            exc,
        )
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=f"{exc.kind}: {exc}",
        )

    if result.status_code >= 400:
        msg = f"HTTP {result.status_code} for {url}"
        _log.warning("Archive download failed: %s", msg)
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=msg,
        )

    try:
        fs_path.parent.mkdir(parents=True, exist_ok=True)
        fs_path.write_bytes(result.body)
    except OSError as exc:
        _log.warning("Archive write failed for %s: %s", fs_path, exc)
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=f"write: {exc}",
        )

    return ArchiveResult(
        ok=True,
        rel_posix_path=rel_posix,
        error="",
    )
