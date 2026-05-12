"""Tests for the domain-aware archive acquisition policy (issue #149).

Covers:

* :class:`ArchivePolicyRegistry` longest-suffix matching and default
  fall-through.
* :func:`classify_failure_kind` mapping of free-form ``ArchiveResult.error``
  strings onto the stable :data:`ArchiveFailureKind` taxonomy.
* :func:`tag_for_failure_kind` mapping into the three public note tags.
* :func:`registry_from_config` round-trips :class:`ArchivePolicyConfig`
  into a registry that the production source adapters can use.

These tests intentionally live alongside the storage / arxiv / rss
integration tests so the policy model can evolve without coupling to
the call-site adapters.
"""

from __future__ import annotations

import pytest

from influx.archive_policy import (
    ARCHIVE_BLOCKED_TAG,
    ARCHIVE_RATE_LIMITED_TAG,
    ARCHIVE_SKIPPED_BY_POLICY_TAG,
    ArchivePolicy,
    ArchivePolicyRegistry,
    build_registry,
    classify_failure_kind,
    default_registry,
    extract_domain,
    registry_from_config,
    tag_for_failure_kind,
)
from influx.config import ArchivePolicyConfig


class TestExtractDomain:
    """``extract_domain`` returns a lowercased hostname or ``""``."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://www.science.org/journal/abc", "www.science.org"),
            ("HTTP://Example.COM/path", "example.com"),
            ("https://arxiv.org/pdf/2601.12345.pdf", "arxiv.org"),
            # Path-only / malformed → empty string, not exception.
            ("not-a-url", ""),
            ("", ""),
            # IP-literal hosts pass through verbatim.
            ("https://192.0.2.1:8443/x", "192.0.2.1"),
        ],
    )
    def test_extracts_host(self, url: str, expected: str) -> None:
        assert extract_domain(url) == expected


class TestRegistryDefaults:
    """The module-level default registry covers the staging hot offenders."""

    def test_science_org_is_blocked(self) -> None:
        policy = default_registry().policy_for("https://www.science.org/article/123")
        assert policy.mode == "blocked"
        assert policy.should_attempt is True
        assert policy.note

    def test_subdomain_inherits_parent_policy(self) -> None:
        # ``preview.science.org`` is not registered explicitly but
        # should inherit the parent suffix's policy.
        policy = default_registry().policy_for("https://preview.science.org/draft/abc")
        assert policy.mode == "blocked"

    def test_unrelated_domain_uses_default_attempt(self) -> None:
        policy = default_registry().policy_for("https://arxiv.org/pdf/2601.12345.pdf")
        assert policy.mode == "attempt"
        assert policy.should_attempt is True

    def test_alignmentforum_is_blocked(self) -> None:
        policy = default_registry().policy_for("https://www.alignmentforum.org/posts/x")
        assert policy.mode == "blocked"


class TestRegistryMatching:
    """Suffix matching is longest-first and case-insensitive."""

    def test_more_specific_suffix_wins(self) -> None:
        registry = build_registry(
            blocked={"example.com": "broad"},
            skip={"api.example.com": "more specific"},
            include_defaults=False,
        )
        # The skip-mode entry is more specific and wins.
        assert registry.policy_for("https://api.example.com/x").mode == "skip"
        # The broader entry still applies to other subdomains.
        assert registry.policy_for("https://www.example.com/y").mode == "blocked"

    def test_hostname_case_is_normalised(self) -> None:
        registry = build_registry(
            blocked={"Example.COM": "case-insensitive"},
            include_defaults=False,
        )
        assert registry.policy_for("https://example.com/x").mode == "blocked"
        assert registry.policy_for("https://EXAMPLE.com/x").mode == "blocked"

    def test_leading_dot_is_stripped(self) -> None:
        registry = build_registry(
            blocked={".example.com": "leading-dot tolerated"},
            include_defaults=False,
        )
        assert registry.policy_for("https://example.com/x").mode == "blocked"
        assert registry.policy_for("https://www.example.com/x").mode == "blocked"

    def test_unmatched_url_returns_attempt(self) -> None:
        registry = build_registry(
            blocked={"example.com": "blocked"},
            include_defaults=False,
        )
        assert registry.policy_for("https://other.test/x").mode == "attempt"

    def test_empty_url_returns_attempt(self) -> None:
        assert default_registry().policy_for("").mode == "attempt"


class TestArchivePolicyShouldAttempt:
    """``should_attempt`` short-circuits for the skip mode only."""

    @pytest.mark.parametrize(
        "mode, should_attempt",
        [
            ("attempt", True),
            ("rate_limited", True),
            ("blocked", True),
            ("skip", False),
        ],
    )
    def test_should_attempt(self, mode: str, should_attempt: bool) -> None:
        # ``mode`` is constrained by the Literal type but the test
        # exercises the runtime contract.
        policy = ArchivePolicy(mode=mode)  # type: ignore[arg-type]
        assert policy.should_attempt is should_attempt


class TestClassifyFailureKind:
    """Free-form error strings map onto stable failure-kind labels."""

    @pytest.mark.parametrize(
        "error, expected",
        [
            ("HTTP 403 for https://example.com/x", "http_403"),
            ("HTTP 429 for https://example.com/x", "http_429"),
            ("HTTP 404 for https://example.com/x", "http_404"),
            ("HTTP 451 for https://example.com/x", "http_4xx"),
            ("HTTP 503 for https://example.com/x", "http_5xx"),
            ("HTTP 600 for https://example.com/x", "network"),
            ("oversize: Response body exceeds 1000 bytes", "oversize"),
            ("timeout: Request timed out", "timeout"),
            ("ssrf: SSRF guard: localhost", "ssrf"),
            ("dns: DNS resolution failed", "dns"),
            (
                "content_type_mismatch: Content-type 'text/html' does not match",
                "content_type_mismatch",
            ),
            ("write: Permission denied", "write"),
            ("", "unknown"),
            ("totally-unrecognised-shape", "unknown"),
        ],
    )
    def test_attempt_mode_classification(self, error: str, expected: str) -> None:
        assert classify_failure_kind(error=error, policy_mode="attempt") == expected

    def test_blocked_policy_reclassifies_403(self) -> None:
        # Under a ``blocked`` policy, HTTP 403 collapses into the
        # public ``blocked`` kind so the tag mapping produces
        # ``influx:archive-blocked`` rather than the generic shape.
        assert (
            classify_failure_kind(
                error="HTTP 403 for https://example.com/x",
                policy_mode="blocked",
            )
            == "blocked"
        )

    def test_rate_limited_policy_reclassifies_429(self) -> None:
        assert (
            classify_failure_kind(
                error="HTTP 429 for https://example.com/x",
                policy_mode="rate_limited",
            )
            == "rate_limited"
        )

    def test_blocked_policy_does_not_swallow_other_codes(self) -> None:
        # A ``blocked`` policy that hits 503 / timeout should NOT
        # collapse to ``blocked`` — only 403 is the policy-driven
        # blocked signal.
        assert (
            classify_failure_kind(
                error="HTTP 503 for https://example.com/x",
                policy_mode="blocked",
            )
            == "http_5xx"
        )
        assert (
            classify_failure_kind(
                error="timeout: Request timed out",
                policy_mode="blocked",
            )
            == "timeout"
        )


class TestTagForFailureKind:
    """Mapping from failure kind onto the three public note tags."""

    @pytest.mark.parametrize(
        "kind, expected_tag",
        [
            ("blocked", ARCHIVE_BLOCKED_TAG),
            ("rate_limited", ARCHIVE_RATE_LIMITED_TAG),
            ("missing_by_policy", ARCHIVE_SKIPPED_BY_POLICY_TAG),
        ],
    )
    def test_policy_kinds_map_to_dedicated_tags(
        self, kind: str, expected_tag: str
    ) -> None:
        # ``kind`` is constrained by the Literal type but the test
        # exercises the runtime contract.
        assert tag_for_failure_kind(kind) == expected_tag  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "kind",
        [
            "http_403",
            "http_429",
            "http_404",
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
        ],
    )
    def test_generic_kinds_have_no_dedicated_tag(self, kind: str) -> None:
        # Generic failures keep the ``influx:archive-missing`` shape.
        assert tag_for_failure_kind(kind) is None  # type: ignore[arg-type]


class TestRegistryFromConfig:
    """``ArchivePolicyConfig`` round-trips into a registry."""

    def test_operator_blocked_entry_wins_over_default(self) -> None:
        # Override ``arxiv.org`` (which is NOT in the staging defaults
        # but stands in for an operator wanting to add a new domain).
        cfg = ArchivePolicyConfig(
            blocked={"arxiv.org": "operator-policy"},
            include_defaults=True,
        )
        registry = registry_from_config(cfg)
        assert registry.policy_for("https://arxiv.org/pdf/2601.12345").mode == "blocked"

    def test_operator_skip_supersedes_default_blocked(self) -> None:
        # Promote the default ``science.org`` blocked policy to ``skip``
        # via an operator-supplied override.
        cfg = ArchivePolicyConfig(
            skip={"science.org": "skip outright"},
            include_defaults=True,
        )
        registry = registry_from_config(cfg)
        policy = registry.policy_for("https://www.science.org/x")
        assert policy.mode == "skip"
        assert policy.should_attempt is False

    def test_include_defaults_false_starts_empty(self) -> None:
        cfg = ArchivePolicyConfig(include_defaults=False)
        registry = registry_from_config(cfg)
        # Without operator overrides and without defaults the registry
        # is empty — every URL falls through to ``attempt``.
        assert registry.policy_for("https://www.science.org/x").mode == "attempt"
        assert registry.domains() == ()


class TestRegistryDomainsListing:
    """``ArchivePolicyRegistry.domains`` exposes the registered suffix set."""

    def test_defaults_listed(self) -> None:
        domains = default_registry().domains()
        assert "science.org" in domains
        assert "alignmentforum.org" in domains

    def test_no_duplicates_after_merge(self) -> None:
        # If an operator override re-declares a default suffix the
        # registry should still contain that suffix exactly once.
        cfg = ArchivePolicyConfig(
            blocked={"science.org": "operator note"},
            include_defaults=True,
        )
        registry = registry_from_config(cfg)
        domains = registry.domains()
        assert domains.count("science.org") == 1


class TestArchivePolicyRegistryFromTuples:
    """The ``_entries`` constructor honours longest-first ordering."""

    def test_direct_construction_matches_longest_first(self) -> None:
        # Equivalent of build_registry() but bypassing the helper so
        # the dataclass's invariant is verified directly.
        broad = ArchivePolicy(mode="blocked", note="broad")
        specific = ArchivePolicy(mode="skip", note="specific")
        entries = (
            ("api.example.com", specific),
            ("example.com", broad),
        )
        # The constructor itself does not sort — ``build_registry``
        # is responsible for that.  Ordering matters here because the
        # registry walks entries in order and short-circuits on first
        # match.  This is a regression test for the helper's ordering
        # contract: pass entries longest-first and the specific entry
        # wins.
        registry = ArchivePolicyRegistry(_entries=entries)
        assert registry.policy_for("https://api.example.com/x").mode == "skip"
