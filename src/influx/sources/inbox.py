"""Inbox manual-submission acquisition + note builder (``docs/plans/inbox.md``).

Mirrors :func:`influx.sources.rss.build_rss_note_item` for agent-submitted
URLs.  Two differences from the RSS path:

- **Fixed archive subtree.** Submitted items archive under
  ``archive_root/inbox/YYYY/MM/<url_hash>.<ext>`` regardless of the
  submitter's ``source_tag`` (§13.1) — archive layout is Influx-owned
  operational state, not submitter-controlled.
- **Acquire once, build per profile.** :func:`acquire_inbox_bytes`
  downloads + extracts the URL a single time; :func:`build_inbox_note_item`
  is then called once per scoring profile, reusing that acquisition so the
  URL is fetched only once per inbox item (§5.2/§5.3).

The URL is fetched a single time and the extraction branch is chosen from
the response ``Content-Type`` (issue #200): a PDF response extracts via
:func:`extract_pdf` over the fetched bytes, an HTML response via
:func:`extract_article_from_html` over the same bytes, and anything else
falls back to the submitter's summary hint.  A public PDF works even when
served from a non-``.pdf`` URL because routing is content-type driven, not
URL-shape driven.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from influx.cascade import Acquired, Cascade, TextFlavour
from influx.errors import ExtractionError, NetworkError
from influx.extraction.article import extract_article_from_html
from influx.extraction.pdf import extract_pdf
from influx.renderer import render
from influx.storage import archive_bytes, download_archive_autodetect
from influx.thin_summary import is_thin_summary
from influx.urls import normalise_url, url_hash

if TYPE_CHECKING:
    from influx.config import AppConfig

_log = logging.getLogger(__name__)

# Fixed archive subtree + default ``source:`` tag (§13.1).
INBOX_SOURCE = "inbox"
# v2 local-PDF archive subtree + synthetic source-URL scheme (§16.4).
INBOX_PDF_SOURCE = "inbox-pdf"


@dataclass(frozen=True, slots=True)
class InboxAcquisition:
    """One inbox URL acquired once: archive state + extracted body.

    Reused across every per-profile :func:`build_inbox_note_item` call for
    the same item so the URL is downloaded + extracted only once.
    """

    source_url: str
    url_hash: str
    archive_path: str | None
    archive_missing: bool
    extracted_text: str | None
    summary: str
    text_flavour: TextFlavour
    # Best-effort title recovered from the fetched HTML (issue #210), or
    # ``None`` (PDFs, extraction failure, no recoverable title).  Used as the
    # title fallback below the submitter hint and above the bare URL.
    extracted_title: str | None = None


def acquire_inbox_bytes(
    url: str,
    *,
    config: AppConfig,
    summary_hint: str | None = None,
) -> InboxAcquisition:
    """Download + extract one inbox URL into the fixed inbox archive subtree.

    Fetches the URL **once** via :func:`download_archive_autodetect` and
    routes the extraction branch on the response ``Content-Type`` (issue
    #200): a ``pdf`` response extracts via :func:`extract_pdf` over the
    fetched bytes; an ``html`` response via
    :func:`extract_article_from_html` over the same bytes.  Extraction
    runs off the in-memory bytes returned by the fetch, so it succeeds
    even when the archive disk-write failed, and the archived artifact
    and the scored text always come from the same response.  On any
    failure the *summary_hint* (a submitter-provided excerpt, if any) is
    the fallback body.
    """
    archive_root = Path(config.storage.archive_dir)
    source_url = normalise_url(url)
    hash_val = url_hash(url)
    now = datetime.now(UTC)

    archive_result = download_archive_autodetect(
        url=url,
        archive_root=archive_root,
        source=INBOX_SOURCE,
        item_id=hash_val,
        published_year=now.year,
        published_month=now.month,
        allow_private_ips=config.security.allow_private_ips,
        max_download_bytes=config.storage.max_download_bytes,
        timeout_seconds=config.storage.download_timeout_seconds,
    )
    archive_path = archive_result.rel_posix_path
    archive_missing = not archive_result.ok
    body = archive_result.body
    family = archive_result.content_type_family

    extracted_text: str | None = None
    extracted_title: str | None = None
    summary = summary_hint or ""
    flavour: TextFlavour = "summary-fallback"
    try:
        if family == "pdf" and body is not None:
            extracted_text = extract_pdf(body, source_url=source_url).text
            summary = extracted_text
            flavour = "pdf"
        elif family == "html" and body is not None:
            html_body = body.decode("utf-8", errors="replace")
            article = extract_article_from_html(
                html_body,
                url=url,
                min_web_chars=config.extraction.min_web_chars,
                strip_tags=config.extraction.strip_tags,
            )
            extracted_text = article.text
            extracted_title = article.title
            summary = extracted_text
            flavour = "html"
        elif body is not None:
            # Fetched bytes whose Content-Type is neither HTML nor PDF
            # (e.g. an XML feed URL submitted to the inbox): no extractor
            # applies, so fall through to the summary hint.  DEBUG, not INFO:
            # this is a routine fallback that fires for every feed URL, and
            # the per-item summary log already records the outcome (#212).
            _log.debug(
                "inbox content-type not extractable url=%s family=%r, "
                "using summary fallback",
                url,
                family or archive_result.content_type,
            )
    except (ExtractionError, NetworkError, OSError) as exc:
        # WARNING, not DEBUG: extraction failed and the item proceeds on the
        # (thinner) summary hint — a content-quality degradation an operator
        # should see at the default level.
        _log.warning("inbox extraction failed for %s, using summary hint: %s", url, exc)

    return InboxAcquisition(
        source_url=source_url,
        url_hash=hash_val,
        archive_path=archive_path,
        archive_missing=archive_missing,
        extracted_text=extracted_text,
        summary=summary,
        text_flavour=flavour,
        extracted_title=extracted_title,
    )


def acquire_inbox_pdf(
    local_path: str | Path,
    *,
    config: AppConfig,
    summary_hint: str | None = None,
) -> InboxAcquisition:
    """Acquire one local PDF into the fixed ``inbox-pdf`` archive subtree.

    Reads the file once, identifies it by **SHA-256 of its bytes** — so the
    same paper under two filenames is one note and a re-submit dedups
    (docs/plans/inbox.md §16.4) — archives a copy under
    ``archive_root/inbox-pdf/YYYY/MM/<sha256>.pdf``, and extracts text via
    :func:`extract_pdf` over the in-memory bytes.  The synthetic
    ``source_url`` is ``inbox-pdf:sha256:<hex>``: stable, collision-free
    with HTTP URLs, and accepted by the existing slug / cache machinery.

    Returns the same :class:`InboxAcquisition` shape as
    :func:`acquire_inbox_bytes`, so the per-Profile fan-out and
    :func:`build_inbox_note_item` are reused unchanged.

    The caller MUST validate *local_path* against ``[inbox] pdf_root``
    before calling (§16.3); this function trusts the path.  Extraction
    runs off the in-memory bytes, so a PDF still ingests even when the
    archive disk-write fails.
    """
    archive_root = Path(config.storage.archive_dir)
    body = Path(local_path).read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    source_url = f"inbox-pdf:sha256:{sha}"
    now = datetime.now(UTC)

    archive_result = archive_bytes(
        body=body,
        archive_root=archive_root,
        source=INBOX_PDF_SOURCE,
        item_id=sha,
        published_year=now.year,
        published_month=now.month,
        ext=".pdf",
        content_type="application/pdf",
        content_type_family="pdf",
    )
    archive_path = archive_result.rel_posix_path
    archive_missing = not archive_result.ok

    extracted_text: str | None = None
    summary = summary_hint or ""
    flavour: TextFlavour = "summary-fallback"
    try:
        extracted_text = extract_pdf(body, source_url=source_url).text
        summary = extracted_text
        flavour = "pdf"
    except (ExtractionError, OSError) as exc:
        # WARNING, not DEBUG: see ``acquire_inbox_bytes`` — a failed PDF
        # extraction proceeds on the summary hint and is worth surfacing.
        _log.warning(
            "inbox pdf extraction failed for %s, using summary hint: %s",
            local_path,
            exc,
        )

    return InboxAcquisition(
        source_url=source_url,
        url_hash=sha,
        archive_path=archive_path,
        archive_missing=archive_missing,
        extracted_text=extracted_text,
        summary=summary,
        text_flavour=flavour,
    )


def build_inbox_note_item(
    *,
    acquired: InboxAcquisition,
    profile_name: str,
    score: int,
    confidence: float,
    reason: str,
    filter_tags: tuple[str, ...],
    source_tag: str,
    submitted_by: str,
    title_hint: str | None,
    config: AppConfig,
) -> dict[str, Any] | None:
    """Build a complete ``ProfileItem`` for one (item, profile) ingestion.

    Returns ``None`` when the thin-summary rule (#166) suppresses the item:
    no body was extracted and the fallback summary is structurally thin, so
    writing a summary-only note would add no value.  ``None`` is the tick's
    signal to skip the dispatch for this profile.
    """
    from influx.config import ProfileThresholds

    source_url = acquired.source_url
    # Title preference (§3.2 / #210): submitter hint → recovered HTML title
    # → bare URL.
    title = (
        (title_hint or "").strip()
        or (acquired.extracted_title or "").strip()
        or source_url
    )
    profile_cfg = next((p for p in config.profiles if p.name == profile_name), None)

    # Thin-summary suppression (#166): only when no body was extracted.
    if acquired.extracted_text is None:
        thin, thin_rule = is_thin_summary(
            summary=acquired.summary,
            title=title,
            min_chars=config.extraction.min_summary_chars,
        )
        if thin:
            _log.info(
                "inbox thin-summary drop url=%s profile=%s rule=%s",
                source_url,
                profile_name,
                thin_rule,
            )
            return None

    acquired_bundle = Acquired(
        item_id=f"inbox-{acquired.url_hash}",
        source_url=source_url,
        title=title,
        abstract=acquired.summary,
        identity_tags=(),
        archive_path=acquired.archive_path,
        archive_missing=acquired.archive_missing,
        extracted_text=acquired.extracted_text,
        text_flavour=(
            acquired.text_flavour
            if acquired.extracted_text is not None
            else "summary-fallback"
        ),
    )

    cascade = Cascade(
        config=config,
        profile_name=profile_name,
        profile_summary=profile_cfg.description if profile_cfg else "",
        thresholds=profile_cfg.thresholds if profile_cfg else ProfileThresholds(),
        tier2_extractor=None,
    )
    sections = cascade.enrich(acquired_bundle, score)

    tags: list[str] = [
        f"profile:{profile_name}",
        f"source:{source_tag}",
        f"submitter:{submitted_by}",
        "ingested-by:influx",
        f"schema:{config.influx.note_schema_version}",
    ]
    if acquired.archive_missing:
        tags.append("influx:archive-missing")
        if "influx:repair-needed" not in tags:
            tags.append("influx:repair-needed")
    if sections.full_text is not None:
        tags.append("full-text")
    if sections.tier3 is not None:
        tags.append("influx:deep-extracted")
    for flag in sections.repair_flags:
        if flag not in tags:
            tags.append(flag)
    for flag in sections.terminal_flags:
        if flag not in tags:
            tags.append(flag)

    now = datetime.now(UTC)
    path = f"articles/{source_tag}/{now.year}/{now.month:02d}"

    # Suppress fallback summary when Tier 1 was attempted but failed
    # (AC-07-A / FR-ENR-6) — mirrors the RSS builder.
    summary_for_note = (
        "" if sections.tier1_attempted and sections.tier1 is None else acquired.summary
    )

    content = render(
        title=title,
        tags=tags,
        confidence=confidence,
        archive_path=acquired.archive_path,
        summary=summary_for_note,
        profile_name=profile_name,
        score=score,
        reason=reason,
        tier1_enrichment=sections.tier1,
        full_text=sections.full_text,
        tier3_extraction=sections.tier3,
    )

    return {
        "id": f"inbox-{acquired.url_hash}",
        "title": title,
        "source": INBOX_SOURCE,
        "source_url": source_url,
        "content": content,
        "tags": tags,
        "filter_tags": list(filter_tags),
        "score": score,
        "confidence": confidence,
        "reason": reason,
        "path": path,
        "abstract_or_summary": acquired.summary,
        "contributions": sections.tier1.contributions if sections.tier1 else None,
        "builds_on": list(sections.tier3.builds_on) if sections.tier3 else None,
    }
