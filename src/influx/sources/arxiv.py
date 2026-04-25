"""arXiv Atom feed fetcher with client-side date filtering.

Queries ``https://export.arxiv.org/api/query`` for configured categories,
parses the Atom response with stdlib ``xml.etree.ElementTree``, and applies
client-side date filtering against ``profile.sources.arxiv.lookback_days``
(FR-SRC-1, FR-SRC-2).

Retry behaviour:
- HTTP 429 → sleep ``resilience.arxiv_429_backoff_seconds`` then retry
  (FR-RES-2)
- Other transient failures → exponential backoff from
  ``resilience.backoff_base_seconds`` (FR-RES-1)
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from influx.config import ArxivSourceConfig, ResilienceConfig
from influx.errors import NetworkError
from influx.http_client import guarded_fetch

__all__ = [
    "ArxivItem",
    "build_query_url",
    "fetch_arxiv",
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


@dataclass(frozen=True, slots=True)
class ArxivItem:
    """A single parsed arXiv entry from the Atom feed."""

    arxiv_id: str
    title: str
    abstract: str
    published: datetime
    categories: list[str]


# ── Query URL construction ─────────────────────────────────────────


def build_query_url(
    *,
    categories: list[str],
    max_results: int,
) -> str:
    """Build the arXiv API query URL per FR-SRC-1.

    Constructs ``search_query`` as an OR-joined expression
    (``cat:X+OR+cat:Y+...``), ``sortBy=submittedDate``,
    ``sortOrder=descending``, and ``max_results`` from the profile.
    """
    cat_expr = "+OR+".join(f"cat:{c}" for c in categories)
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


def fetch_arxiv(
    *,
    arxiv_config: ArxivSourceConfig,
    resilience: ResilienceConfig,
    now: datetime | None = None,
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

    Returns
    -------
    list[ArxivItem]
        Parsed and date-filtered items, newest first.
    """
    url = build_query_url(
        categories=arxiv_config.categories,
        max_results=arxiv_config.max_results_per_category,
    )

    body = _fetch_with_retry(
        url=url,
        resilience=resilience,
    )

    items = _parse_atom(body)
    return _filter_by_lookback(
        items,
        arxiv_config.lookback_days,
        now=now,
    )


def _fetch_with_retry(
    *,
    url: str,
    resilience: ResilienceConfig,
) -> bytes:
    """Fetch *url* with 429 backoff and exponential retry (FR-RES-1/2)."""
    max_retries = resilience.max_retries
    backoff_base = resilience.backoff_base_seconds
    backoff_429 = resilience.arxiv_429_backoff_seconds

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            # Fetch without expected_content_type so status-code handling
            # (429 backoff, 5xx retry) runs first. Non-XML 429/5xx
            # responses would otherwise be raised as content-type errors
            # before reaching the rate-limit branch (FR-RES-2).
            result = guarded_fetch(url)
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
                _sleep(delay)
                continue
            raise

        if result.status_code == 429:
            last_error = NetworkError(
                f"HTTP 429 from arXiv API at {url}",
                url=url,
                kind="rate_limit",
            )
            if attempt < max_retries:
                _log.warning(
                    "arXiv 429 on attempt %d/%d, backing off %ds (FR-RES-2)",
                    attempt + 1,
                    max_retries + 1,
                    backoff_429,
                )
                _sleep(backoff_429)
                continue
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

        return result.body

    # Should not reach here, but satisfy type checker
    assert last_error is not None  # noqa: S101
    raise last_error
