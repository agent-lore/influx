"""RSS/Atom feed parser and note builder (PRD 09 FR-SRC-4, FR-SRC-5).

Parses RSS 2.0 and Atom feeds, yielding per-item records that carry the
feed's configured ``source_tag`` verbatim.  Each parsed item includes
``title``, ``url``, ``published``, and ``summary``.

The parser does not infer or override ``source_tag`` — it flows through
from the feed configuration unchanged so that downstream archive layout,
note paths, and ``source:*`` tags all agree.

``build_rss_note_item`` constructs a complete ``ProfileItem`` dict for the
scheduler by downloading the article HTML via the guarded HTTP client
(FR-SRC-5 / FR-RES-4) and archiving it on disk.  After archiving, the
builder attempts web article extraction via ``extract_article``; when the
extracted body is shorter than ``extraction.min_web_chars`` (or extraction
fails), the feed item's ``<summary>`` is used instead (FR-ENR-3, AC-09-J).
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import feedparser

from influx import metrics
from influx.archive_policy import (
    registry_from_config as _archive_policy_registry_from_config,
)
from influx.archive_policy import (
    tag_for_failure_kind as _tag_for_archive_failure_kind,
)
from influx.cascade import Acquired, Cascade
from influx.coordinator import RunKind
from influx.errors import ExtractionError, NetworkError
from influx.extraction.article import extract_article, extract_article_from_html
from influx.filter import FilterScorerError, _acall_filter_model_with_retry
from influx.http_client import aguarded_fetch as _aguarded_fetch
from influx.renderer import render
from influx.repair_hooks import has_usable_source
from influx.slugs import slugify_feed_name
from influx.source import BoundScoredCandidate, Candidate, ScoredCandidate
from influx.storage import download_archive
from influx.telemetry import (
    current_run_id,
    get_tracer,
    record_empty_source_write,
    record_fetched_items,
    record_filter_error,
    record_invalid_url_rejection,
    record_source_acquisition_error,
    record_source_cooldown_skip,
    record_summary_thin_drop,
)
from influx.thin_summary import is_thin_summary
from influx.urls import classify_article_url, normalise_url, url_hash

if TYPE_CHECKING:
    from influx.config import AppConfig, ProfileConfig, ResilienceConfig, RssSourceEntry
    from influx.sources import FetchCache

__all__ = [
    "RssFeedItem",
    "RssSource",
    "build_rss_note_item",
    "make_rss_item_provider",
    "parse_feed",
]

_log = logging.getLogger(__name__)


# ── Per-feed timeout cooldown (issue #163) ─────────────────────────────
#
# A single flaky RSS feed (e.g. the staging-observed "Two Stop Bits"
# aggregator emitting ``httpx.ReadTimeout``) degrades every scheduled
# run with ``source_acquisition``.  The cooldown is a per-feed-URL
# state machine layered on top of the existing fetch path:
#
#   NORMAL  ── timeout_streak >= threshold ──► COOLDOWN
#   COOLDOWN ── deadline elapsed ─────────► NORMAL  (lazy clear)
#   COOLDOWN ── successful fetch ─────────► NORMAL  (eager clear)
#
# Only ``timeout`` failures count — other kinds (DNS, network,
# content_type_mismatch, …) do not enter the cooldown path, so a one-off
# transport hiccup cannot quarantine a healthy feed.
#
# State is per-feed-URL so distinct feeds in the same profile do not
# share counters: one slow feed never suppresses the rest.  The state
# map is process-local — a restart resets every deadline (documented as
# a caveat mirroring the arXiv 429 cooldown in :mod:`influx.sources.arxiv`).


@dataclass
class _RssFeedCooldownState:
    """Per-feed-URL cooldown bookkeeping (issue #163)."""

    streak: int = 0
    deadline_monotonic: float | None = None


_RSS_COOLDOWN_LOCK = threading.Lock()
_RSS_COOLDOWN_STATE: dict[str, _RssFeedCooldownState] = {}


def _reset_rss_cooldown_for_tests() -> None:
    """Reset the per-feed cooldown state — test seam only (issue #163).

    Unit and integration tests need a clean baseline between cases;
    production code never calls this.
    """
    with _RSS_COOLDOWN_LOCK:
        _RSS_COOLDOWN_STATE.clear()


def _rss_cooldown_snapshot(url: str) -> tuple[int, float | None]:
    """Return ``(streak, deadline_monotonic)`` for *url* — test seam only."""
    with _RSS_COOLDOWN_LOCK:
        state = _RSS_COOLDOWN_STATE.get(url)
        if state is None:
            return 0, None
        return state.streak, state.deadline_monotonic


def _should_skip_rss_for_cooldown(
    url: str, resilience: ResilienceConfig
) -> tuple[bool, float | None]:
    """Inspect the per-feed cooldown state machine before a fetch.

    Returns ``(skip, remaining_seconds)``.  When the threshold is ``0``
    (feature disabled) or no deadline is set for *url* the helper
    returns ``(False, None)`` and the caller proceeds normally.

    When an active deadline has elapsed, the state machine transitions
    back to NORMAL *lazily* — the deadline and streak are both cleared
    as part of this call so the next timeout starts a fresh streak.
    """
    threshold = int(resilience.rss_timeout_cooldown_threshold)
    if threshold <= 0:
        return False, None
    now = time.monotonic()
    with _RSS_COOLDOWN_LOCK:
        state = _RSS_COOLDOWN_STATE.get(url)
        if state is None or state.deadline_monotonic is None:
            return False, None
        remaining = state.deadline_monotonic - now
        if remaining <= 0:
            # Deadline elapsed — clear state and proceed normally.
            state.deadline_monotonic = None
            state.streak = 0
            return False, None
        return True, remaining


def _record_rss_timeout(url: str, resilience: ResilienceConfig) -> tuple[int, bool]:
    """Tick the per-feed timeout streak after a timeout fetch failure.

    Returns ``(streak, entered_cooldown)``.  When the resulting streak
    reaches ``rss_timeout_cooldown_threshold`` the state machine
    transitions NORMAL → COOLDOWN by setting a fresh deadline; the
    caller receives ``entered_cooldown=True`` so it can emit a single
    distinctive WARNING line summarising the quarantine.
    """
    threshold = int(resilience.rss_timeout_cooldown_threshold)
    if threshold <= 0:
        return 0, False
    cooldown_seconds = float(resilience.rss_timeout_cooldown_seconds)
    now = time.monotonic()
    with _RSS_COOLDOWN_LOCK:
        state = _RSS_COOLDOWN_STATE.setdefault(url, _RssFeedCooldownState())
        state.streak += 1
        streak = state.streak
        if streak >= threshold and cooldown_seconds > 0:
            state.deadline_monotonic = now + cooldown_seconds
            return streak, True
        return streak, False


def _record_rss_fetch_success(url: str) -> None:
    """Clear the per-feed cooldown state after a successful fetch.

    A successful fetch is the strongest signal that the feed has
    recovered, so the deadline (if any) is cleared *eagerly* — the
    streak counter is also reset so an unrelated timeout later starts
    a fresh streak.
    """
    with _RSS_COOLDOWN_LOCK:
        state = _RSS_COOLDOWN_STATE.get(url)
        if state is None:
            return
        state.streak = 0
        state.deadline_monotonic = None


@dataclass(frozen=True, slots=True)
class RssFeedItem:
    """A single parsed item from an RSS/Atom feed."""

    title: str
    url: str
    published: datetime
    summary: str
    source_tag: str
    feed_name: str


def parse_feed(
    content: bytes | str,
    feed_entry: RssSourceEntry,
    *,
    allow_private_ips: bool = False,
    profile: str = "",
) -> list[RssFeedItem]:
    """Parse RSS/Atom feed content and return per-item records.

    Each item inherits the feed's configured ``source_tag`` verbatim
    (FR-SRC-4).  The parser does not infer or override ``source_tag``.

    Issue #131: each entry's ``link`` is validated via
    :func:`influx.urls.classify_article_url` before the item is
    admitted.  Items whose URL is loopback / private / link-local /
    multicast / malformed / disallowed-scheme are dropped pre-acquire
    (logged at WARNING, counted on the run-ledger entry, no
    ``influx:archive-missing`` note produced).  Each rejection also
    bumps the per-run pre-filter ``fetched_total`` so a feed that
    returned items but had every URL rejected is not misclassified as
    ``fetch_stall`` (review concern 2 on PR #133).

    ``allow_private_ips`` is intentionally **not** threaded from
    ``config.security.allow_private_ips`` by the production
    :func:`_fetch_rss_feed` caller — that flag is the fetcher's
    escape hatch for *configured* internal services and must not
    accidentally re-allow third-party feeds to advertise localhost
    links.  It exists here purely for direct test callers (e.g. the
    ``FakeArticleServer`` integration tests in
    ``tests/integration/test_rss_to_lithos.py``).

    Parameters
    ----------
    content:
        Raw feed XML (bytes or str).
    feed_entry:
        The ``RssSourceEntry`` from the profile config providing
        ``name``, ``url``, and ``source_tag``.
    allow_private_ips:
        Test-only opt-in to accept loopback / private / link-local /
        multicast hosts.  Production callers leave this at ``False``.
    profile:
        Profile name used as a metric label on rejection counts.

    Returns
    -------
    list[RssFeedItem]
        Parsed items sorted by published date (newest first).
    """
    feed = feedparser.parse(content)

    items: list[RssFeedItem] = []
    for entry in feed.entries:
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("link", "")).strip()
        summary = str(entry.get("summary", "")).strip()

        if not title or not link:
            _log.debug("Skipping feed entry with missing title or link")
            continue

        validation = classify_article_url(link, allow_private=allow_private_ips)
        if not validation.ok:
            _log.warning(
                "rss item rejected pre-acquire profile=%s feed=%r url=%s "
                "reason=%s title=%r",
                profile,
                feed_entry.name,
                link,
                validation.reason,
                title,
            )
            record_invalid_url_rejection()
            # Issue #131 review concern 2: count the rejected entry
            # toward the run's pre-filter ``fetched_total`` so a feed
            # that returned items with every URL invalid is not
            # misclassified as ``fetch_stall`` (which means "no source
            # returned anything at all").  The ledger uses
            # ``invalid_url_rejections_total`` to fire
            # ``invalid_url_stall`` instead when warranted.
            record_fetched_items(1)
            metrics.rss_items_rejected_invalid_url().add(
                1,
                {
                    "profile": profile,
                    "source": "rss",
                    "reason": validation.reason or "unknown",
                },
            )
            continue

        published = _parse_published(entry)

        items.append(
            RssFeedItem(
                title=title,
                url=link,
                published=published,
                summary=summary,
                source_tag=feed_entry.source_tag,
                feed_name=feed_entry.name,
            )
        )

    items.sort(key=lambda it: it.published, reverse=True)
    return items


def _parse_published(entry: Any) -> datetime:
    """Extract published datetime from a feed entry.

    Feedparser returns time tuples in UTC, so the conversion uses
    :func:`calendar.timegm` (UTC) rather than :func:`time.mktime` (local
    time).  Using ``mktime`` would shift entries authored near midnight
    UTC by the host's UTC offset, sending them to the wrong
    ``{YYYY-MM-DD}`` archive segment / note path bucket.

    Falls back to ``updated_parsed`` and then ``datetime.now(UTC)`` when
    the entry lacks a parseable date.
    """
    for attr in ("published_parsed", "updated_parsed"):
        parsed = entry.get(attr)
        if parsed is not None:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
            except (TypeError, ValueError, OverflowError):
                continue

    return datetime.now(UTC)


# ── Note item builder (PRD 09 US-003) ──────────────────────────────


def build_rss_note_item(
    *,
    item: RssFeedItem,
    profile_name: str,
    config: AppConfig,
    score: int = 0,
    confidence: float = 0.0,
    reason: str = "",
    filter_tags: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Build a complete ``ProfileItem`` dict for an RSS feed item.

    Downloads the article HTML via the guarded HTTP client (FR-SRC-5),
    archives it on disk under the PRD 09 archive layout, and renders a
    canonical note with the ``source:*`` tag and note path matching the
    feed's ``source_tag``.

    Parameters
    ----------
    item:
        Parsed RSS/Atom feed item.
    profile_name:
        Profile name for the ``profile:*`` tag.
    config:
        Loaded :class:`~influx.config.AppConfig`.

    Returns
    -------
    dict[str, Any] | None
        Ready-to-yield ``ProfileItem`` dict, or ``None`` when the
        thin-summary rule (#166) suppressed this item.  ``None`` is
        the orchestrator's signal to skip the item; the consumer in
        :mod:`influx.run` calls ``continue`` on ``None`` rather than
        appending to the acquired-items list.
    """
    feed_slug = slugify_feed_name(item.feed_name)
    hash_val = url_hash(item.url)
    pub = item.published

    # item_id matches PRD 09 FR-ST-1:
    # {feed-slug}-{YYYY-MM-DD}-{url-hash}
    item_id = f"{feed_slug}-{pub.year:04d}-{pub.month:02d}-{pub.day:02d}-{hash_val}"

    archive_root = Path(config.storage.archive_dir)

    # ── Acquire stage (RSS-specific) ──────────────────────────────
    # Download and archive the article HTML (FR-SRC-5 / FR-RES-4).
    # Issue #149: build the per-domain archive policy registry from
    # config; ``download_archive`` short-circuits skip-mode policies
    # and tags blocked / rate-limited failures distinctly so the
    # repair sweep does not tight-loop on doomed paths.
    archive_policy_registry = _archive_policy_registry_from_config(
        config.storage.archive_policy
    )
    tracer = get_tracer()
    with tracer.span(
        "influx.archive.download",
        attributes={
            "influx.profile": profile_name,
            "influx.run_id": current_run_id.get() or "",
            "influx.source": item.source_tag,
        },
    ):
        archive_result = download_archive(
            url=item.url,
            archive_root=archive_root,
            source=item.source_tag,
            item_id=item_id,
            published_year=pub.year,
            published_month=pub.month,
            ext=".html",
            allow_private_ips=config.security.allow_private_ips,
            max_download_bytes=config.storage.max_download_bytes,
            timeout_seconds=config.storage.download_timeout_seconds,
            expected_content_type="html",
            policy_registry=archive_policy_registry,
        )

    archive_path = archive_result.rel_posix_path
    # Issue #160: ``non_html_source`` is a deliberate short-circuit at
    # the URL-shape level, not a missing archive — the URL never
    # pointed at HTML to begin with (RSS/feed pointer, HN discussion
    # link, …).  Treat it as a terminal, informational outcome rather
    # than an acquisition failure so the run-level
    # ``archive_acquisition`` degraded reason does not fire on these
    # known non-article URLs.
    # Issue #161: ``unsupported`` is the same shape but the
    # discriminator is the domain policy (``openai.com`` etc.) rather
    # than the URL shape.  Both share the "not a missing archive"
    # semantics — they get a dedicated tag + terminal flag, but no
    # ``influx:archive-missing`` and no contribution to
    # ``archive_failures_total``.
    archive_skipped_no_failure = archive_result.failure_kind in (
        "non_html_source",
        "unsupported",
    )
    archive_missing = not archive_result.ok and not archive_skipped_no_failure
    # Issue #149: extract the policy-driven tag (if any) so the tag
    # composition below can apply ``influx:archive-blocked`` /
    # ``influx:archive-rate-limited`` /
    # ``influx:archive-skipped-by-policy`` alongside
    # ``influx:archive-missing``.  Returns ``None`` for generic
    # failures (http_404, oversize, timeout, …) so the existing
    # ``influx:archive-missing`` shape is preserved.  Issues #160/#161:
    # ``non_html_source`` / ``unsupported`` also return their dedicated
    # tags here so the informational ``influx:archive-non-html-source``
    # / ``influx:archive-unsupported`` tag is applied below alongside
    # the terminal flag.
    archive_policy_tag = (
        _tag_for_archive_failure_kind(
            archive_result.failure_kind  # type: ignore[arg-type]
        )
        if archive_missing or archive_skipped_no_failure
        else None
    )
    # Issue #166 review: deferred until after the thin-summary
    # suppression check below so the metric stays consistent with the
    # ``influx:archive-*`` tag actually applied to a written note.
    # ``archive_policy_failures()`` is documented as "Increments
    # alongside archive_missing" so it moves in lock-step with the
    # archive-missing tag.  Eager bump here pollutes the counter for
    # items the suppression rule drops.
    _archive_policy_failure_kind: str | None = None
    if archive_missing:
        _archive_policy_failure_kind = archive_result.failure_kind or "unknown"

    # Article text extraction with summary fallback (FR-ENR-3): try web
    # article extraction; fall back to the feed item's ``<summary>``
    # when the extracted body is below ``min_web_chars`` or extraction
    # fails entirely (AC-09-J).  Tier 1 always uses the feed summary
    # as its abstract input; Tier 2/3 use the extracted body if present.
    extracted_text: str | None = None
    summary = item.summary
    try:
        if archive_result.body is not None:
            # Issue #200: the archive download already fetched this exact
            # URL's HTML — reuse those bytes for extraction instead of a
            # redundant second fetch (same URL, same representation), so
            # the archived artifact and the scored text never diverge.
            # ``download_archive`` only populates ``body`` on a successful
            # HTML fetch (incl. the write-failure case, which still holds
            # the fetched bytes), so reusing it whenever present avoids a
            # redundant fetch even when the disk write failed.
            extraction = extract_article_from_html(
                archive_result.body.decode("utf-8", errors="replace"),
                url=item.url,
                min_web_chars=config.extraction.min_web_chars,
                strip_tags=config.extraction.strip_tags,
            )
        else:
            # No archived bytes (policy skip/block, non-HTML short-circuit,
            # HTTP error, network failure, …) — fall back to a direct fetch
            # so policy-skipped items still get an extraction attempt.
            extraction = extract_article(
                item.url,
                min_web_chars=config.extraction.min_web_chars,
                strip_tags=config.extraction.strip_tags,
                allow_private_ips=config.security.allow_private_ips,
                max_download_bytes=config.storage.max_download_bytes,
                timeout_seconds=config.storage.download_timeout_seconds,
            )
        summary = extraction.text
        extracted_text = extraction.text
    except (ExtractionError, NetworkError) as exc:
        _log.debug(
            "Article extraction failed for %s, using feed summary: %s",
            item.url,
            exc,
        )

    source_url = normalise_url(item.url)
    profile_cfg = next((p for p in config.profiles if p.name == profile_name), None)

    # Issue #166: thin-summary suppression.  When the archive fetch did
    # NOT deliver a body (any failure kind — http_404 / timeout /
    # blocked / non_html_source / unsupported / …) AND the
    # post-extraction-fallback summary is "thin" by the structural test
    # in :mod:`influx.thin_summary`, drop the item entirely rather than
    # writing a low-value summary-only note.  The check runs
    # post-extraction so a successful ``extract_article`` that filled
    # ``summary`` with real body text bypasses the suppression — only
    # genuinely-no-body items end up here.  Deliberately broader than
    # the ``archive_missing`` flag so ``non_html_source`` /
    # ``unsupported`` short-circuits also benefit (a thin summary + a
    # URL that never pointed at HTML is still a low-value note).
    if not archive_result.ok:
        thin, thin_rule = is_thin_summary(
            summary=summary,
            title=item.title,
            min_chars=config.extraction.min_summary_chars,
        )
        if thin:
            failure_kind_label = archive_result.failure_kind or "unknown"
            metrics.summary_thin_drops().add(
                1,
                {
                    "profile": profile_name,
                    "source": item.source_tag,
                    "failure_kind": failure_kind_label,
                    "rule": thin_rule or "unknown",
                },
            )
            record_summary_thin_drop()
            _log.info(
                "thin-summary drop source=rss profile=%s feed-slug=%s "
                "url=%s failure_kind=%s rule=%s",
                profile_name,
                feed_slug,
                source_url,
                failure_kind_label,
                thin_rule,
            )
            return None

    # Issue #189: observe-only empty-source guard.  The item survived the
    # thin-summary check and WILL be written below.  When it has no usable
    # source — a blank ``source:`` tag AND a URL we can't infer a source
    # from (a non-arxiv link) — it is exactly the population a future
    # #187-class metadata loss would later strip into an
    # ``influx:source-invalid`` zombie.  We do NOT drop it (a real article
    # with a working link but no reconstructable feed-slug tag is still
    # legitimate content); we count + log it so the real-world fire-rate
    # is measurable.  Shares ``has_usable_source`` with the repair sweep's
    # ``infer_note_source`` so ingest and repair agree on "usable source".
    if not has_usable_source(source_tag_suffix=item.source_tag, source_url=source_url):
        metrics.empty_source_writes().add(
            1,
            {
                "profile": profile_name,
                # The guard only fires when ``item.source_tag`` is blank
                # (that is the trigger), so there is no per-feed suffix to
                # label with — use the stable adapter identity.
                "source": "rss",
                "reason": "no_usable_source",
            },
        )
        record_empty_source_write()
        _log.info(
            "empty-source write source=rss profile=%s feed-slug=%s "
            "url=%s reason=no_usable_source",
            profile_name,
            feed_slug,
            source_url,
        )

    # Issue #166 review: the item survived the thin-summary suppression
    # check and will be written below.  Bump the deferred
    # ``archive_policy_failures`` counter now so it stays consistent
    # with the archive-failure tag actually applied to the rendered
    # note.  No bump on the suppression path above — the dropped item
    # never produced a tag.
    if _archive_policy_failure_kind is not None:
        metrics.archive_policy_failures().add(
            1,
            {
                "profile": profile_name,
                "source": item.source_tag,
                "kind": _archive_policy_failure_kind,
            },
        )

    acquired = Acquired(
        item_id=item_id,
        source_url=source_url,
        title=item.title,
        # Tier 1 uses the raw feed summary as its abstract input,
        # regardless of extraction success (preserves prior behaviour).
        abstract=item.summary,
        identity_tags=(f"feed-slug:{feed_slug}",),
        archive_path=archive_path,
        archive_missing=archive_missing,
        extracted_text=extracted_text,
        # ``summary-fallback`` records that the feed body was the source
        # of the rendered text, distinguishing it from ``html``-extracted
        # bodies.  Used by future telemetry / Renderer rules.
        text_flavour="html" if extracted_text is not None else "summary-fallback",
    )

    # ── Cascade ───────────────────────────────────────────────────
    # ``ProfileThresholds`` defaults gate every stage off when no
    # profile config is found, mirroring the prior RSS behaviour where
    # an unknown profile produced an unenriched note.
    from influx.config import ProfileThresholds

    cascade = Cascade(
        config=config,
        profile_name=profile_name,
        profile_summary=profile_cfg.description if profile_cfg else "",
        thresholds=profile_cfg.thresholds if profile_cfg else ProfileThresholds(),
        # RSS pre-populates ``Acquired.extracted_text`` at acquire time,
        # so the cascade does not need a Tier-2 extractor seam.
        tier2_extractor=None,
    )
    sections = cascade.enrich(acquired, score)

    # ── Tag composition ──────────────────────────────────────────
    # RSS does not stamp a ``text:*`` provenance tag (canonical
    # convention preserved from PRD 09).  Otherwise the tag-list shape
    # mirrors arXiv's: provenance + identity tags first, archive
    # repair flags next, then cascade-driven tags.
    tags: list[str] = [
        f"profile:{profile_name}",
        f"source:{item.source_tag}",
        f"feed-slug:{feed_slug}",
        "ingested-by:influx",
        f"schema:{config.influx.note_schema_version}",
    ]
    if archive_missing:
        tags.append("influx:archive-missing")
    # Issue #149: domain-policy tag (blocked / rate-limited /
    # skipped-by-policy) — applied alongside ``influx:archive-missing``
    # so existing repair-sweep selectors keep working, but the
    # diagnostic shape is now visible in the tag list.  For ``blocked``
    # and ``missing_by_policy`` (skip) the note is also flipped to
    # ``influx:archive-terminal`` so the repair sweep does NOT
    # tight-loop on a doomed path (this is the staging-log behaviour
    # we are fixing).  ``rate_limited`` retries on cool-down, so it
    # stays in the normal repair-needed loop.
    # Issue #160: ``non_html_source`` also lands its dedicated tag
    # (``influx:archive-non-html-source``) and the terminal flag — the
    # URL shape never pointed at HTML, so there is nothing to repair.
    # Issue #161: ``unsupported`` lands its dedicated tag
    # (``influx:archive-unsupported``) and the terminal flag — the
    # domain has no archive surface.
    if archive_policy_tag is not None and archive_policy_tag not in tags:
        tags.append(archive_policy_tag)
    if (
        archive_policy_tag
        in (
            "influx:archive-blocked",
            "influx:archive-skipped-by-policy",
            "influx:archive-non-html-source",
            "influx:archive-unsupported",
        )
        and "influx:archive-terminal" not in tags
    ):
        tags.append("influx:archive-terminal")
    if sections.full_text is not None:
        tags.append("full-text")
    if archive_missing and "influx:repair-needed" not in tags:
        tags.append("influx:repair-needed")
    if sections.tier3 is not None:
        tags.append("influx:deep-extracted")
    for flag in sections.repair_flags:
        if flag not in tags:
            tags.append(flag)
    for flag in sections.terminal_flags:
        if flag not in tags:
            tags.append(flag)

    # Note storage path: articles/{source_tag}/{YYYY}/{MM} (FR-NOTE-2)
    path = f"articles/{item.source_tag}/{pub.year}/{pub.month:02d}"

    # Suppress the plain-text summary when Tier 1 was attempted but
    # failed (AC-07-A / FR-ENR-6).  When Tier 1 was not attempted
    # (e.g. score below threshold), keep the extracted/feed summary so
    # ``## Summary`` renders the fallback body.
    summary_for_note = (
        "" if sections.tier1_attempted and sections.tier1 is None else summary
    )

    content = render(
        title=item.title,
        tags=tags,
        confidence=confidence,
        archive_path=archive_path,
        summary=summary_for_note,
        profile_name=profile_name,
        score=score,
        reason=reason,
        tier1_enrichment=sections.tier1,
        full_text=sections.full_text,
        tier3_extraction=sections.tier3,
    )

    return {
        "id": f"rss-{feed_slug}-{hash_val}",
        "title": item.title,
        "source": "rss",
        "source_url": source_url,
        "content": content,
        "tags": tags,
        "filter_tags": list(filter_tags) if filter_tags is not None else [],
        "score": score,
        "confidence": confidence,
        "reason": reason,
        "path": path,
        "abstract_or_summary": summary,
        "contributions": sections.tier1.contributions if sections.tier1 else None,
        "builds_on": list(sections.tier3.builds_on) if sections.tier3 else None,
    }


async def _score_rss_items(
    *,
    items: list[RssFeedItem],
    profile: str,
    filter_prompt: str,
    config: AppConfig,
) -> dict[str, Any]:
    """Score RSS items with the configured relevance filter."""
    if not items:
        return {}
    slot = config.models.get("filter")
    if slot is None:
        raise FilterScorerError("models.filter is not configured")
    provider = config.providers.get(slot.provider)
    if provider is None:
        raise FilterScorerError(f"filter provider {slot.provider!r} not configured")

    headers: dict[str, str] = {**provider.extra_headers}
    if provider.api_key_env:
        api_key = os.environ.get(provider.api_key_env, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    url = f"{provider.base_url.rstrip('/')}/chat/completions"
    attempts = slot.max_retries + 1

    all_scores: dict[str, Any] = {}
    batch_size = max(int(config.filter.batch_size), 1)
    for chunk_start in range(0, len(items), batch_size):
        chunk = items[chunk_start : chunk_start + batch_size]
        candidates = [
            {"id": _rss_filter_id(item), "title": item.title, "abstract": item.summary}
            for item in chunk
        ]
        body: dict[str, Any] = {
            "model": slot.model,
            "temperature": slot.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"{filter_prompt}\n\n## CANDIDATES\n"
                        f"{json.dumps(candidates, ensure_ascii=False)}"
                    ),
                }
            ],
        }
        if slot.max_tokens is not None:
            body["max_tokens"] = slot.max_tokens
        if slot.json_mode:
            body["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for _attempt in range(attempts):
            try:
                response = await _acall_filter_model_with_retry(
                    config=config,
                    profile=profile,
                    url=url,
                    body=body,
                    headers=headers,
                    attempts=attempts,
                )
                all_scores.update({result.id: result for result in response.results})
                break
            except Exception as exc:
                last_error = exc
                continue
        else:
            exc_info = None
            if last_error is not None:
                exc_info = (type(last_error), last_error, last_error.__traceback__)
            _log.warning(
                "RSS filter failed for profile %r items %d-%d; skipping chunk",
                profile,
                chunk_start,
                chunk_start + len(chunk) - 1,
                exc_info=exc_info,
            )
            record_filter_error()

    return all_scores


def _rss_filter_id(item: RssFeedItem) -> str:
    return url_hash(item.url)


# ── Source adapter (issue #57) ──────────────────────────────────────


class RssSource:
    """RSS adapter conforming to :class:`influx.source.Source`.

    Fans out across the profile's configured feeds in
    :meth:`fetch_candidates`; per-feed filter scoring stays in the
    legacy provider closure so :func:`_score_rss_items` retains its
    test seam.
    """

    name = "rss"

    def __init__(
        self,
        config: AppConfig,
        *,
        fetch_cache: FetchCache | None = None,
    ) -> None:
        self._config = config
        self._cache = fetch_cache

    async def fetch_candidates(
        self,
        *,
        profile_cfg: ProfileConfig,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
    ) -> list[Candidate]:
        """Fetch every configured feed and flatten into a list of Candidates.

        Each :class:`Candidate.payload` is the original
        :class:`RssFeedItem`, so :meth:`acquire` can reconstruct the
        feed-specific metadata (source_tag, feed_name) without a
        secondary lookup.
        """
        del kind, run_range
        config = self._config
        candidates: list[Candidate] = []
        for feed_entry in profile_cfg.sources.rss:
            items = await _fetch_rss_feed(
                feed_entry,
                self._cache,
                max_download_bytes=config.storage.max_download_bytes,
                timeout_seconds=config.storage.download_timeout_seconds,
                profile=profile_cfg.name,
                resilience=config.resilience,
            )
            metrics.candidates_fetched().add(
                len(items), {"profile": profile_cfg.name, "source": "rss"}
            )
            # #85: per-feed pre-filter count.  Multiple feeds on one
            # profile accumulate.  Sum-of-feeds is the correct
            # ``fetched_total`` for filter_stall discrimination — what
            # matters is whether ANY items reached the filter, not
            # which feed they came from.
            record_fetched_items(len(items))
            for item in items:
                candidates.append(
                    Candidate(
                        item_id=_rss_filter_id(item),
                        title=item.title,
                        abstract=item.summary,
                        source_url=item.url,
                        payload=item,
                    )
                )
        return candidates

    def acquire(
        self,
        scored: ScoredCandidate,
        *,
        profile_cfg: ProfileConfig,
        config: AppConfig,
    ) -> dict[str, Any] | None:
        """Acquire stage: download archive + run cascade + render note.

        Returns ``None`` when the thin-summary rule (#166) suppressed
        the item — the orchestrator skips ``None`` results.
        """
        item = scored.candidate.payload
        if not isinstance(item, RssFeedItem):
            raise TypeError(
                "RssSource.acquire requires Candidate.payload to be RssFeedItem; "
                f"got {type(item).__name__}",
            )
        return build_rss_note_item(
            item=item,
            profile_name=profile_cfg.name,
            config=config,
            score=scored.score,
            confidence=scored.confidence,
            reason=scored.reason,
            filter_tags=scored.filter_tags,
        )


# ── Production-default RSS item provider ────────────────────────────


def make_rss_item_provider(
    config: AppConfig,
    *,
    fetch_cache: FetchCache | None = None,
) -> Any:
    """Build the item provider for RSS feed profiles.

    Fetches each RSS feed configured for the profile, parses items,
    and maps each through :func:`build_rss_note_item`.

    Parameters
    ----------
    config:
        Loaded :class:`~influx.config.AppConfig`.
    fetch_cache:
        Optional shared :class:`~influx.sources.FetchCache` for
        per-fire dedup (R-8).  When two profiles share the same RSS
        feed URL the feed is fetched once and the result shared.
    """
    cache = fetch_cache

    async def provider(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> Iterable[BoundScoredCandidate]:
        del kind, run_range

        profile_cfg = next((p for p in config.profiles if p.name == profile), None)
        if profile_cfg is None:
            _log.info("rss source skipped profile=%s reason=unknown_profile", profile)
            return ()

        # ── Telemetry: influx.fetch.rss span (FR-OBS-4) ──
        _tracer = get_tracer()
        with _tracer.span(
            "influx.fetch.rss",
            attributes={
                "influx.profile": profile,
                "influx.run_id": current_run_id.get() or "",
                "influx.source": "rss",
            },
        ) as fetch_span:
            bounds: list[BoundScoredCandidate] = []
            for feed_entry in profile_cfg.sources.rss:
                _log.info(
                    "rss feed fetch started profile=%s feed=%r url=%s source_tag=%s",
                    profile,
                    feed_entry.name,
                    feed_entry.url,
                    feed_entry.source_tag,
                )
                items = await _fetch_rss_feed(
                    feed_entry,
                    cache,
                    max_download_bytes=config.storage.max_download_bytes,
                    timeout_seconds=config.storage.download_timeout_seconds,
                    profile=profile,
                    resilience=config.resilience,
                )
                metrics.candidates_fetched().add(
                    len(items), {"profile": profile, "source": "rss"}
                )
                # #85: per-feed pre-filter count for the run-level
                # ``fetched_total`` (filter_stall vs fetch_stall split).
                record_fetched_items(len(items))
                _log.info(
                    "rss feed fetch completed profile=%s feed=%r items=%d",
                    profile,
                    feed_entry.name,
                    len(items),
                )
                try:
                    scores = await _score_rss_items(
                        items=items,
                        profile=profile,
                        filter_prompt=filter_prompt,
                        config=config,
                    )
                except FilterScorerError:
                    _log.warning(
                        "RSS filter failed for feed %r; skipping feed batch",
                        feed_entry.name,
                        exc_info=True,
                    )
                    # #85 review: see arxiv.py for rationale.  Per-feed
                    # failures each tick the counter so a profile with
                    # multiple feeds reflects the true partial-failure
                    # surface.
                    record_filter_error()
                    continue
                _log.info(
                    "rss filter completed profile=%s feed=%r items=%d "
                    "scores_returned=%d",
                    profile,
                    feed_entry.name,
                    len(items),
                    len(scores),
                )
                drop_attrs = {"profile": profile, "decision": "drop"}
                pass_attrs = {"profile": profile, "decision": "pass"}
                for item in items:
                    scored = scores.get(_rss_filter_id(item))
                    if scored is None:
                        metrics.articles_filtered().add(1, drop_attrs)
                        _log.info(
                            "article inspected source=rss profile=%s feed=%r "
                            "published=%s score=none decision=drop "
                            "reason=not_returned_by_filter title=%r url=%s",
                            profile,
                            feed_entry.name,
                            item.published.isoformat(),
                            item.title,
                            item.url,
                        )
                        continue
                    if scored.score < profile_cfg.thresholds.relevance:
                        metrics.articles_filtered().add(1, drop_attrs)
                        _log.info(
                            "article inspected source=rss profile=%s feed=%r "
                            "published=%s score=%d threshold=%d decision=drop "
                            "reason=below_relevance title=%r url=%s",
                            profile,
                            feed_entry.name,
                            item.published.isoformat(),
                            scored.score,
                            profile_cfg.thresholds.relevance,
                            item.title,
                            item.url,
                        )
                        continue
                    metrics.articles_filtered().add(1, pass_attrs)
                    _log.info(
                        "article inspected source=rss profile=%s feed=%r "
                        "published=%s score=%d threshold=%d decision=accept "
                        "title=%r url=%s",
                        profile,
                        feed_entry.name,
                        item.published.isoformat(),
                        scored.score,
                        profile_cfg.thresholds.relevance,
                        item.title,
                        item.url,
                    )
                    # Issue #125: emit a BoundScoredCandidate so the Run
                    # stage can run pre-acquire ``lithos_cache_lookup``
                    # before paying the article fetch / archive / extract
                    # cost.  ``build_rss_note_item`` is the per-item
                    # acquire entrypoint; issue #124's ``asyncio.to_thread``
                    # offload moves into the closure, fired only when the
                    # orchestrator decides this candidate must be acquired.
                    sc = ScoredCandidate(
                        candidate=Candidate(
                            item_id=_rss_filter_id(item),
                            title=item.title,
                            abstract=item.summary or "",
                            source_url=item.url,
                            payload=item,
                        ),
                        score=scored.score,
                        confidence=1.0,
                        reason=scored.reason,
                        filter_tags=scored.tags,
                    )

                    async def _acquire(
                        item: RssFeedItem = item,
                        scored: Any = scored,
                    ) -> dict[str, Any] | None:
                        return await asyncio.to_thread(
                            build_rss_note_item,
                            item=item,
                            profile_name=profile,
                            config=config,
                            score=scored.score,
                            confidence=1.0,
                            reason=scored.reason,
                            filter_tags=scored.tags,
                        )

                    bounds.append(
                        BoundScoredCandidate(
                            scored=sc,
                            acquire=_acquire,
                            source_label=f"rss:{feed_entry.name}",
                        )
                    )
                _log.info(
                    "rss feed completed profile=%s feed=%r accepted_so_far=%d",
                    profile,
                    feed_entry.name,
                    len(bounds),
                )
            fetch_span.set_attribute("influx.item_count", len(bounds))
            _log.info(
                "rss source completed profile=%s feeds=%d accepted=%d",
                profile,
                len(profile_cfg.sources.rss),
                len(bounds),
            )

        return bounds

    return provider


async def _fetch_rss_feed(
    feed_entry: RssSourceEntry,
    cache: FetchCache | None,
    *,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
    profile: str = "",
    resilience: ResilienceConfig | None = None,
) -> list[RssFeedItem]:
    """Fetch raw feed bytes, then parse-and-stamp per *feed_entry*.

    The cache is keyed on the feed URL but stores **only** the raw
    response bytes — never the parsed :class:`RssFeedItem` list.  This
    matters because each parsed item embeds the caller's ``source_tag``
    and ``feed_name``: caching the parsed list would let a second profile
    that configured the same URL with different metadata receive items
    stamped with the FIRST profile's metadata, routing them to the wrong
    bucket / archive path (review finding 3).  Re-parsing per call keeps
    the network savings of dedup while preserving the FR-SRC-4 rule that
    each item inherits its feed's configured ``source_tag`` verbatim.

    Issue #163: when *resilience* is provided, the helper consults the
    per-feed timeout cooldown state machine before each fetch.  A feed
    in active cooldown short-circuits with ``source_cooldown_skip``
    instead of repeating the upstream timeout and degrading the run
    with ``source_acquisition``.
    """
    cache_key = f"rss-bytes:{feed_entry.url}"

    # Issue #163: short-circuit fetch when the feed is in active
    # cooldown.  Records ``source_cooldown_skip`` so the run-ledger
    # entry distinguishes "throttled on purpose" from "tried and lost"
    # — mirroring the arXiv 429 cooldown integration (#146).
    if resilience is not None:
        skip, remaining = _should_skip_rss_for_cooldown(feed_entry.url, resilience)
        if skip:
            remaining_str = f"{remaining:.0f}" if remaining is not None else "?"
            _log.info(
                "rss feed skipped (cooldown active) profile=%s feed=%r url=%s "
                "remaining=%ss "
                "(repeated ReadTimeout — quarantined until cooldown elapses)",
                profile,
                feed_entry.name,
                feed_entry.url,
                remaining_str,
            )
            record_source_cooldown_skip(
                source="rss",
                kind="timeout_cooldown",
                detail=(
                    f"{feed_entry.name}: cooldown active, "
                    f"remaining_seconds={remaining_str}"
                ),
            )
            metrics.source_cooldown_skips().add(
                1, {"profile": profile, "source": "rss"}
            )
            return []

    async def _fetch_bytes() -> bytes | None:
        try:
            result = await _aguarded_fetch(
                feed_entry.url,
                max_download_bytes=max_download_bytes,
                timeout_seconds=timeout_seconds,
            )
        except NetworkError as exc:
            # Issue #163: bump the per-feed timeout streak so repeated
            # timeouts (the staging "Two Stop Bits" shape) eventually
            # quarantine this feed.  Other failure kinds (DNS, network,
            # content_type_mismatch, …) do NOT enter the cooldown
            # path so a one-off transport hiccup cannot suppress a
            # healthy feed.
            entered_cooldown = False
            cooldown_streak = 0
            if resilience is not None and exc.kind == "timeout":
                cooldown_streak, entered_cooldown = _record_rss_timeout(
                    feed_entry.url, resilience
                )
            if entered_cooldown:
                _log.warning(
                    "rss feed cooldown engaged after %d consecutive timeouts "
                    "profile=%s feed=%r url=%s cooldown=%ds "
                    "(subsequent runs will skip with source_cooldown_skip)",
                    cooldown_streak,
                    profile,
                    feed_entry.name,
                    feed_entry.url,
                    int(resilience.rss_timeout_cooldown_seconds)
                    if resilience is not None
                    else 0,
                )
            else:
                _log.warning(
                    "RSS feed fetch failed for %r; yielding zero items",
                    feed_entry.name,
                    exc_info=True,
                )
            # Issue #20: surface to the run ledger so the degraded
            # outcome is distinguishable from a quiet feed.
            record_source_acquisition_error(
                source="rss",
                kind=exc.kind or "unknown",
                detail=f"{feed_entry.name}: {exc}",
            )
            metrics.source_acquisition_errors().add(
                1,
                {
                    "profile": profile,
                    "source": "rss",
                    "kind": exc.kind or "unknown",
                },
            )
            return None
        # Issue #163: a successful fetch clears any accumulated streak
        # / deadline for this feed.  Cheap no-op when the feed has
        # never timed out.
        if resilience is not None:
            _record_rss_fetch_success(feed_entry.url)
        return result.body

    if cache is not None:
        body = await cache.get_or_fetch(cache_key, _fetch_bytes)
    else:
        body = await _fetch_bytes()

    if body is None:
        return []

    # NB: ``allow_private_ips`` is deliberately NOT threaded from
    # ``config.security.allow_private_ips`` here.  That flag is the
    # fetcher's escape hatch for talking to *configured* internal
    # services; it must NOT be a "trust upstream feeds to advertise
    # localhost links" knob.  Issue #131: a staging config with
    # ``allow_private_ips = true`` would otherwise re-enable the very
    # behaviour this fix removes.  Direct test callers of
    # :func:`parse_feed` may still opt in.
    return parse_feed(body, feed_entry, profile=profile)
