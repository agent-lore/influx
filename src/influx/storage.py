"""Archive path construction, download, and path-safety verification.

Builds archive filesystem paths and note-facing relative paths for
archived PDFs and other source documents.  Path-safety checks prevent
directory traversal attacks (AC-04-C).  The download helper uses the
guarded HTTP client from PRD 02 (FR-ST-4).

Issue #149: :func:`download_archive` consults an
:class:`~influx.archive_policy.ArchivePolicyRegistry` so that known
blocked / rate-limited / skip-by-policy domains are classified
distinctly instead of producing identical generic
``influx:archive-missing`` failures.  The classification surfaces on
:attr:`ArchiveResult.failure_kind` so callers can apply the matching
``influx:archive-blocked`` / ``influx:archive-rate-limited`` /
``influx:archive-skipped-by-policy`` tag and the run-ledger
discriminator picks up the structural signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from influx.archive_policy import (
    ArchivePolicyRegistry,
    classify_failure_kind,
    default_registry,
)
from influx.config import StorageConfig
from influx.errors import InfluxError, NetworkError
from influx.http_client import (
    ContentTypeFamily,
    FetchResult,
    content_type_family,
    guarded_fetch,
)
from influx.slugs import is_valid_slug
from influx.source_kind import classify_source_kind

__all__ = [
    "ArchivePathError",
    "ArchiveResult",
    "build_archive_path",
    "download_archive",
    "download_archive_autodetect",
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

    Issue #149: :attr:`failure_kind` is a stable
    :data:`~influx.archive_policy.ArchiveFailureKind` discriminator
    (``blocked`` / ``rate_limited`` / ``missing_by_policy`` / ``http_*``
    / ``oversize`` / ``timeout`` / …).  :attr:`policy_mode` records the
    :class:`~influx.archive_policy.ArchivePolicy` that was applied for
    the request URL — ``attempt`` for the no-op default case.  Both
    are empty strings on success so the field remains the failure
    side's concern.
    """

    ok: bool
    rel_posix_path: str | None
    error: str
    # Issue #149: stable discriminator for the failure shape.  Empty
    # string on success; one of :data:`ArchiveFailureKind` on failure.
    failure_kind: str = ""
    # Issue #149: the policy mode applied for this URL — ``""`` when
    # no policy was applied (no registry threaded through), otherwise
    # one of :data:`ArchivePolicyMode`.  Surfaced on
    # ``ArchiveResult`` rather than inferred from ``failure_kind``
    # because the ``skip`` mode never produces a failure_kind beyond
    # ``missing_by_policy`` yet the run summary still wants to log
    # which domain policy fired.
    policy_mode: str = ""
    # Domain (host) of the URL the policy was applied against.
    # Convenience field so callers don't have to re-parse the URL.
    domain: str = ""
    # Issue #200: the response Content-Type seen on the fetch (raw
    # header, e.g. ``application/pdf; charset=...``) and its classified
    # family (``"html"`` / ``"pdf"`` / …), plus the fetched bytes.
    # Populated on a successful network fetch so callers can route
    # extraction on the real content type and reuse the bytes without a
    # second fetch.  Empty / ``None`` when no fetch happened (policy
    # short-circuit) or on failure.
    content_type: str = ""
    content_type_family: str = ""
    body: bytes | None = None


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
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
    expected_content_type: ContentTypeFamily = "pdf",
    policy_registry: ArchivePolicyRegistry | None = None,
) -> ArchiveResult:
    """Download a file via the guarded HTTP client and archive it.

    Uses :func:`build_archive_path` for path construction and
    :func:`~influx.http_client.guarded_fetch` for the download with
    SSRF guard, oversize abort, and timeout enforcement.

    Returns an :class:`ArchiveResult` indicating success or failure.
    On any archive-step failure the result signals the caller to render
    the note with an empty ``## Archive`` body + failure tags (FR-ST-4).

    ``max_download_bytes`` and ``timeout_seconds`` default to ``None``;
    when omitted they are resolved from the pydantic
    :class:`~influx.config.StorageConfig` field defaults so the only
    place these tunable defaults live is config-parsing code (AC-X-1).

    Issue #149: when *policy_registry* is provided, the URL's host is
    looked up to obtain an :class:`ArchivePolicy`.  For ``skip``-mode
    policies the function returns immediately with
    ``failure_kind="missing_by_policy"`` and no network call is made.
    For ``blocked`` / ``rate_limited`` policies the download is still
    attempted; the failure (if any) is classified into the
    domain-aware :data:`ArchiveFailureKind` shape so callers can apply
    the matching ``influx:archive-blocked`` /
    ``influx:archive-rate-limited`` tag.  When *policy_registry* is
    ``None``, the module-level
    :func:`~influx.archive_policy.default_registry` is used so even
    untargeted callers (CLI smoke commands, repair sweep) honour the
    staging-defaults set.

    Issue #161: ``unsupported``-mode policies also short-circuit
    before any network call.  Distinct from ``skip`` because the
    caller treats it as "this domain has no archive surface" —
    callers do NOT tag ``influx:archive-missing`` on the resulting
    note, so the item does not contribute to the run-level
    ``archive_acquisition`` degraded reason.  The default registry
    includes ``openai.com`` under this mode.
    """
    if max_download_bytes is None or timeout_seconds is None:
        _storage_defaults = StorageConfig()
        if max_download_bytes is None:
            max_download_bytes = _storage_defaults.max_download_bytes
        if timeout_seconds is None:
            timeout_seconds = _storage_defaults.download_timeout_seconds

    # Issue #149: resolve the policy for this URL.  Defaults to the
    # module-level registry so legacy callers (CLI, scripts) still
    # honour the staging-defaults block-list without rewiring.
    registry = policy_registry if policy_registry is not None else default_registry()
    policy = registry.policy_for(url)
    domain = _domain_from_url(url)

    # Issue #160: short-circuit URLs whose shape already indicates a
    # non-HTML source kind (RSS/Atom/feed pointer, HN discussion link,
    # etc.) when the caller expects HTML.  These URLs would otherwise
    # be sent through the HTML content-type guard and fail every
    # acquisition with ``content_type_mismatch``, looking identical to
    # a real archive failure even though the source-kind never pointed
    # at HTML in the first place.  Distinct from the ``skip`` policy
    # because the discriminator is the URL itself, not a per-domain
    # operator decision.
    if expected_content_type == "html":
        source_kind = classify_source_kind(url)
        if source_kind != "html":
            _log.info(
                "archive download skipped (non-html source) url=%s "
                "domain=%s detected_kind=%s",
                url,
                domain,
                source_kind,
            )
            return ArchiveResult(
                ok=False,
                rel_posix_path=None,
                error=f"non_html_source: detected source kind {source_kind!r}",
                failure_kind="non_html_source",
                policy_mode=policy.mode,
                domain=domain,
            )

    # Skip-mode policies short-circuit before path construction so an
    # entire blocked domain pays zero IO cost (the staging issue: the
    # same blocked domain was retried on every sweep, generating
    # hundreds of repair-noise failures per 12h window).
    if policy.mode == "skip":
        _log.info(
            "archive download skipped (policy=skip) url=%s domain=%s note=%s",
            url,
            domain,
            policy.note,
        )
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=f"missing_by_policy: {policy.note or 'archive disabled by policy'}",
            failure_kind="missing_by_policy",
            policy_mode=policy.mode,
            domain=domain,
        )

    # Issue #161: ``unsupported`` mode also short-circuits before any
    # network call.  Distinct from ``skip``: the caller treats the
    # result as "this domain has no archive surface, that's fine" — no
    # ``influx:archive-missing`` tag, no contribution to the run-level
    # ``archive_acquisition`` degraded reason.  Logged at INFO once per
    # acquisition so an operator sees the policy decision was applied
    # without producing the WARNING-level archive-failure noise.
    if policy.mode == "unsupported":
        _log.info(
            "archive download skipped (policy=unsupported) url=%s domain=%s note=%s",
            url,
            domain,
            policy.note,
        )
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=f"unsupported: {policy.note or 'archive unsupported by policy'}",
            failure_kind="unsupported",
            policy_mode=policy.mode,
            domain=domain,
        )

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
        error = f"{exc.kind}: {exc}"
        failure_kind = classify_failure_kind(error=error, policy_mode=policy.mode)
        _log.warning(
            "Archive download failed for %s: [%s] %s (failure_kind=%s "
            "policy=%s domain=%s)",
            url,
            exc.kind,
            exc,
            failure_kind,
            policy.mode,
            domain,
        )
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=error,
            failure_kind=failure_kind,
            policy_mode=policy.mode,
            domain=domain,
        )

    if result.status_code >= 400:
        msg = f"HTTP {result.status_code} for {url}"
        failure_kind = classify_failure_kind(error=msg, policy_mode=policy.mode)
        _log.warning(
            "Archive download failed: %s (failure_kind=%s policy=%s domain=%s)",
            msg,
            failure_kind,
            policy.mode,
            domain,
        )
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=msg,
            failure_kind=failure_kind,
            policy_mode=policy.mode,
            domain=domain,
        )

    return _persist_archive_body(
        fetched=result,
        fs_path=fs_path,
        rel_posix=rel_posix,
        family=content_type_family(result.content_type),
        policy_mode=policy.mode,
        domain=domain,
    )


def _persist_archive_body(
    *,
    fetched: FetchResult,
    fs_path: Path,
    rel_posix: str,
    family: ContentTypeFamily | None,
    policy_mode: str,
    domain: str,
) -> ArchiveResult:
    """Write a fetched response body to *fs_path* and build the result.

    Shared by :func:`download_archive` and
    :func:`download_archive_autodetect` so the archive-write step plus
    the issue #200 body / content-type surfacing live in one place.  The
    fetched bytes and content type are carried on the returned
    :class:`ArchiveResult` even on a write failure, so a caller can still
    extract from the in-memory bytes when the disk write fails.
    """
    try:
        fs_path.parent.mkdir(parents=True, exist_ok=True)
        fs_path.write_bytes(fetched.body)
    except OSError as exc:
        error = f"write: {exc}"
        _log.warning("Archive write failed for %s: %s", fs_path, exc)
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=error,
            failure_kind="write",
            policy_mode=policy_mode,
            domain=domain,
            content_type=fetched.content_type,
            content_type_family=family or "",
            body=fetched.body,
        )

    return ArchiveResult(
        ok=True,
        rel_posix_path=rel_posix,
        error="",
        failure_kind="",
        policy_mode=policy_mode,
        domain=domain,
        content_type=fetched.content_type,
        content_type_family=family or "",
        body=fetched.body,
    )


_DEFAULT_EXT_BY_FAMILY: dict[ContentTypeFamily, str] = {
    "html": ".html",
    "pdf": ".pdf",
    "xml": ".xml",
}


def download_archive_autodetect(
    *,
    url: str,
    archive_root: Path,
    source: str,
    item_id: str,
    published_year: int,
    published_month: int,
    acceptable_families: tuple[ContentTypeFamily, ...] = ("html", "pdf"),
    allow_private_ips: bool = False,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
    policy_registry: ArchivePolicyRegistry | None = None,
) -> ArchiveResult:
    """Fetch a URL once and archive it, routing on the real Content-Type.

    Unlike :func:`download_archive` — which is told the expected content
    type and file extension up front — this fetches with **no**
    content-type guard, classifies the response via
    :func:`~influx.http_client.content_type_family`, derives the archive
    extension from that family, and returns the fetched bytes on the
    :class:`ArchiveResult` so the caller can extract without a second
    fetch (issue #200).  Used by the inbox source, where a submitter
    hands an arbitrary URL whose true type is only known after the fetch.

    ``skip`` / ``unsupported`` domain policies short-circuit before any
    network call, exactly as in :func:`download_archive`.  The issue
    #160 non-HTML-source URL-shape short-circuit is intentionally *not*
    applied here: routing happens on the actual response type, so a
    URL-shape guard would be redundant and could wrongly reject a real
    article served from a feed-shaped URL.

    A response whose family is not in *acceptable_families* yields an
    ``ok=False`` result with ``failure_kind="content_type_mismatch"``
    but still carries ``content_type`` + ``body`` for the caller's
    fallback and diagnostics.
    """
    if max_download_bytes is None or timeout_seconds is None:
        _storage_defaults = StorageConfig()
        if max_download_bytes is None:
            max_download_bytes = _storage_defaults.max_download_bytes
        if timeout_seconds is None:
            timeout_seconds = _storage_defaults.download_timeout_seconds

    registry = policy_registry if policy_registry is not None else default_registry()
    policy = registry.policy_for(url)
    domain = _domain_from_url(url)

    if policy.mode in ("skip", "unsupported"):
        note = policy.note or f"archive {policy.mode} by policy"
        _log.info(
            "archive download skipped (policy=%s) url=%s domain=%s note=%s",
            policy.mode,
            url,
            domain,
            policy.note,
        )
        failure_kind = "missing_by_policy" if policy.mode == "skip" else "unsupported"
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=f"{failure_kind}: {note}",
            failure_kind=failure_kind,
            policy_mode=policy.mode,
            domain=domain,
        )

    try:
        result = guarded_fetch(
            url,
            allow_private_ips=allow_private_ips,
            max_download_bytes=max_download_bytes,
            timeout_seconds=timeout_seconds,
            expected_content_type=None,
        )
    except NetworkError as exc:
        error = f"{exc.kind}: {exc}"
        failure_kind = classify_failure_kind(error=error, policy_mode=policy.mode)
        _log.warning(
            "Archive download failed for %s: [%s] %s (failure_kind=%s "
            "policy=%s domain=%s)",
            url,
            exc.kind,
            exc,
            failure_kind,
            policy.mode,
            domain,
        )
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=error,
            failure_kind=failure_kind,
            policy_mode=policy.mode,
            domain=domain,
        )

    if result.status_code >= 400:
        msg = f"HTTP {result.status_code} for {url}"
        failure_kind = classify_failure_kind(error=msg, policy_mode=policy.mode)
        _log.warning(
            "Archive download failed: %s (failure_kind=%s policy=%s domain=%s)",
            msg,
            failure_kind,
            policy.mode,
            domain,
        )
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=msg,
            failure_kind=failure_kind,
            policy_mode=policy.mode,
            domain=domain,
            content_type=result.content_type,
            body=result.body,
        )

    family = content_type_family(result.content_type)
    if family is None or family not in acceptable_families:
        msg = (
            f"content_type_mismatch: {result.content_type!r} "
            f"(family={family!r}) not in {acceptable_families}"
        )
        _log.info("archive autodetect content-type mismatch url=%s %s", url, msg)
        return ArchiveResult(
            ok=False,
            rel_posix_path=None,
            error=msg,
            failure_kind="content_type_mismatch",
            policy_mode=policy.mode,
            domain=domain,
            content_type=result.content_type,
            content_type_family=family or "",
            body=result.body,
        )

    fs_path, rel_posix = build_archive_path(
        archive_root=archive_root,
        source=source,
        item_id=item_id,
        published_year=published_year,
        published_month=published_month,
        ext=_DEFAULT_EXT_BY_FAMILY[family],
    )

    return _persist_archive_body(
        fetched=result,
        fs_path=fs_path,
        rel_posix=rel_posix,
        family=family,
        policy_mode=policy.mode,
        domain=domain,
    )


def _domain_from_url(url: str) -> str:
    """Best-effort host extraction without raising — for ArchiveResult.domain.

    Wraps :func:`~influx.archive_policy.extract_domain` but kept local
    so the storage module does not pull the policy module into a tight
    import cycle if the policy registry is ever bootstrapped from
    storage code (defence-in-depth).
    """
    from influx.archive_policy import extract_domain

    return extract_domain(url)
