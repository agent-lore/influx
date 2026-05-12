"""Domain-aware archive acquisition policy (issue #149).

Influx archives every fetched candidate by default — download the source
document via the guarded HTTP client and persist it under
``storage.archive_dir``.  Some domains repeatedly fail this download
loop for structural reasons that no retry can fix:

* **Blocked** domains return HTTP 403 / 401 / WAF challenges to every
  unauthenticated client.  Examples observed in staging:
  ``science.org`` (Cloudflare bot challenge), ``alignmentforum.org``
  (LessWrong-family auth wall), ``therobotreport.com`` (anti-bot CDN).

* **Rate-limited** domains return HTTP 429 episodically when the run
  cadence overlaps with their per-IP throttle.  These deserve a bounded
  retry / cool-down distinct from the hard-blocked case so a polite
  back-off can recover the archive on a subsequent sweep.

Treating every archive failure as a uniform generic HTTP error means
the operator sees hundreds of ``influx:archive-missing`` repair tags
per sweep with no signal about *why* — they all look the same in
``classify_failure``.  This module introduces a tiny policy model that:

* maps domain → :class:`ArchivePolicy` (``attempt`` / ``rate_limited``
  / ``blocked`` / ``skip``),
* classifies an :class:`~influx.storage.ArchiveResult` error string into
  a stable :class:`ArchiveFailureKind` discriminator, and
* exports the tag literals (``influx:archive-blocked``,
  ``influx:archive-rate-limited``, ``influx:archive-skipped-by-policy``)
  source adapters apply when a policy short-circuits the attempt or
  reclassifies its failure.

The registry is intentionally **extensible**: built-in defaults cover
the staging-log hot offenders, and operators can extend it via
``[storage.archive_policy]`` in ``influx.toml`` (a Python override
helper :func:`build_registry` lets tests / callers compose registries
without round-tripping through TOML).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from influx.config import ArchivePolicyConfig

__all__ = [
    "ARCHIVE_BLOCKED_TAG",
    "ARCHIVE_RATE_LIMITED_TAG",
    "ARCHIVE_SKIPPED_BY_POLICY_TAG",
    "ArchiveFailureKind",
    "ArchivePolicy",
    "ArchivePolicyMode",
    "ArchivePolicyRegistry",
    "build_registry",
    "classify_failure_kind",
    "default_registry",
    "extract_domain",
    "registry_from_config",
    "tag_for_failure_kind",
]


# ── Public tag literals ────────────────────────────────────────────────
# These are the tags downstream source adapters / repair hooks add to a
# note when a policy short-circuits an archive attempt or reclassifies
# the failure.  Kept here so callers do not have to remember the literal
# string shape.

ARCHIVE_BLOCKED_TAG = "influx:archive-blocked"
ARCHIVE_RATE_LIMITED_TAG = "influx:archive-rate-limited"
ARCHIVE_SKIPPED_BY_POLICY_TAG = "influx:archive-skipped-by-policy"


# ── Failure-kind taxonomy ──────────────────────────────────────────────


# Stable discriminator for archive failures.  ``ArchiveResult.error``
# (a free-form string) is mapped into one of these values by
# :func:`classify_failure_kind` so callers can decide on tags, retries,
# and run diagnostics without re-parsing the error.
#
# ``blocked`` and ``rate_limited`` are the policy-aware HTTP failure
# kinds (HTTP 403 / 429 specifically).  ``missing_by_policy`` is set
# when the policy short-circuited the attempt entirely — no network
# call was made.
ArchiveFailureKind = Literal[
    "blocked",
    "rate_limited",
    "missing_by_policy",
    "http_403",
    "http_404",
    "http_429",
    "http_4xx",
    "http_5xx",
    "oversize",
    "timeout",
    "ssrf",
    "dns",
    "network",
    "content_type_mismatch",
    "write",
    "unknown",
]


# Patterns used by :func:`classify_failure_kind` to map free-form
# ``ArchiveResult.error`` strings onto :data:`ArchiveFailureKind`
# values.  Anchored at the start where possible so partial-match noise
# stays bounded.
_HTTP_PREFIX_RE = re.compile(r"^HTTP (\d{3})\b")
_KIND_PREFIX_RE = re.compile(r"^([a-z_]+):")


# ── Policy taxonomy ────────────────────────────────────────────────────


# Three modes for an archive policy:
#
# * ``attempt``: the default — try the download, classify failure
#   generically.  Equivalent to "no policy registered".
# * ``rate_limited``: try the download but expect occasional 429s.
#   When the failure kind is ``rate_limited`` the source applies the
#   ``influx:archive-rate-limited`` tag instead of (or alongside) the
#   generic ``influx:archive-missing`` tag, so the repair sweep can
#   apply a bounded cool-down rather than tight-looping.
# * ``blocked``: known to refuse archive downloads.  The source still
#   attempts the download (so a domain that lifts its block recovers
#   automatically), but a ``blocked`` / ``http_403`` failure is tagged
#   ``influx:archive-blocked`` and the note carries
#   ``influx:archive-missing`` exactly once rather than re-attempting
#   the same doomed path every sweep.
# * ``skip``: do not attempt the download at all.  The note is
#   tagged ``influx:archive-skipped-by-policy`` and never produces a
#   network call.  Reserved for domains where every observed attempt
#   over weeks of operation has produced 403 / 401 / WAF challenge.
ArchivePolicyMode = Literal["attempt", "rate_limited", "blocked", "skip"]


@dataclass(frozen=True, slots=True)
class ArchivePolicy:
    """A small policy decision for one archive acquisition.

    Attributes
    ----------
    mode:
        One of :data:`ArchivePolicyMode`.
    note:
        Operator-facing diagnostic string ("WAF challenge, observed
        2026-05-01 onward").  Surfaced verbatim in WARN-level logs so
        an operator inspecting why a domain was skipped does not have
        to grep the source for the policy entry.
    """

    mode: ArchivePolicyMode = "attempt"
    note: str = ""

    @property
    def should_attempt(self) -> bool:
        """Return ``True`` when the policy permits an archive download attempt.

        ``skip`` mode is the only one that short-circuits before a
        network call.  ``blocked`` and ``rate_limited`` still attempt
        the download so a domain that lifts its restriction recovers
        without an operator hand-edit.
        """
        return self.mode != "skip"


# Single sentinel for the default "no specific policy, attempt the
# download" decision.  Returned by :meth:`ArchivePolicyRegistry.policy_for`
# when no registered domain matches.
DEFAULT_POLICY = ArchivePolicy(mode="attempt", note="")


# ── Built-in registry defaults ─────────────────────────────────────────
#
# Domains observed in staging logs (issue #149) where archive
# acquisition fails for structural reasons.  These are *defaults* — an
# operator may override them via :func:`build_registry` or by editing
# the runtime registry directly.
#
# ``blocked`` domains: every recent attempt returned HTTP 403 / WAF
# challenge.  ``rate_limited`` domains: episodic 429 under normal
# cadence — try with a bounded cool-down.
#
# Suffix-matched: ``science.org`` matches ``www.science.org`` and any
# other subdomain.  See :meth:`ArchivePolicyRegistry.policy_for`.
_DEFAULT_BLOCKED_DOMAINS: tuple[tuple[str, str], ...] = (
    ("science.org", "Cloudflare bot challenge; archive download returns 403."),
    (
        "therobotreport.com",
        "Anti-bot CDN; archive download returns 403 to unauthenticated clients.",
    ),
    (
        "alignmentforum.org",
        "LessWrong-family auth wall; archive download returns 403.",
    ),
    (
        "tnmoc.org",
        "Site returns 403 to unauthenticated archive download requests.",
    ),
    (
        "lesswrong.com",
        "Auth-walled article body; archive download returns 403.",
    ),
)


_DEFAULT_RATE_LIMITED_DOMAINS: tuple[tuple[str, str], ...] = (
    # Reserved for sites observed to throttle archive downloads under
    # production cadence.  Empty for now; populated as staging logs
    # identify candidates.
)


# ── Registry ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ArchivePolicyRegistry:
    """Maps a request URL to an :class:`ArchivePolicy`.

    The registry stores a mapping of *registrable domain suffix* →
    :class:`ArchivePolicy`.  :meth:`policy_for` matches a URL's host
    against each registered suffix (longest first) so a more specific
    entry (``preview.science.org``) wins over a broader one
    (``science.org``).

    The registry is intentionally immutable — :func:`build_registry`
    constructs a new instance per config load.  Tests construct
    registries directly via the constructor.
    """

    # Map of lowercased suffix → policy.  Frozenset-of-tuples lets the
    # dataclass stay frozen while keeping efficient lookup; we copy to
    # a dict at construction so :meth:`policy_for` is O(suffix count)
    # rather than O(N**2).
    _entries: tuple[tuple[str, ArchivePolicy], ...] = field(default_factory=tuple)

    def policy_for(self, url: str) -> ArchivePolicy:
        """Return the :class:`ArchivePolicy` for *url* (default ``attempt``).

        Matches the request URL's host against each registered suffix.
        Longer (more specific) suffixes win.  Returns
        :data:`DEFAULT_POLICY` when no suffix matches — this is the
        ``mode="attempt"`` no-op case.
        """
        host = extract_domain(url)
        if not host:
            return DEFAULT_POLICY
        # Iterate longest-first so a more specific suffix wins over a
        # broader one (longest is the operator's most specific signal).
        for suffix, policy in self._entries:
            if host == suffix or host.endswith("." + suffix):
                return policy
        return DEFAULT_POLICY

    def domains(self) -> tuple[str, ...]:
        """Return the registered domain suffixes (sorted longest-first).

        Exposed for diagnostics and tests; production callers go
        through :meth:`policy_for`.
        """
        return tuple(suffix for suffix, _ in self._entries)


def build_registry(
    *,
    blocked: dict[str, str] | None = None,
    rate_limited: dict[str, str] | None = None,
    skip: dict[str, str] | None = None,
    include_defaults: bool = True,
) -> ArchivePolicyRegistry:
    """Construct an :class:`ArchivePolicyRegistry` from policy overrides.

    Operator overrides win over the built-in defaults.  Use
    ``include_defaults=False`` to start from an empty registry (e.g.
    in unit tests that need to exercise the registry's matching logic
    in isolation from the staging-defaults set).

    Parameters
    ----------
    blocked:
        Mapping of ``domain_suffix → operator-facing note`` for
        :data:`ArchivePolicyMode` ``"blocked"`` entries.
    rate_limited:
        Same shape, for ``"rate_limited"`` entries.
    skip:
        Same shape, for ``"skip"`` entries (no archive attempt at all).
    include_defaults:
        Include the built-in :data:`_DEFAULT_BLOCKED_DOMAINS` and
        :data:`_DEFAULT_RATE_LIMITED_DOMAINS` (overridable per-suffix
        by the explicit mappings).  Defaults to ``True`` so production
        behaviour is "ship with sensible defaults" out of the box.
    """
    merged: dict[str, ArchivePolicy] = {}
    if include_defaults:
        for suffix, note in _DEFAULT_BLOCKED_DOMAINS:
            merged[suffix.lower()] = ArchivePolicy(mode="blocked", note=note)
        for suffix, note in _DEFAULT_RATE_LIMITED_DOMAINS:
            merged[suffix.lower()] = ArchivePolicy(mode="rate_limited", note=note)

    # Operator overrides win.  ``skip`` then ``blocked`` then
    # ``rate_limited`` so that a later-declared mode beats an earlier
    # mode for the same suffix (last-write-wins discipline).
    overrides: tuple[tuple[Mapping[str, str] | None, ArchivePolicyMode], ...] = (
        (rate_limited, "rate_limited"),
        (blocked, "blocked"),
        (skip, "skip"),
    )
    for table, mode in overrides:
        if not table:
            continue
        for suffix, note in table.items():
            cleaned = suffix.strip().lower().lstrip(".")
            if not cleaned:
                continue
            merged[cleaned] = ArchivePolicy(mode=mode, note=note)

    # Sort longest-first so :meth:`policy_for`'s iteration order makes
    # the most specific suffix win.
    entries = tuple(sorted(merged.items(), key=lambda kv: (-len(kv[0]), kv[0])))
    return ArchivePolicyRegistry(_entries=entries)


# Module-level shared default registry — used by sources that have no
# explicit injection point (e.g. legacy direct ``download_archive``
# callers in scripts).  Production paths pass a config-derived registry
# so an operator's TOML overrides win.
_DEFAULT_REGISTRY = build_registry()


def default_registry() -> ArchivePolicyRegistry:
    """Return the module-level default :class:`ArchivePolicyRegistry`.

    Exposed for callers that have no obvious place to thread a
    config-derived registry through (e.g. CLI smoke commands).  The
    Run path passes a registry built from
    :class:`~influx.config.ArchivePolicyConfig` instead, so operator
    overrides win on production calls.
    """
    return _DEFAULT_REGISTRY


def registry_from_config(
    archive_policy: ArchivePolicyConfig,
) -> ArchivePolicyRegistry:
    """Build an :class:`ArchivePolicyRegistry` from
    :class:`~influx.config.ArchivePolicyConfig`.

    Imported separately to keep the policy module free of pydantic /
    config-load dependencies — production callers (the Run path)
    invoke this once at config-load time and pass the registry into
    :func:`download_archive` per-acquisition.
    """
    return build_registry(
        blocked=dict(archive_policy.blocked),
        rate_limited=dict(archive_policy.rate_limited),
        skip=dict(archive_policy.skip),
        include_defaults=archive_policy.include_defaults,
    )


# ── Host extraction ────────────────────────────────────────────────────


def extract_domain(url: str) -> str:
    """Return the lowercased host of *url*, or ``""`` when unparseable.

    Used by :meth:`ArchivePolicyRegistry.policy_for`.  Stripped to a
    plain ``urlparse`` because policy suffix matching wants the full
    host (including any subdomain) — a subdomain-specific policy entry
    should win over a parent-domain entry.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return ""
    host = parsed.hostname or ""
    return host.lower()


# ── Failure classification ────────────────────────────────────────────


# Map low-level NetworkError ``kind`` values onto the public
# :data:`ArchiveFailureKind` taxonomy.  Anything not listed falls
# through to ``"network"`` so :func:`classify_failure_kind` always
# returns a recognised label.
_NETWORK_KIND_TO_FAILURE: dict[str, ArchiveFailureKind] = {
    "ssrf": "ssrf",
    "scheme": "network",
    "oversize": "oversize",
    "timeout": "timeout",
    "dns": "dns",
    "content_type_mismatch": "content_type_mismatch",
    "network": "network",
}


def classify_failure_kind(
    *,
    error: str,
    policy_mode: ArchivePolicyMode = "attempt",
) -> ArchiveFailureKind:
    """Map an :class:`~influx.storage.ArchiveResult` error string onto a kind.

    ``ArchiveResult.error`` is one of:

    * ``""`` — success (callers should not invoke this helper).
    * ``"HTTP {code} for {url}"`` — non-2xx response from the guarded
      fetch.
    * ``"{kind}: {message}"`` — :class:`~influx.errors.NetworkError`
      packed by :func:`~influx.storage.download_archive`.
    * ``"write: {message}"`` — local filesystem write failure.

    The policy mode subtly influences classification: for a
    ``rate_limited`` policy, an HTTP 429 collapses to the
    ``rate_limited`` kind (its tag is ``influx:archive-rate-limited``);
    for ``blocked``, an HTTP 403 collapses to the ``blocked`` kind.
    The raw ``http_403`` / ``http_429`` kinds are still returned for
    the ``attempt`` policy so the diagnostic shape is preserved when
    no policy is active.
    """
    if not error:
        return "unknown"

    http_match = _HTTP_PREFIX_RE.match(error)
    if http_match:
        code = int(http_match.group(1))
        if code == 403:
            if policy_mode == "blocked":
                return "blocked"
            return "http_403"
        if code == 429:
            if policy_mode == "rate_limited":
                return "rate_limited"
            return "http_429"
        if code == 404:
            return "http_404"
        if 400 <= code < 500:
            return "http_4xx"
        if 500 <= code < 600:
            return "http_5xx"
        return "network"

    if error.startswith("write:"):
        return "write"

    kind_match = _KIND_PREFIX_RE.match(error)
    if kind_match:
        kind = kind_match.group(1)
        return _NETWORK_KIND_TO_FAILURE.get(kind, "network")

    return "unknown"


def tag_for_failure_kind(kind: ArchiveFailureKind) -> str | None:
    """Return the policy-driven tag for *kind*, or ``None`` for generic kinds.

    Three failure kinds carry a dedicated policy tag callers add to
    the note alongside (or in place of) the generic
    ``influx:archive-missing``:

    * ``blocked`` → :data:`ARCHIVE_BLOCKED_TAG`
    * ``rate_limited`` → :data:`ARCHIVE_RATE_LIMITED_TAG`
    * ``missing_by_policy`` → :data:`ARCHIVE_SKIPPED_BY_POLICY_TAG`

    All other kinds (``http_404``, ``timeout``, ``oversize``, …) keep
    the generic missing-tag shape — they describe transient or
    item-specific failures rather than a structural domain policy.
    """
    if kind == "blocked":
        return ARCHIVE_BLOCKED_TAG
    if kind == "rate_limited":
        return ARCHIVE_RATE_LIMITED_TAG
    if kind == "missing_by_policy":
        return ARCHIVE_SKIPPED_BY_POLICY_TAG
    return None
