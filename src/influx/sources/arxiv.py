"""arXiv Atom feed fetcher with client-side date filtering.

Queries ``https://export.arxiv.org/api/query`` for configured categories,
parses the Atom response with stdlib ``xml.etree.ElementTree``, and applies
client-side date filtering against ``profile.sources.arxiv.lookback_days``
(FR-SRC-1, FR-SRC-2).

Retry behaviour:
- HTTP 429 → honour ``Retry-After`` when present, otherwise sleep
  ``resilience.arxiv_429_backoff_seconds`` then retry (FR-RES-2)
- Other transient failures → exponential backoff from
  ``resilience.backoff_base_seconds`` (FR-RES-1)

``build_arxiv_note_item`` (PRD 07 US-014) constructs a complete
``ProfileItem`` dict for the scheduler, running the HTML → PDF →
abstract-only extraction cascade and rendering the canonical note.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from influx import metrics
from influx.archive_policy import (
    registry_from_config as _archive_policy_registry_from_config,
)
from influx.archive_policy import (
    tag_for_failure_kind as _tag_for_archive_failure_kind,
)
from influx.cascade import Acquired, Cascade, Tier2Result
from influx.config import (
    AppConfig,
    ArxivSourceConfig,
    ProfileConfig,
    ProfileThresholds,
    ResilienceConfig,
    StorageConfig,
)
from influx.coordinator import RunKind
from influx.errors import NetworkError
from influx.extraction.pipeline import extract_arxiv_text
from influx.filter import BatchScorer, Filter, make_default_batch_scorer
from influx.http_client import guarded_fetch
from influx.repair_hooks import has_usable_source
from influx.source import (
    ARXIV_ID_TAG_PREFIX,
    ArchiveDownloadIdentity,
    BoundScoredCandidate,
    Candidate,
    ScoredCandidate,
    find_note_tag,
    note_tags,
    year_month_from_created_at,
    year_month_from_note_path,
)
from influx.sources.note_builder import (
    append_cascade_outcome_tags,
    profile_item_dict,
    render_note_content,
)
from influx.storage import download_archive
from influx.telemetry import (
    current_archive_terminal_arxiv_ids,
    current_run_id,
    get_tracer,
    record_empty_source_write,
    record_fetched_items,
    record_source_acquisition_error,
    record_source_cooldown_skip,
    record_source_retry,
    record_summary_thin_drop,
)
from influx.thin_summary import is_thin_summary

if TYPE_CHECKING:
    from influx.sources import FetchCache

__all__ = [
    "ArxivCooldownError",
    "ArxivItem",
    "ArxivSource",
    "BackfillRange",
    "build_arxiv_note_item",
    "build_query_url",
    "fetch_arxiv",
    "make_arxiv_item_provider",
    "resolve_backfill_range",
]

_log = logging.getLogger(__name__)

_ARXIV_API_URL = "https://export.arxiv.org/api/query"

_ATOM_NS = "http://www.w3.org/2005/Atom"

# Acceptable XML content-type family for successful arXiv responses.
# Content-type validation is performed locally after status-code handling
# so that 429/5xx responses with non-XML bodies route through the proper
# backoff paths (FR-RES-1/2) instead of being raised as content-type
# errors by the guarded fetch.
_XML_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/xml",
        "application/xml",
        "application/atom+xml",
        "application/rss+xml",
    }
)
_RETRY_AFTER_MAX_SECONDS = 300.0


# ── 429 classification (issue #145) ────────────────────────────────
#
# arXiv staff (November 2025) clarified that ``429 Rate exceeded.``
# responses today often reflect *shared* upstream capacity rather than
# a single client's abuse.  The classifier inspects the response body
# and headers to refine the generic ``rate_limit`` retry kind into one
# of:
#
# * ``rate_limit_upstream_capacity`` — the response body contains the
#   canonical arXiv ``Rate exceeded.`` marker (case-insensitive), which
#   the staff guidance attributes to total user load on the public API
#   rather than per-client throttling.  This is the strongest signal we
#   have today; operators interpret it as "back off harder, this is not
#   us".
# * ``rate_limit_local`` — a 429 with a ``Retry-After`` header but
#   without the shared-capacity body marker.  Conventional per-client
#   throttling: the server told us exactly how long to wait, so we
#   treat it as a local pacing issue (likely fixed by tighter
#   ``arxiv_request_min_interval_seconds`` or fewer concurrent
#   profiles).
# * ``rate_limit_unknown`` — neither signal is present.  Behaviour is
#   unchanged from the legacy ``rate_limit`` path; the label exists so
#   operators can still see "we hit a 429 but couldn't tell why" in the
#   ledger instead of having to inspect raw logs.
#
# The classification is what we record into telemetry / the run ledger.
# The thrown ``NetworkError.kind`` remains the legacy ``"rate_limit"``
# so any external consumer keyed on that kind continues to work — the
# refined kind is additive context, not a breaking rename.
_ARXIV_UPSTREAM_CAPACITY_MARKER = b"rate exceeded"


_ARXIV_429_CLASSIFICATION_PREFIX = "classification="


def _extract_arxiv_429_classification(error: NetworkError) -> str | None:
    """Recover the refined 429 classification from a ``NetworkError`` reason.

    ``_fetch_with_retry`` raises 429 failures with the legacy
    ``kind="rate_limit"`` for backward-compatibility with downstream
    consumers, but stashes the refined classification (e.g.
    ``rate_limit_upstream_capacity``) at the head of the *reason*
    string.  Returns ``None`` for non-429 errors so callers can fall
    back to ``error.kind`` unchanged.
    """
    if error.kind != "rate_limit" or not error.reason:
        return None
    reason = error.reason
    if not reason.startswith(_ARXIV_429_CLASSIFICATION_PREFIX):
        return None
    rest = reason[len(_ARXIV_429_CLASSIFICATION_PREFIX) :]
    # ``classification=<kind> retry_after_present=<bool>``; split on the
    # first whitespace so refined kinds with underscores survive intact.
    return rest.split(" ", 1)[0] or None


def _classify_arxiv_429(*, body: bytes, headers: dict[str, str]) -> tuple[str, bool]:
    """Classify an arXiv 429 into a refined retry/error kind (issue #145).

    Returns a ``(kind, retry_after_present)`` pair.  *kind* is one of:

    * ``"rate_limit_upstream_capacity"`` — the response body carries the
      known arXiv ``Rate exceeded.`` marker (case-insensitive); per
      arXiv staff guidance this signals shared upstream capacity rather
      than a per-client abuse event.
    * ``"rate_limit_local"`` — a 429 with a ``Retry-After`` header but
      no shared-capacity body marker; treated as classical per-client
      throttling.
    * ``"rate_limit_unknown"`` — neither signal is present.

    *retry_after_present* is ``True`` exactly when a ``Retry-After``
    header was supplied by the upstream — surfaced to log call sites
    so operators can see at a glance whether the upstream nominated a
    wait time.

    The classification is intentionally tolerant of small variations:
    a body of ``b"Rate exceeded."`` matches, and so does
    ``b"<html>...Rate exceeded...</html>"`` or a body whose case differs
    (``b"rate exceeded"``).  Empty bodies and non-decodable bodies fall
    through to ``rate_limit_unknown`` / ``rate_limit_local`` based only
    on the header signal.
    """
    retry_after_present = _header_value(headers, "Retry-After") is not None
    body_marker = body is not None and _ARXIV_UPSTREAM_CAPACITY_MARKER in body.lower()
    if body_marker:
        return "rate_limit_upstream_capacity", retry_after_present
    if retry_after_present:
        return "rate_limit_local", retry_after_present
    return "rate_limit_unknown", retry_after_present


@dataclass(frozen=True, slots=True)
class ArxivItem:
    """A single parsed arXiv entry from the Atom feed."""

    arxiv_id: str
    title: str
    abstract: str
    published: datetime
    categories: list[str]


# ── Backfill range ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BackfillRange:
    """Resolved backfill date range with absolute UTC bounds.

    Either constructed directly with ``date_from`` / ``date_to`` (the
    explicit ``--from`` / ``--to`` form) or via :func:`resolve_backfill_range`
    which converts the ``--days N`` form into a concrete window relative
    to *now*.
    """

    date_from: date
    date_to: date

    @property
    def days(self) -> int:
        """Number of days covered by this range (inclusive lower bound)."""
        return max((self.date_to - self.date_from).days, 0)


def resolve_backfill_range(
    run_range: dict[str, str | int] | None,
    *,
    now: datetime | None = None,
) -> BackfillRange | None:
    """Convert a ``run_range`` dict into a concrete :class:`BackfillRange`.

    Returns ``None`` when *run_range* is ``None`` (i.e. scheduled / manual
    runs).  Otherwise resolves either the ``--days N`` form (today minus
    *N* days through today) or the explicit ``--from`` / ``--to`` form.
    """
    if run_range is None:
        return None
    if "days" in run_range:
        days = int(run_range["days"])
        ref = now if now is not None else datetime.now(UTC)
        date_to = ref.date()
        date_from = date_to - timedelta(days=days)
        return BackfillRange(date_from=date_from, date_to=date_to)
    if "from" in run_range and "to" in run_range:
        return BackfillRange(
            date_from=date.fromisoformat(str(run_range["from"])),
            date_to=date.fromisoformat(str(run_range["to"])),
        )
    return None


# ── Query URL construction ─────────────────────────────────────────


def build_query_url(
    *,
    categories: list[str],
    max_results: int,
    backfill_range: BackfillRange | None = None,
) -> str:
    """Build the arXiv API query URL per FR-SRC-1.

    Constructs ``search_query`` as an OR-joined expression
    (``cat:X+OR+cat:Y+...``), ``sortBy=submittedDate``,
    ``sortOrder=descending``, and ``max_results`` from the profile.

    When *backfill_range* is provided, an additional
    ``+AND+submittedDate:[YYYYMMDDHHMM+TO+YYYYMMDDHHMM]`` clause restricts
    results to items submitted within the requested window so that
    ``backfill --days N`` actually fetches historical items rather than
    the current feed window (FR-BF-1).

    Range convention (review finding 2): ``BackfillRange`` is half-open
    ``[date_from, date_to)``.  ``date_to`` is exclusive, so a request
    with ``days=N`` covers exactly N calendar days and an explicit
    ``from=A, to=B`` covers exactly ``(B - A).days`` calendar days.
    Because the arXiv ``submittedDate:[... TO ...]`` clause is itself
    inclusive on both endpoints, the upper bound is emitted as the last
    minute (``2359``) of the day BEFORE ``date_to``.
    """
    cat_expr = "+OR+".join(f"cat:{c}" for c in categories)
    if backfill_range is not None:
        from_stamp = backfill_range.date_from.strftime("%Y%m%d") + "0000"
        last_included = backfill_range.date_to - timedelta(days=1)
        if last_included < backfill_range.date_from:
            # Zero-day window — emit a degenerate equal-bound range so
            # the server returns no items rather than an inverted query.
            to_stamp = backfill_range.date_from.strftime("%Y%m%d") + "0000"
        else:
            to_stamp = last_included.strftime("%Y%m%d") + "2359"
        cat_expr = f"({cat_expr})+AND+submittedDate:[{from_stamp}+TO+{to_stamp}]"
    return (
        f"{_ARXIV_API_URL}"
        f"?search_query={cat_expr}"
        f"&sortBy=submittedDate"
        f"&sortOrder=descending"
        f"&max_results={max_results}"
    )


# ── Atom parsing ───────────────────────────────────────────────────


def _extract_arxiv_id(raw_id: str) -> str:
    """Extract the bare arXiv ID from an Atom ``<id>`` element.

    The ``<id>`` element looks like ``http://arxiv.org/abs/2601.12345v1``.
    We strip the URL prefix and the version suffix to get ``2601.12345``.
    """
    # Strip URL prefix
    bare = raw_id
    for prefix in ("http://arxiv.org/abs/", "https://arxiv.org/abs/"):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break

    # Strip version suffix (e.g. "v1", "v2")
    if "v" in bare:
        base, _, rest = bare.rpartition("v")
        if rest.isdigit() and base:
            bare = base

    return bare


def _parse_atom(body: bytes) -> list[ArxivItem]:
    """Parse an arXiv Atom XML response into :class:`ArxivItem` entries."""
    root = ET.fromstring(body)  # noqa: S314
    items: list[ArxivItem] = []

    for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
        id_el = entry.find(f"{{{_ATOM_NS}}}id")
        title_el = entry.find(f"{{{_ATOM_NS}}}title")
        summary_el = entry.find(f"{{{_ATOM_NS}}}summary")
        published_el = entry.find(f"{{{_ATOM_NS}}}published")

        if id_el is None or id_el.text is None:
            continue
        if title_el is None or title_el.text is None:
            continue
        if summary_el is None or summary_el.text is None:
            continue
        if published_el is None or published_el.text is None:
            continue

        arxiv_id = _extract_arxiv_id(id_el.text.strip())
        title = " ".join(title_el.text.strip().split())
        abstract = summary_el.text.strip()

        pub_text = published_el.text.strip()
        published = datetime.fromisoformat(pub_text.replace("Z", "+00:00"))

        categories: list[str] = []
        for cat_el in entry.findall(f"{{{_ATOM_NS}}}category"):
            term = cat_el.get("term")
            if term:
                categories.append(term)

        items.append(
            ArxivItem(
                arxiv_id=arxiv_id,
                title=title,
                abstract=abstract,
                published=published,
                categories=categories,
            )
        )

    return items


def _filter_by_lookback(
    items: list[ArxivItem],
    lookback_days: int,
    now: datetime | None = None,
) -> list[ArxivItem]:
    """Drop items older than *lookback_days* from *now* (FR-SRC-2)."""
    if now is None:
        now = datetime.now(UTC)
    cutoff = now - timedelta(days=lookback_days)
    return [item for item in items if item.published >= cutoff]


# ── Fetch with retry ──────────────────────────────────────────────


def _sleep(seconds: float) -> None:
    """Sleep wrapper for monkeypatching in tests."""
    time.sleep(seconds)


def _header_value(headers: dict[str, str], name: str) -> str | None:
    """Return an HTTP header value using case-insensitive lookup."""
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return None


def _parse_retry_after_seconds(value: str) -> float | None:
    """Parse RFC 7231 ``Retry-After`` as seconds or HTTP-date."""
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = retry_at.timestamp() - time.time()
    if seconds < 0:
        return 0.0
    return min(seconds, _RETRY_AFTER_MAX_SECONDS)


def _arxiv_429_delay(
    result_headers: dict[str, str],
    resilience: ResilienceConfig,
    *,
    attempt: int,
) -> float:
    """Compute the per-attempt 429 backoff delay.

    Issue #129: progressive doubling of ``arxiv_429_backoff_seconds``
    by ``2 ** attempt`` so a run that hits 429 repeatedly waits longer
    each time, capped at ``arxiv_429_backoff_max_seconds``.  An
    ``Retry-After`` header (when present and parseable) overrides the
    computed delay but is itself clamped to the same cap so a misbehaving
    upstream cannot extend a run indefinitely.  ``attempt`` is the
    zero-based index of the current attempt (0 on the first try).
    """
    cap = float(resilience.arxiv_429_backoff_max_seconds)
    min_interval = float(resilience.arxiv_request_min_interval_seconds)
    base = float(resilience.arxiv_429_backoff_seconds)
    retry_after = _header_value(result_headers, "Retry-After")
    if retry_after is not None:
        parsed = _parse_retry_after_seconds(retry_after)
        if parsed is not None:
            return min(max(min_interval, parsed), cap)
    return min(base * (2**attempt), cap)


# Issue #129: process-wide next-slot allocator used to enforce
# ``arxiv_request_min_interval_seconds`` across *all* arXiv fetches in
# the same process — not just the per-day backfill loop.
#
# The pattern is a token-bucket-style "claim a slot under the lock,
# then sleep until that slot outside the lock" scheme.  Storing
# *next-allowed* (rather than *last-fetched*) lets concurrent callers
# claim distinct, non-overlapping slots in a single critical section —
# review of the original implementation flagged that holding the lock
# only across the read/write but not across the sleep let two
# concurrent callers observe the same ``last`` timestamp, compute the
# same wait, and then start their HTTP fetches together, defeating the
# pacing guarantee.  Slot allocation runs under the lock; the wait
# itself happens with the lock released so a long ``Retry-After``-style
# sleep does not block fetches that already hold a later slot.
_FETCH_PACING_LOCK = threading.Lock()
_NEXT_FETCH_SLOT_MONOTONIC: float | None = None


def _reset_fetch_pacing_for_tests() -> None:
    """Reset the module-level pacing state — test seam only.

    Unit tests covering :func:`_apply_min_interval` need a clean baseline
    between cases; production code never calls this.
    """
    global _NEXT_FETCH_SLOT_MONOTONIC  # noqa: PLW0603
    with _FETCH_PACING_LOCK:
        _NEXT_FETCH_SLOT_MONOTONIC = None


def _apply_min_interval(min_interval: float) -> None:
    """Block until this caller's paced slot starts.

    Atomically claims the next ``min_interval``-spaced slot under
    :data:`_FETCH_PACING_LOCK`, then sleeps the difference between the
    claimed slot and ``now`` with the lock released.  Two concurrent
    callers therefore receive *different* slots — caller A sleeps zero
    and starts immediately, caller B sleeps ``min_interval`` and starts
    after A — instead of both observing the same "last fetch" timestamp
    and racing each other to the wire.

    When *min_interval* is ``0`` (or negative) the helper short-circuits
    without touching the slot state: pacing is disabled wholesale and a
    later paced call starts from a clean baseline.
    """
    global _NEXT_FETCH_SLOT_MONOTONIC  # noqa: PLW0603
    if min_interval <= 0:
        return
    now = time.monotonic()
    with _FETCH_PACING_LOCK:
        next_slot = _NEXT_FETCH_SLOT_MONOTONIC
        my_slot = now if next_slot is None or next_slot <= now else next_slot
        _NEXT_FETCH_SLOT_MONOTONIC = my_slot + min_interval
    wait = my_slot - now
    if wait > 0:
        _sleep(wait)


# ── Adaptive 429 cooldown (issue #146) ─────────────────────────────
#
# Influx's per-fetch retry budget already absorbs individual 429s but
# cannot dampen a *burst* across scheduled runs: each new run begins
# cold and immediately retries with the same pacing, so staging logs
# show degraded run after degraded run with zero ingested candidates
# until upstream calms down.  The cooldown is a small process-local
# state machine layered on top of the existing retry loop:
#
#   NORMAL  ─── failure_streak >= threshold ──► COOLDOWN
#   COOLDOWN ── cooldown_deadline_elapsed ─► NORMAL  (lazy clear)
#   COOLDOWN ── successful fetch ─────────► NORMAL  (eager clear)
#
# Counted events are only *final* 429 failures — failures the in-fetch
# retry loop already gave up on.  Transient 429s that the loop
# recovered from never tick the streak; they continue to surface
# through ``record_source_retry``.  Other final failure kinds
# (network, content_type_mismatch, …) do not enter the cooldown path
# at all so a single transport hiccup cannot suppress arXiv.
#
# Setting ``arxiv_429_cooldown_threshold`` to 0 disables the feature
# wholesale: ``_should_skip_for_cooldown`` becomes a no-op, the streak
# stays at 0, and behaviour is identical to the pre-#146 path.
#
# State is *process-local* (caveat noted in PR / config docstring):
# distinct deployments do not share streak counters and a restart
# resets the deadline.  A future cross-restart variant would persist
# the state under ``storage.state_dir`` alongside the run ledger.
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_FAILURE_STREAK: int = 0
_COOLDOWN_DEADLINE_MONOTONIC: float | None = None
# Last refined 429 classification observed by ``_record_429_final_failure``.
# Surfaced into the cooldown-skip ``NetworkError.reason`` so operators
# can tell at a glance whether the cooldown was driven by
# upstream-capacity events ("not us, back off harder") or local
# pacing ("we are the problem").
_COOLDOWN_LAST_CLASSIFICATION: str | None = None


def _reset_arxiv_cooldown_for_tests() -> None:
    """Reset the module-level cooldown state — test seam only.

    Unit and integration tests need a clean baseline between cases;
    production code never calls this.
    """
    global _COOLDOWN_FAILURE_STREAK  # noqa: PLW0603
    global _COOLDOWN_DEADLINE_MONOTONIC  # noqa: PLW0603
    global _COOLDOWN_LAST_CLASSIFICATION  # noqa: PLW0603
    with _COOLDOWN_LOCK:
        _COOLDOWN_FAILURE_STREAK = 0
        _COOLDOWN_DEADLINE_MONOTONIC = None
        _COOLDOWN_LAST_CLASSIFICATION = None


def _arxiv_cooldown_snapshot() -> tuple[int, float | None, str | None]:
    """Return ``(streak, deadline_monotonic, last_classification)`` snapshot.

    Test seam only — production code reads the state via
    :func:`_should_skip_for_cooldown` and the recorder helpers.
    """
    with _COOLDOWN_LOCK:
        return (
            _COOLDOWN_FAILURE_STREAK,
            _COOLDOWN_DEADLINE_MONOTONIC,
            _COOLDOWN_LAST_CLASSIFICATION,
        )


def _should_skip_for_cooldown(
    resilience: ResilienceConfig,
) -> tuple[bool, float | None, str | None]:
    """Inspect the cooldown state machine before a fetch.

    Returns ``(skip, remaining_seconds, classification)``.  When the
    threshold is ``0`` (feature disabled) or no deadline is set the
    helper returns ``(False, None, classification)`` and the caller
    proceeds with the normal fetch path.

    When an active deadline has elapsed, the state machine
    transitions back to NORMAL *lazily* — that is, the deadline and
    streak are both cleared as part of this call so the next 429
    burst starts a fresh streak.  This keeps the state machine
    self-healing without requiring a background thread.
    """
    global _COOLDOWN_FAILURE_STREAK  # noqa: PLW0603
    global _COOLDOWN_DEADLINE_MONOTONIC  # noqa: PLW0603
    threshold = int(resilience.arxiv_429_cooldown_threshold)
    if threshold <= 0:
        # Feature disabled — never skip; do not consult or mutate state.
        return False, None, None
    now = time.monotonic()
    with _COOLDOWN_LOCK:
        deadline = _COOLDOWN_DEADLINE_MONOTONIC
        classification = _COOLDOWN_LAST_CLASSIFICATION
        if deadline is None:
            return False, None, classification
        remaining = deadline - now
        if remaining <= 0:
            # Deadline elapsed — clear state and proceed normally.
            _COOLDOWN_DEADLINE_MONOTONIC = None
            _COOLDOWN_FAILURE_STREAK = 0
            return False, None, classification
        return True, remaining, classification


def _record_429_final_failure(
    resilience: ResilienceConfig, *, classification: str
) -> tuple[int, bool]:
    """Tick the failure streak after the retry loop gave up on a 429.

    Returns ``(streak, entered_cooldown)``.  When the resulting streak
    reaches ``arxiv_429_cooldown_threshold`` the state machine
    transitions NORMAL → COOLDOWN by setting a fresh deadline; the
    caller receives ``entered_cooldown=True`` so it can emit a single
    distinctive log line.

    When the feature is disabled (threshold ``0``) the function is a
    no-op and returns ``(0, False)`` so callers do not have to gate
    their own observability on the disable knob.
    """
    global _COOLDOWN_FAILURE_STREAK  # noqa: PLW0603
    global _COOLDOWN_DEADLINE_MONOTONIC  # noqa: PLW0603
    global _COOLDOWN_LAST_CLASSIFICATION  # noqa: PLW0603
    threshold = int(resilience.arxiv_429_cooldown_threshold)
    if threshold <= 0:
        return 0, False
    cooldown_seconds = float(resilience.arxiv_429_cooldown_seconds)
    now = time.monotonic()
    with _COOLDOWN_LOCK:
        _COOLDOWN_LAST_CLASSIFICATION = classification
        _COOLDOWN_FAILURE_STREAK += 1
        streak = _COOLDOWN_FAILURE_STREAK
        if streak >= threshold and cooldown_seconds > 0:
            _COOLDOWN_DEADLINE_MONOTONIC = now + cooldown_seconds
            return streak, True
        return streak, False


def _record_arxiv_fetch_success() -> None:
    """Clear the cooldown state after a successful arXiv fetch.

    A successful fetch is the strongest possible signal that the
    upstream has recovered, so the cooldown deadline (if any) is
    cleared *eagerly* — operators do not have to wait the rest of
    ``arxiv_429_cooldown_seconds`` once arXiv is healthy again.  The
    streak counter is also reset so an unrelated 429 later in the run
    starts a fresh streak.
    """
    global _COOLDOWN_FAILURE_STREAK  # noqa: PLW0603
    global _COOLDOWN_DEADLINE_MONOTONIC  # noqa: PLW0603
    with _COOLDOWN_LOCK:
        _COOLDOWN_FAILURE_STREAK = 0
        _COOLDOWN_DEADLINE_MONOTONIC = None


class ArxivCooldownError(NetworkError):
    """Raised by ``_fetch_with_retry`` when cooldown suppresses a fetch.

    Subclasses :class:`NetworkError` so existing ``except NetworkError``
    branches continue to handle it without code changes; carries the
    distinctive ``kind="cooldown_active"`` and a remaining-seconds
    reason so the caller can route the failure into the dedicated
    cooldown degraded-reason path instead of the generic
    ``source_acquisition`` path.
    """


def fetch_arxiv(
    *,
    arxiv_config: ArxivSourceConfig,
    resilience: ResilienceConfig,
    now: datetime | None = None,
    backfill_range: BackfillRange | None = None,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> list[ArxivItem]:
    """Fetch and filter arXiv items for the given config.

    Parameters
    ----------
    arxiv_config:
        The ``profile.sources.arxiv`` section from the config.
    resilience:
        The ``resilience`` section for retry/backoff settings.
    now:
        Override for the current time (for testing date filtering).
    backfill_range:
        Optional historical date range.  When supplied, the query URL
        is constrained to ``submittedDate`` within the range (FR-BF-1)
        and the standard ``lookback_days`` lower-bound filter is replaced
        by the explicit range bounds.  The pacing budget for backfills
        is enforced by ``ResilienceConfig.arxiv_request_min_interval_seconds``
        (FR-BF-3) and applied by the caller around each fetch.
    max_download_bytes:
        Maximum response body size in bytes for the underlying
        ``guarded_fetch``.  ``None`` resolves to the
        :class:`~influx.config.StorageConfig` field default so the only
        place this tunable lives is config-parsing code (AC-X-1).
    timeout_seconds:
        Connect + read timeout in seconds for the underlying
        ``guarded_fetch``.  ``None`` resolves to
        ``resilience.arxiv_api_timeout_seconds`` — the arXiv query
        endpoint is slower than a PDF download, so it has a dedicated
        timeout independent of ``storage.download_timeout_seconds``
        (#165 follow-up).

    Returns
    -------
    list[ArxivItem]
        Parsed and date-filtered items, newest first.
    """
    url = build_query_url(
        categories=arxiv_config.categories,
        max_results=arxiv_config.max_results_per_category,
        backfill_range=backfill_range,
    )

    body = _fetch_with_retry(
        url=url,
        resilience=resilience,
        max_download_bytes=max_download_bytes,
        timeout_seconds=timeout_seconds,
    )

    items = _parse_atom(body)
    if backfill_range is not None:
        # Server-side ``submittedDate`` already constrains the window;
        # apply the same bounds client-side as a defense-in-depth check
        # against off-by-one timezone drift (FR-BF-1).  The range is
        # half-open ``[date_from, date_to)`` so that ``days=N`` covers
        # exactly N calendar days (review finding 2).
        from_dt = datetime.combine(
            backfill_range.date_from,
            datetime.min.time(),
            tzinfo=UTC,
        )
        to_dt = datetime.combine(
            backfill_range.date_to,
            datetime.min.time(),
            tzinfo=UTC,
        )
        return [it for it in items if from_dt <= it.published < to_dt]
    return _filter_by_lookback(
        items,
        arxiv_config.lookback_days,
        now=now,
    )


def _fetch_with_retry(
    *,
    url: str,
    resilience: ResilienceConfig,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> bytes:
    """Fetch *url* with 429 backoff and exponential retry (FR-RES-1/2).

    ``max_download_bytes`` and ``timeout_seconds`` default to ``None``;
    when omitted, ``max_download_bytes`` resolves to the
    :class:`~influx.config.StorageConfig` default and ``timeout_seconds``
    to ``resilience.arxiv_api_timeout_seconds`` — the arXiv query
    endpoint has a dedicated timeout, separate from PDF downloads
    (#165 follow-up).

    Issue #129 hardening:

    - 429 retries use a separate, more generous budget
      (``arxiv_429_max_retries``) than network/5xx retries
      (``max_retries``) because 429 is a soft, recoverable signal.
    - 429 backoff is progressive (doubling per attempt) and capped at
      ``arxiv_429_backoff_max_seconds`` so a flapping upstream cannot
      degrade the run while still preventing unbounded waits.
    - The first attempt waits for ``arxiv_request_min_interval_seconds``
      to elapse since the last in-process arXiv fetch, pacing fetches
      across profiles within a single scheduled tick (the per-day
      backfill loop already paces itself with the same interval, so the
      helper fast-paths through there).
    - Each retry decision is recorded via :func:`record_source_retry`
      so the run ledger surfaces "we hit 429 N times but recovered"
      distinct from the swallowed-error list.
    """
    if max_download_bytes is None:
        max_download_bytes = StorageConfig().max_download_bytes
    if timeout_seconds is None:
        # The discovery fetch defaults to the arXiv-API timeout, not the
        # shared storage download timeout — the query endpoint is slower
        # than a PDF download (#165 follow-up).
        timeout_seconds = resilience.arxiv_api_timeout_seconds

    max_retries = resilience.max_retries
    backoff_base = resilience.backoff_base_seconds
    rate_limit_max_retries = resilience.arxiv_429_max_retries

    # Issue #146: short-circuit before pacing / HTTP when the cooldown
    # state machine is active.  Raising before ``_apply_min_interval``
    # means a suppressed fetch returns in milliseconds rather than
    # blocking on the cross-fetch pacing window — operators see the
    # "we skipped on purpose" signal immediately and the run stays
    # responsive.
    skip, remaining, classification = _should_skip_for_cooldown(resilience)
    if skip:
        remaining_str = f"{remaining:.1f}" if remaining is not None else "?"
        cls_str = classification or "unknown"
        _log.warning(
            "arxiv fetch skipped (cooldown active) remaining=%ss "
            "classification=%s url=%s",
            remaining_str,
            cls_str,
            url,
        )
        raise ArxivCooldownError(
            "arXiv fetch skipped: cooldown active after repeated 429 bursts",
            url=url,
            kind="cooldown_active",
            reason=(f"remaining_seconds={remaining_str} classification={cls_str}"),
        )

    # Pace this fetch against the previous in-process arXiv fetch.  A
    # zero / negative interval is treated as "no pacing" by the helper.
    _apply_min_interval(float(resilience.arxiv_request_min_interval_seconds))

    last_error: Exception | None = None

    # The retry loop runs up to ``1 + max(network, rate-limit)`` times so
    # the more generous 429 budget can still drive forward progress when
    # 429s dominate.  Each branch checks its own budget before deciding
    # to retry.
    total_attempts = 1 + max(max_retries, rate_limit_max_retries)
    for attempt in range(total_attempts):
        try:
            # Fetch without expected_content_type so status-code handling
            # (429 backoff, 5xx retry) runs first. Non-XML 429/5xx
            # responses would otherwise be raised as content-type errors
            # before reaching the rate-limit branch (FR-RES-2).
            result = guarded_fetch(
                url,
                max_download_bytes=max_download_bytes,
                timeout_seconds=timeout_seconds,
            )
        except NetworkError as exc:
            last_error = exc
            if attempt < max_retries:
                delay = backoff_base * (2**attempt)
                _log.warning(
                    "arXiv fetch attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    exc.kind,
                    delay,
                )
                record_source_retry(
                    source="arxiv",
                    kind=exc.kind or "network",
                )
                _sleep(delay)
                continue
            raise

        if result.status_code == 429:
            # Issue #145: classify the 429 into a more specific kind so
            # operators can tell shared-capacity upstream events apart
            # from local per-client throttling.  The thrown
            # ``NetworkError.kind`` keeps the legacy ``"rate_limit"``
            # value so downstream consumers don't break; the refined
            # kind flows into ``record_source_retry`` /
            # ``record_source_acquisition_error`` (via ``reason``) so the
            # run-ledger entry and telemetry counters carry the
            # classification.
            kind_refined, retry_after_present = _classify_arxiv_429(
                body=result.body,
                headers=result.headers,
            )
            last_error = NetworkError(
                f"HTTP 429 from arXiv API at {url}",
                url=url,
                kind="rate_limit",
                reason=(
                    f"classification={kind_refined} "
                    f"retry_after_present={str(retry_after_present).lower()}"
                ),
            )
            if attempt < rate_limit_max_retries:
                delay = _arxiv_429_delay(result.headers, resilience, attempt=attempt)
                _log.warning(
                    "arXiv 429 on attempt %d/%d, backing off %.1fs "
                    "(FR-RES-2 classification=%s retry_after_present=%s)",
                    attempt + 1,
                    rate_limit_max_retries + 1,
                    delay,
                    kind_refined,
                    retry_after_present,
                )
                record_source_retry(source="arxiv", kind=kind_refined)
                _sleep(delay)
                continue
            # Issue #146: retry budget exhausted on 429 — tick the
            # cooldown streak.  We pass the refined classification so
            # the cooldown state remembers whether the burst was
            # dominated by upstream-capacity vs local-pacing events;
            # operators see that classification in the eventual
            # cooldown-skip ledger entry.
            streak, entered = _record_429_final_failure(
                resilience, classification=kind_refined
            )
            if entered:
                _log.warning(
                    "arxiv cooldown engaged after %d consecutive 429 final "
                    "failures (classification=%s, cooldown=%ds)",
                    streak,
                    kind_refined,
                    int(resilience.arxiv_429_cooldown_seconds),
                )
            raise last_error

        if result.status_code >= 500:
            last_error = NetworkError(
                f"HTTP {result.status_code} from arXiv API",
                url=url,
                kind="network",
                reason=f"status={result.status_code}",
            )
            if attempt < max_retries:
                delay = backoff_base * (2**attempt)
                _log.warning(
                    "arXiv HTTP %d on attempt %d/%d, retrying in %.1fs (FR-RES-1)",
                    result.status_code,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                record_source_retry(source="arxiv", kind="network")
                _sleep(delay)
                continue
            raise last_error

        if result.status_code >= 400:
            raise NetworkError(
                f"HTTP {result.status_code} from arXiv API",
                url=url,
                kind="network",
                reason=f"status={result.status_code}",
            )

        # Successful response: validate the XML content-type family now.
        mime = result.content_type.split(";")[0].strip().lower()
        if mime not in _XML_CONTENT_TYPES:
            raise NetworkError(
                (f"Content-type {mime!r} does not match expected XML family"),
                url=result.final_url,
                kind="content_type_mismatch",
                reason=(
                    f"Expected one of {', '.join(sorted(_XML_CONTENT_TYPES))}"
                    f"; got {mime!r}"
                ),
            )

        # Issue #146: a clean fetch is the strongest "upstream is OK"
        # signal we have.  Clear the cooldown state machine eagerly so
        # the next request goes back to the normal path immediately.
        _record_arxiv_fetch_success()
        return result.body

    # Should not reach here, but satisfy type checker
    assert last_error is not None  # noqa: S101
    raise last_error


# ── Item builder (PRD 07 US-014) ─────────────────────────────────


def build_arxiv_note_item(
    *,
    item: ArxivItem,
    score: int,
    confidence: float,
    reason: str,
    profile_name: str,
    config: AppConfig,
    thresholds: ProfileThresholds | None = None,
    filter_tags: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Build a complete ``ProfileItem`` dict for the scheduler.

    Runs the HTML → PDF → abstract-only extraction cascade when the
    candidate's *score* crosses the ``full_text`` threshold, sets the
    appropriate ``text:*`` tier tag, and renders the canonical note via
    :func:`~influx.renderer.render`.

    Parameters
    ----------
    item:
        Parsed arXiv entry.
    score:
        LLM-filter score (1–10).
    confidence:
        Filter confidence (0.0–1.0).
    reason:
        Human-readable filter reason.
    profile_name:
        Profile name for the ``profile:*`` tag.
    config:
        Loaded :class:`~influx.config.AppConfig`.
    thresholds:
        Optional explicit thresholds; when ``None`` the first matching
        profile's thresholds are used from *config*.

    Returns
    -------
    dict[str, Any] | None
        Ready-to-yield ``ProfileItem`` dict, or ``None`` when the
        thin-summary rule (#166) suppressed this item.  ``None`` is
        the orchestrator's signal to skip the item; the consumer in
        :mod:`influx.run` calls ``continue`` on ``None`` rather than
        appending to the acquired-items list.
    """
    profile_cfg = next((p for p in config.profiles if p.name == profile_name), None)
    if thresholds is None:
        thresholds = profile_cfg.thresholds if profile_cfg else ProfileThresholds()

    source_url = f"https://arxiv.org/abs/{item.arxiv_id}"
    cat_tags = [f"cat:{c}" for c in item.categories]

    # ── Acquire stage (arXiv-specific) ────────────────────────────
    archive_terminal_ids = current_archive_terminal_arxiv_ids.get()
    is_archive_terminal = item.arxiv_id in archive_terminal_ids
    archive_path: str | None = None
    archive_missing = False
    # Issue #149: per-domain policy tag (blocked / rate-limited /
    # skipped-by-policy) the acquire stage adds when the policy
    # short-circuits or reclassifies the failure.
    archive_policy_tag: str | None = None
    pdf_url = f"https://arxiv.org/pdf/{item.arxiv_id}.pdf"
    tracer = get_tracer()

    # Issue #166: label of the failure that caused the archive to NOT
    # deliver a body — used by the thin-summary suppression check
    # below.  ``None`` when the archive succeeded; a string like
    # ``"terminal"`` / ``"http_404"`` / ``"unsupported"`` otherwise.
    _archive_failure_kind_label: str | None = None
    # Issue #166 review: deferred metric bumps so
    # ``influx_archive_missing_total`` /
    # ``influx_archive_policy_failures_total`` only increment for items
    # that survive the thin-summary suppression check (i.e. items that
    # actually get written with the corresponding tags).  The bumps
    # happen in a single block below, after the suppression decision.
    _archive_missing_bump_pending = False
    _archive_policy_failure_kind: str | None = None
    if is_archive_terminal:
        # Issue #14: this paper's archive download has been terminal-flipped
        # by an earlier repair sweep (or hand-set by an operator).  Skip the
        # download attempt entirely; the existing Lithos note's tags will
        # be preserved by the canonical merge_tags path on rewrite.
        archive_missing = True
        _archive_failure_kind_label = "terminal"
        _archive_missing_bump_pending = True
        _log.info(
            "archive download skipped (terminal) profile=%s arxiv_id=%s",
            profile_name,
            item.arxiv_id,
        )
    else:
        # Issue #149: build the policy registry from config so the
        # operator's blocked / rate-limited / skip overrides win.  The
        # registry is built once per acquire (cheap — pure dataclass
        # composition); a per-run cache would help only at high QPS.
        policy_registry = _archive_policy_registry_from_config(
            config.storage.archive_policy
        )
        with tracer.span(
            "influx.archive.download",
            attributes={
                "influx.profile": profile_name,
                "influx.run_id": current_run_id.get() or "",
                "influx.source": "arxiv",
            },
        ):
            archive_result = download_archive(
                url=pdf_url,
                archive_root=Path(config.storage.archive_dir),
                source="arxiv",
                item_id=item.arxiv_id,
                published_year=item.published.year,
                published_month=item.published.month,
                ext=".pdf",
                allow_private_ips=config.security.allow_private_ips,
                max_download_bytes=config.storage.max_download_bytes,
                timeout_seconds=config.storage.download_timeout_seconds,
                expected_content_type="pdf",
                policy_registry=policy_registry,
            )
        if archive_result.ok:
            archive_path = archive_result.rel_posix_path
        else:
            # Issue #161: ``unsupported`` is a deliberate "this domain
            # has no archive surface" outcome — not a missing archive.
            # Don't tag the note with ``influx:archive-missing``, and
            # don't increment the archive-failure metric.  Still
            # emit the dedicated tag below so an operator can see the
            # policy fired.
            archive_unsupported = archive_result.failure_kind == "unsupported"
            archive_missing = not archive_unsupported
            # Issue #166: capture the failure_kind label for the
            # thin-summary suppression check below.  Includes
            # ``unsupported`` because the user-decided trigger scope is
            # broader than ``archive_missing == True``.
            _archive_failure_kind_label = archive_result.failure_kind or "unknown"
            archive_policy_tag = _tag_for_archive_failure_kind(
                # ArchiveFailureKind is a Literal; mypy needs a cast.
                archive_result.failure_kind  # type: ignore[arg-type]
            )
            if archive_missing:
                # Issue #166 review: deferred until after the thin-summary
                # suppression check below so the metrics stay consistent
                # with the tags actually applied to a written note.
                _archive_missing_bump_pending = True
                _archive_policy_failure_kind = archive_result.failure_kind or "unknown"

    # Issue #166: thin-summary suppression.  When the archive fetch did
    # NOT deliver a body (any failure kind: terminal / http_404 /
    # timeout / blocked / unsupported / …) AND the feed-provided
    # abstract is thin per :mod:`influx.thin_summary`, drop the item
    # entirely rather than writing a low-value abstract-only note.
    # arXiv abstracts are typically several hundred characters of real
    # content so the default 80-char threshold rarely fires; the rule
    # is here for uniformity with the RSS adapter (PR description
    # acceptance criterion: "per-source consistency").
    if _archive_failure_kind_label is not None:
        thin, thin_rule = is_thin_summary(
            summary=item.abstract,
            title=item.title,
            min_chars=config.extraction.min_summary_chars,
        )
        if thin:
            metrics.summary_thin_drops().add(
                1,
                {
                    "profile": profile_name,
                    "source": "arxiv",
                    "failure_kind": _archive_failure_kind_label,
                    "rule": thin_rule or "unknown",
                },
            )
            record_summary_thin_drop()
            _log.info(
                "thin-summary drop source=arxiv profile=%s arxiv_id=%s "
                "url=%s failure_kind=%s rule=%s",
                profile_name,
                item.arxiv_id,
                source_url,
                _archive_failure_kind_label,
                thin_rule,
            )
            return None

    # Issue #189: observe-only empty-source guard, for per-source
    # consistency with the RSS builder.  arXiv notes always carry a
    # ``source:arxiv`` tag and an arxiv ``source_url``, so this never
    # fires here — but wiring it identically keeps the two builders
    # symmetric and guards against a future arXiv code path that somehow
    # produces a tag-less item.  Counts, never drops (mirrors RSS).
    if not has_usable_source(source_tag_suffix="arxiv", source_url=source_url):
        metrics.empty_source_writes().add(
            1,
            {
                "profile": profile_name,
                "source": "arxiv",
                "reason": "no_usable_source",
            },
        )
        record_empty_source_write()
        _log.info(
            "empty-source write source=arxiv profile=%s arxiv_id=%s "
            "url=%s reason=no_usable_source",
            profile_name,
            item.arxiv_id,
            source_url,
        )

    # Issue #166 review: the item survived the thin-summary suppression
    # check and will be written below.  Bump the archive-missing-side
    # counters now so they stay consistent with the
    # ``influx:archive-missing`` tag and the policy tag applied by the
    # tag-composition block.  Deferred from the eager bumps that lived
    # inside the if/else above before this review.
    if _archive_missing_bump_pending:
        metrics.archive_missing().add(1, {"profile": profile_name, "source": "arxiv"})
        if _archive_policy_failure_kind is not None:
            # Issue #149 + #166 review: the policy_failures counter is
            # documented as "Increments alongside archive_missing" so
            # the two move in lock-step — including when thin-summary
            # suppression defers both.
            metrics.archive_policy_failures().add(
                1,
                {
                    "profile": profile_name,
                    "source": "arxiv",
                    "kind": _archive_policy_failure_kind,
                },
            )

    acquired = Acquired(
        item_id=item.arxiv_id,
        source_url=source_url,
        title=item.title,
        abstract=item.abstract,
        identity_tags=tuple(cat_tags),
        archive_path=archive_path,
        archive_missing=archive_missing,
        archive_terminal=is_archive_terminal,
    )

    # ── Cascade ───────────────────────────────────────────────────
    cascade = Cascade(
        config=config,
        profile_name=profile_name,
        profile_summary=profile_cfg.description if profile_cfg else "",
        thresholds=thresholds,
        tier2_extractor=_make_arxiv_tier2_extractor(config),
    )
    sections = cascade.enrich(acquired, score)

    # ── Tag composition ───────────────────────────────────────────
    tags: list[str] = [
        f"profile:{profile_name}",
        f"arxiv-id:{item.arxiv_id}",
        "source:arxiv",
        "ingested-by:influx",
        f"schema:{config.influx.note_schema_version}",
        *cat_tags,
    ]
    if archive_missing:
        tags.append("influx:archive-missing")
    if is_archive_terminal:
        tags.append("influx:archive-terminal")
    # Issue #149: domain-policy tag (blocked / rate-limited /
    # skipped-by-policy) — applied alongside ``influx:archive-missing``
    # so existing repair-sweep selectors keep working, but the
    # diagnostic shape is now visible in the tag list.  For ``blocked``
    # and ``missing_by_policy`` (skip) the note is also flipped to
    # ``influx:archive-terminal`` so the repair sweep does NOT
    # tight-loop on a doomed path (this is the staging-log behaviour
    # we are fixing).  ``rate_limited`` retries on cool-down, so it
    # stays in the normal repair-needed loop.
    if archive_policy_tag is not None and archive_policy_tag not in tags:
        tags.append(archive_policy_tag)
    if (
        archive_policy_tag
        in (
            "influx:archive-blocked",
            "influx:archive-skipped-by-policy",
            "influx:archive-unsupported",
        )
        and "influx:archive-terminal" not in tags
    ):
        tags.append("influx:archive-terminal")
    tags.append(sections.text_tag)
    if sections.full_text is not None:
        tags.append("full-text")
    # Archive-driven repair flag fires at the early position so a
    # missing archive is visible in tags before the cascade's outcomes.
    if archive_missing and "influx:repair-needed" not in tags:
        tags.append("influx:repair-needed")
    append_cascade_outcome_tags(tags, sections)

    # ── Render note ───────────────────────────────────────────────
    # The Tier-1-attempted-but-failed summary suppression (AC-07-A /
    # FR-ENR-6) lives in the shared renderer helper.
    content = render_note_content(
        title=item.title,
        tags=tags,
        confidence=confidence,
        archive_path=archive_path,
        summary=item.abstract,
        profile_name=profile_name,
        score=score,
        reason=reason,
        sections=sections,
    )

    pub = item.published
    path = f"papers/arxiv/{pub.year}/{pub.month:02d}"

    return profile_item_dict(
        item_id=f"arxiv-{item.arxiv_id}",
        title=item.title,
        source="arxiv",
        source_url=source_url,
        content=content,
        tags=tags,
        filter_tags=filter_tags,
        score=score,
        confidence=confidence,
        reason=reason,
        path=path,
        abstract_or_summary=item.abstract,
        sections=sections,
    )


def _make_arxiv_tier2_extractor(
    config: AppConfig,
) -> Callable[[Acquired], Tier2Result]:
    """Build a Tier-2 extractor closure for arXiv that the Cascade calls.

    Wraps :func:`extract_arxiv_text` (HTML → PDF cascade) and adapts
    its ``ArxivExtractionResult`` into the source-agnostic
    :class:`Tier2Result` the Cascade consumes.
    """

    def _extractor(acquired: Acquired) -> Tier2Result:
        result = extract_arxiv_text(acquired.item_id, config)
        flavour = "html" if result.source_tag == "text:html" else "pdf"
        return Tier2Result(
            text=result.text, flavour=flavour, text_tag=result.source_tag
        )

    return _extractor


# ── Source adapter (issue #57) ──────────────────────────────────────


# Modern arxiv ids encode the publication YYMM in their first four digits
# (e.g. ``2605.10178`` -> 2026-05), matching acquisition's published_year/month.
_ARXIV_ID_YYMM_RE = re.compile(r"^(?P<yy>\d{2})(?P<mm>\d{2})\.")


def _year_month_from_arxiv_id(arxiv_id: str) -> tuple[int, int] | None:
    """Derive ``(year, month)`` from a modern arxiv id's ``YYMM`` prefix."""
    m = _ARXIV_ID_YYMM_RE.match(arxiv_id)
    if not m:
        return None
    month = int(m.group("mm"))
    if not 1 <= month <= 12:
        return None
    return 2000 + int(m.group("yy")), month


class ArxivSource:
    """arXiv adapter conforming to :class:`influx.source.Source`.

    Splits the legacy provider closure into the two stages CONTEXT.md
    names: :meth:`fetch_candidates` and :meth:`acquire`.  Filter scoring
    happens between them in :class:`influx.filter.Filter`; the cascade /
    renderer run inside :meth:`acquire`.
    """

    name = "arxiv"

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
        """Fetch raw arXiv items and wrap them as :class:`Candidate` records.

        Surfaces fetch failures via the run-ledger ``source_acquisition``
        path (issue #20) and returns an empty list when the source is
        disabled or fetch failed; the orchestrator then yields zero
        items for arXiv.
        """
        config = self._config
        profile = profile_cfg.name
        if not profile_cfg.sources.arxiv.enabled:
            _log.info("arxiv source skipped profile=%s reason=disabled", profile)
            return []

        arxiv_cfg = profile_cfg.sources.arxiv
        backfill_range = (
            resolve_backfill_range(run_range) if kind == RunKind.BACKFILL else None
        )
        cache_key = "arxiv:" + build_query_url(
            categories=arxiv_cfg.categories,
            max_results=arxiv_cfg.max_results_per_category,
            backfill_range=backfill_range,
        )

        async def _do_fetch() -> list[ArxivItem]:
            return await _fetch_arxiv_items(
                profile=profile,
                kind=kind,
                arxiv_cfg=arxiv_cfg,
                config=config,
                backfill_range=backfill_range,
            )

        tracer = get_tracer()
        with tracer.span(
            "influx.fetch.arxiv",
            attributes={
                "influx.profile": profile,
                "influx.run_id": current_run_id.get() or "",
                "influx.source": "arxiv",
            },
        ) as fetch_span:
            try:
                if self._cache is not None:
                    items = await self._cache.get_or_fetch(cache_key, _do_fetch)
                else:
                    items = await _do_fetch()
            except NetworkError as exc:
                # Issue #146: a cooldown-suppressed fetch is *not* a
                # source-acquisition failure — we deliberately chose
                # not to call upstream.  Surface it through the
                # dedicated cooldown-skip path so the run-ledger entry
                # carries ``source_cooldown_skip`` instead of
                # ``source_acquisition`` and operators can tell
                # "throttled on purpose" apart from "tried and lost".
                if exc.kind == "cooldown_active":
                    _log.info(
                        "arxiv fetch skipped (cooldown active) profile=%r reason=%s",
                        profile,
                        exc.reason or "",
                    )
                    record_source_cooldown_skip(
                        source="arxiv",
                        kind=exc.kind,
                        detail=str(exc),
                    )
                    metrics.source_cooldown_skips().add(
                        1, {"profile": profile, "source": "arxiv"}
                    )
                    return []
                # Issue #145: prefer the refined 429 classification
                # stashed on the reason field so the run-ledger and
                # metric counters surface "shared-capacity upstream"
                # vs "local pacing" vs "unknown 429" instead of a
                # generic ``rate_limit``.  Non-429 errors fall back
                # to ``exc.kind`` unchanged.
                ledger_kind = (
                    _extract_arxiv_429_classification(exc) or exc.kind or "unknown"
                )
                _log.warning(
                    "arxiv fetch failed for profile %r kind=%s; yielding zero items",
                    profile,
                    ledger_kind,
                    exc_info=True,
                )
                record_source_acquisition_error(
                    source="arxiv",
                    kind=ledger_kind,
                    detail=str(exc),
                )
                metrics.source_acquisition_errors().add(
                    1,
                    {
                        "profile": profile,
                        "source": "arxiv",
                        "kind": ledger_kind,
                    },
                )
                return []
            fetch_span.set_attribute("influx.item_count", len(items))
            metrics.candidates_fetched().add(
                len(items), {"profile": profile, "source": "arxiv"}
            )
            # #85: feed the pre-filter count into the run-level
            # ``fetched_total`` so the ledger can split fetch_stall
            # (no items reached the filter) from filter_stall (items
            # reached the filter, all rejected).  Source-error path
            # above returned early on NetworkError, so this only fires
            # when the fetch actually succeeded.
            record_fetched_items(len(items))
            _log.info(
                "arxiv fetch completed profile=%s kind=%s items=%d",
                profile,
                kind.value,
                len(items),
            )

        return [
            Candidate(
                item_id=item.arxiv_id,
                title=item.title,
                abstract=item.abstract,
                source_url=f"https://arxiv.org/abs/{item.arxiv_id}",
                payload=item,
            )
            for item in items
        ]

    def acquire(
        self,
        scored: ScoredCandidate,
        *,
        profile_cfg: ProfileConfig,
        config: AppConfig,
    ) -> dict[str, Any] | None:
        """Acquire stage: download archive + run cascade + render note.

        Delegates to :func:`build_arxiv_note_item`.  The legacy module
        binding is preserved so existing tests that patch
        ``influx.sources.arxiv.build_arxiv_note_item`` continue to work.

        Returns ``None`` when the thin-summary rule (#166) suppressed
        the item — the orchestrator skips ``None`` results.
        """
        item = scored.candidate.payload
        if not isinstance(item, ArxivItem):
            raise TypeError(
                "ArxivSource.acquire requires Candidate.payload to be ArxivItem; "
                f"got {type(item).__name__}",
            )
        return build_arxiv_note_item(
            item=item,
            score=scored.score,
            confidence=scored.confidence,
            reason=scored.reason,
            profile_name=profile_cfg.name,
            config=config,
            filter_tags=scored.filter_tags,
        )

    def archive_download_identity(
        self, note: dict[str, object]
    ) -> ArchiveDownloadIdentity | None:
        """Rebuild the arXiv archive-download identity from a note (finding 3b).

        The inverse of the acquire-time identity: the PDF URL, ``.pdf``
        extension, and ``item_id = arxiv_id`` mirror the download that
        :meth:`acquire` -> :func:`build_arxiv_note_item` performs
        (``https://arxiv.org/pdf/<id>.pdf``).  The archive ``(year, month)``
        bucket falls back path -> arxiv-id ``YYMM`` -> ``created_at`` because
        ``read_note`` does not preserve the note path; the retry bucket may
        differ from the original (acceptable — acquisition failed, so no
        archive lives on disk for the original path).

        Returns ``None`` when the note lacks the ``arxiv-id`` tag or any
        resolvable publication ``(year, month)``.
        """
        arxiv_id = find_note_tag(note_tags(note), ARXIV_ID_TAG_PREFIX)
        if not arxiv_id:
            _log.warning(
                "arxiv archive re-acquire: no arxiv-id tag on note id=%s",
                note.get("id", "?"),
            )
            return None
        ym = (
            year_month_from_note_path(note)
            or _year_month_from_arxiv_id(arxiv_id)
            or year_month_from_created_at(note)
        )
        if ym is None:
            _log.warning(
                "arxiv archive re-acquire: no year/month from path, arxiv id, "
                "or created_at for note id=%s",
                note.get("id", "?"),
            )
            return None
        year, month = ym
        return ArchiveDownloadIdentity(
            url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            item_id=arxiv_id,
            published_year=year,
            published_month=month,
            ext=".pdf",
            expected_content_type="pdf",
        )


async def _fetch_arxiv_items(
    *,
    profile: str,
    kind: RunKind,
    arxiv_cfg: ArxivSourceConfig,
    config: AppConfig,
    backfill_range: BackfillRange | None,
) -> list[ArxivItem]:
    """Run the (possibly per-day) arXiv fetch loop and return raw items.

    Extracted from the legacy provider closure so :class:`ArxivSource`
    and the legacy ``make_arxiv_item_provider`` share one fetch
    implementation.
    """
    if kind != RunKind.BACKFILL or backfill_range is None:
        _log.info(
            "arxiv fetch started profile=%s kind=%s categories=%s "
            "max_results=%d lookback_days=%d",
            profile,
            kind.value,
            arxiv_cfg.categories,
            arxiv_cfg.max_results_per_category,
            arxiv_cfg.lookback_days,
        )
        # Issue #124: ``fetch_arxiv`` is synchronous and performs
        # blocking HTTP via ``guarded_fetch`` plus blocking ``_sleep``
        # backoff for arxiv 429s. Offload to a worker thread so the
        # admin event loop stays responsive throughout the fetch.
        return await asyncio.to_thread(
            fetch_arxiv,
            arxiv_config=arxiv_cfg,
            resilience=config.resilience,
            backfill_range=backfill_range,
            max_download_bytes=config.storage.max_download_bytes,
            timeout_seconds=config.resilience.arxiv_api_timeout_seconds,
        )

    n_categories = max(len(arxiv_cfg.categories), 1)
    per_day_max = arxiv_cfg.max_results_per_category * n_categories
    per_day_arxiv_cfg = ArxivSourceConfig(
        enabled=arxiv_cfg.enabled,
        categories=list(arxiv_cfg.categories),
        max_results_per_category=per_day_max,
        lookback_days=arxiv_cfg.lookback_days,
    )
    pacing = float(config.resilience.arxiv_request_min_interval_seconds)
    collected: list[ArxivItem] = []
    seen_ids: set[str] = set()
    current = backfill_range.date_from
    while current < backfill_range.date_to:
        day_range = BackfillRange(
            date_from=current,
            date_to=current + timedelta(days=1),
        )
        _log.info(
            "arxiv backfill day fetch started profile=%s day=%s "
            "categories=%s max_results=%d",
            profile,
            current.isoformat(),
            arxiv_cfg.categories,
            per_day_max,
        )
        # Issue #124: blocking sleep + sync fetch on the event loop —
        # offload both to a worker thread so the admin API stays
        # responsive across the per-day backfill loop.
        await asyncio.to_thread(_sleep, pacing)
        try:
            day_items = await asyncio.to_thread(
                fetch_arxiv,
                arxiv_config=per_day_arxiv_cfg,
                resilience=config.resilience,
                backfill_range=day_range,
                max_download_bytes=config.storage.max_download_bytes,
                timeout_seconds=config.resilience.arxiv_api_timeout_seconds,
            )
        except NetworkError:
            _log.warning(
                "arxiv fetch failed for day %s; continuing backfill",
                current.isoformat(),
                exc_info=True,
            )
            day_items = []
        for it in day_items:
            if it.arxiv_id not in seen_ids:
                seen_ids.add(it.arxiv_id)
                collected.append(it)
        _log.info(
            "arxiv backfill day fetch completed profile=%s day=%s items=%d "
            "collected=%d",
            profile,
            current.isoformat(),
            len(day_items),
            len(collected),
        )
        current = current + timedelta(days=1)
    return collected


# ── Production-default item provider (PRD 07 finding #1) ──────────────


def make_arxiv_item_provider(
    config: AppConfig,
    *,
    scorer: BatchScorer | None = None,
    fetch_cache: FetchCache | None = None,
) -> Any:
    """Build the production-default ``item_provider`` for arXiv profiles.

    Returns an async callable that conforms to
    :data:`~influx.scheduler.ItemProvider`: it iterates each profile's
    enabled arXiv source, fetches items via :func:`fetch_arxiv`, and
    maps each result through :func:`build_arxiv_note_item` so the
    scheduler's ``run_profile`` drives the real HTML → PDF →
    abstract-only extraction stack and the Tier 1 / Tier 3 enrichment
    callers end-to-end.

    Score-gating seam
    -----------------
    *scorer* is the batched :data:`~influx.filter.BatchScorer` that
    :class:`~influx.filter.Filter` drives — the same source-agnostic seam
    RSS uses (finding 2.4).  When ``None`` the production default
    :func:`~influx.filter.make_default_batch_scorer` is installed, which
    wraps the configured ``[models.filter]`` slot; tests inject a
    deterministic ``BatchScorer`` to exercise the score-gated extraction /
    enrichment paths (US-014/US-015) without a real LLM.

    When no scorer is wired and ``[models.filter]`` is absent, the default
    is ``None`` and the run yields zero items rather than fabricating
    scores, so a misconfigured deployment completes cleanly instead of
    ingesting unscored notes (``Filter.score`` returns an empty list when
    its scorer is ``None``).

    Parameters
    ----------
    fetch_cache:
        Optional shared :class:`~influx.sources.FetchCache` for
        per-fire dedup (R-8).  When two profiles build the same
        arXiv query URL the fetch is executed once and the result
        shared.
    """
    cache = fetch_cache
    batch_scorer = scorer if scorer is not None else make_default_batch_scorer(config)

    async def provider(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> Iterable[BoundScoredCandidate]:
        profile_cfg = next((p for p in config.profiles if p.name == profile), None)
        if profile_cfg is None:
            _log.info("arxiv source skipped profile=%s reason=unknown_profile", profile)
            return ()

        # ── 1. Source.fetch_candidates ────────────────────────────
        source = ArxivSource(config, fetch_cache=cache)
        candidates = await source.fetch_candidates(
            profile_cfg=profile_cfg,
            kind=kind,
            run_range=run_range,
        )
        if not candidates:
            return ()

        # ── 2. Filter.score (shared score-gated seam, finding 2.3/2.4) ──
        # arXiv and RSS both score through ``influx.filter.Filter`` with
        # the source-agnostic ``BatchScorer``, so the chunk +
        # threshold-gate + drop implementation lives in one place instead
        # of a per-source copy.  The ``influx.filter`` span stays at this
        # call site (rather than moving into ``Filter.score``) so it keeps
        # arXiv's exact telemetry and does not change ``Filter`` for other
        # callers.
        arxiv_filter = Filter(
            config=config, profile_cfg=profile_cfg, scorer=batch_scorer
        )
        # Wrap the scoring call in the ``influx.filter`` span whenever a
        # scorer is wired (production default or test-injected); with no
        # scorer nothing is scored, so no span is emitted.
        span_cm = (
            get_tracer().span(
                "influx.filter",
                attributes={
                    "influx.profile": profile,
                    "influx.run_id": current_run_id.get() or "",
                    "influx.item_count": len(candidates),
                },
            )
            if batch_scorer is not None
            else nullcontext()
        )
        with span_cm:
            scored_list = await arxiv_filter.score(
                candidates, filter_prompt=filter_prompt, source="arxiv"
            )

        # ── 3. Bind per-item acquire as closures (#125) ────────────
        # ``Source.acquire`` is invoked by the Run's Acquire stage after
        # pre-acquire cache_lookup partitions cache misses + merge-bound
        # hits from outright skips.  Issue #124 still applies — the
        # closure wraps the blocking acquire in ``asyncio.to_thread`` so
        # admin endpoints stay responsive once the closure is invoked.
        bounds: list[BoundScoredCandidate] = []
        for sc in scored_list:

            async def _acquire(sc: ScoredCandidate = sc) -> dict[str, Any] | None:
                return await asyncio.to_thread(
                    source.acquire,
                    sc,
                    profile_cfg=profile_cfg,
                    config=config,
                )

            bounds.append(
                BoundScoredCandidate(
                    scored=sc,
                    acquire=_acquire,
                    source_label="arxiv",
                )
            )

        _log.info(
            "arxiv source completed profile=%s fetched=%d scored=%d",
            profile,
            len(candidates),
            len(bounds),
        )
        return bounds

    return provider
