"""Thin-summary suppression helper (issue #166).

When an item's archive fetch fails for any reason, Influx today still
writes a Lithos note built from the feed-provided ``<summary>`` / arXiv
``<abstract>`` plus the title.  In practice many of those summaries are
operationally garbage — pointer-style strings like ``"Discussion (47
points)"``, single-line title repetitions, or generic "read the full
article at…" teasers — that accumulate in Lithos with no recovery path.

This module provides a pure structural test the source adapters call
*before* constructing a summary-only note.  If the rule fires the
adapter drops the item entirely (no note, no ``influx:archive-missing``
tag, no repair-sweep entry); the drop is surfaced via dedicated
telemetry so the suppression is visible to operators without polluting
the existing archive-failure counters.

The rule is **any-of** across three checks:

* **Length** — the trimmed summary is shorter than ``min_chars``.
  Skipped entirely when ``min_chars == 0`` so an operator can disable
  the length test (but title-equality / boilerplate remain on).
* **Title-equality** — the summary, after a normalisation pass
  (lowercase, strip Unicode punctuation, collapse whitespace), equals
  the title under the same normalisation.  Catches feeds that mirror
  the title field into the summary with no extra content.
* **Boilerplate match** — the trimmed summary matches one of a small
  built-in set of regex patterns covering common RSS pointers /
  teasers / ellipsis markers.  See :data:`_BOILERPLATE_PATTERNS`; the
  staging runbook documents each entry's rationale so operators can
  propose additions.

Pure module — no IO, no global state, no Lithos client.  All run-time
data flows through the function arguments so tests pass synthetic
strings directly.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

__all__ = [
    "BOILERPLATE_PATTERNS",
    "ThinSummaryRule",
    "is_thin_summary",
]


ThinSummaryRule = Literal["length", "title_equality", "boilerplate"]


# Module-level constant of (compiled-regex, operator-facing rationale)
# pairs.  Listed in the staging runbook verbatim so operators can decide
# whether to propose additions.  Each pattern is anchored / shaped to
# fire on the *whole* summary string after whitespace trimming, so a
# legitimate article body that happens to mention "continue reading"
# inline is not affected — only summaries that *are* the boilerplate.
_BOILERPLATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^Discussion \(\d+ points?\)$", re.IGNORECASE),
        "Hacker News pointer summary — the feed surfaces only the "
        "comment-thread points count, not the article body.",
    ),
    (
        re.compile(r"^Comments(\.|\s|$)", re.IGNORECASE),
        "HN / Reddit pointer summary — the feed item is just a link to "
        "the comment thread.",
    ),
    (
        re.compile(
            r"^Read (the |this )?(full |entire )?(article|post|story|entry) "
            r"(at|on|via)\s",
            re.IGNORECASE,
        ),
        "Generic teaser — the feed publisher truncates the body to a "
        "'read the full article at…' pointer with no real content.",
    ),
    (
        re.compile(r"^Continue reading\b", re.IGNORECASE),
        "WordPress / Substack-style truncation marker with no extra "
        "content beyond the title.",
    ),
    (
        re.compile(r"^Read more\b", re.IGNORECASE),
        "Generic 'Read more' truncation marker — the feed body is effectively empty.",
    ),
    (
        re.compile(r"^[…\.\s\[\]]*$"),
        "Empty-ish marker — the summary is only ellipsis / bracket "
        "characters / whitespace, no real text at all.",
    ),
)


# Public re-export so callers / tests can introspect the pattern list
# without reaching into a private name.  The list is intentionally
# small; new entries should land alongside a runbook update.
BOILERPLATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = _BOILERPLATE_PATTERNS


def _normalize_for_equality(text: str) -> str:
    """Normalise *text* for the title-equality check.

    Lowercases, strips characters with Unicode general-category starting
    with ``P`` (all punctuation classes — ``Pc``, ``Pd``, ``Pe``, ``Pf``,
    ``Pi``, ``Po``, ``Ps``), collapses internal whitespace to single
    spaces, and trims leading / trailing whitespace.  Two strings that
    differ only in punctuation / casing / whitespace compare equal
    under this transform.
    """
    stripped_chars = (ch for ch in text if not unicodedata.category(ch).startswith("P"))
    no_punct = "".join(stripped_chars).lower()
    return " ".join(no_punct.split())


def is_thin_summary(
    *, summary: str, title: str, min_chars: int
) -> tuple[bool, ThinSummaryRule | None]:
    """Decide whether *summary* is "thin" relative to *title* / *min_chars*.

    Returns ``(True, rule_name)`` on first match, where ``rule_name`` is
    the rule that fired (``"length"`` / ``"title_equality"`` /
    ``"boilerplate"``).  Returns ``(False, None)`` when the summary
    passes all three checks.

    Parameters
    ----------
    summary:
        The (post-extraction-fallback) summary that would otherwise be
        used to build the Lithos note.  Pass the *final* string — if
        the source adapter has already replaced the feed summary with
        extracted body text, pass the extracted text; the rule then
        only fires when the extraction also produced a thin string.
    title:
        The item title.  Used only by the title-equality check.
    min_chars:
        Length-rule threshold.  When ``0`` the length check is skipped
        entirely; title-equality / boilerplate continue to fire.  Must
        be non-negative — negative values are treated as ``0`` so a
        misconfigured override cannot raise inside the source adapter.

    Notes
    -----
    Order matters: length is checked first (cheapest), then
    title-equality (one normalisation pass), then the boilerplate
    pattern list (regex sweep).  The returned ``rule_name`` reflects
    the *first* match so the INFO log line a caller emits points the
    operator at the actionable rule.
    """
    trimmed = summary.strip()
    safe_min_chars = max(min_chars, 0)

    if safe_min_chars > 0 and len(trimmed) < safe_min_chars:
        return True, "length"

    if _normalize_for_equality(trimmed) == _normalize_for_equality(title):
        return True, "title_equality"

    for pattern, _rationale in _BOILERPLATE_PATTERNS:
        if pattern.match(trimmed):
            return True, "boilerplate"

    return False, None
