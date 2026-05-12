"""Tests for archive-policy tag emission in source adapters (issue #149).

Verifies the cross-product behaviour:

* ``build_rss_note_item`` and ``build_arxiv_note_item`` consult the
  per-domain :class:`~influx.archive_policy.ArchivePolicy` (built from
  :class:`~influx.config.ArchivePolicyConfig` on the loaded config).
* When the policy short-circuits (``skip``) or reclassifies the failure
  (``blocked`` / ``rate_limited``), the resulting note carries the
  policy-driven tag (``influx:archive-blocked`` /
  ``influx:archive-rate-limited`` / ``influx:archive-skipped-by-policy``).
* ``blocked`` and ``missing_by_policy`` notes also carry
  ``influx:archive-terminal`` so the repair sweep does not tight-loop on
  a doomed path (this is the staging-log behaviour the issue fixes).
* A generic non-policy failure (404, oversize, …) keeps the existing
  ``influx:archive-missing`` shape with NO policy tag — guaranteeing
  zero behaviour change for the common case.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ArchivePolicyConfig,
    ExtractionConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    RssSourceEntry,
    ScheduleConfig,
    SecurityConfig,
    StorageConfig,
)
from influx.sources.arxiv import ArxivItem, build_arxiv_note_item
from influx.sources.rss import RssFeedItem, build_rss_note_item
from influx.storage import ArchiveResult


def _profile() -> ProfileConfig:
    return ProfileConfig(
        name="ai-robotics",
        description="AI and robotics research",
        thresholds=ProfileThresholds(
            relevance=7, full_text=100, deep_extract=100
        ),
    )


def _make_config(
    *,
    blocked: dict[str, str] | None = None,
    rate_limited: dict[str, str] | None = None,
    skip: dict[str, str] | None = None,
    include_defaults: bool = False,
    archive_dir: str = "/archive",
) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        schedule=ScheduleConfig(),
        storage=StorageConfig(
            archive_dir=archive_dir,
            archive_policy=ArchivePolicyConfig(
                blocked=blocked or {},
                rate_limited=rate_limited or {},
                skip=skip or {},
                include_defaults=include_defaults,
            ),
        ),
        profiles=[_profile()],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(),
    )


# ── RSS adapter × policy ──────────────────────────────────────────────


def _make_rss_item(url: str = "https://example.com/article") -> RssFeedItem:
    return RssFeedItem(
        title="Test Article",
        url=url,
        published=datetime(2026, 4, 25, tzinfo=UTC),
        summary="A summary of the article.",
        source_tag="rss",
        feed_name="example-feed",
    )


class TestRssArchivePolicyTags:
    """``build_rss_note_item`` applies the policy tag for known domains."""

    @patch("influx.sources.rss.download_archive")
    def test_skip_policy_emits_skipped_by_policy_tag(
        self, mock_dl: object
    ) -> None:
        # The skip-mode policy short-circuits ``download_archive`` so
        # the mock must return the same shape the real function would.
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=False,
            rel_posix_path=None,
            error="missing_by_policy: skip outright",
            failure_kind="missing_by_policy",
            policy_mode="skip",
            domain="blocked.example",
        )
        config = _make_config(skip={"blocked.example": "skip outright"})
        item = _make_rss_item(url="https://blocked.example/article")

        result = build_rss_note_item(item=item, profile_name="ai-robotics", config=config)

        tags = cast(list[str], result["tags"])
        assert "influx:archive-skipped-by-policy" in tags
        assert "influx:archive-missing" in tags
        # Blocked / skip flip the note terminal so the repair sweep
        # does NOT tight-loop on the doomed path.
        assert "influx:archive-terminal" in tags

    @patch("influx.sources.rss.download_archive")
    def test_blocked_policy_emits_blocked_tag(self, mock_dl: object) -> None:
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=False,
            rel_posix_path=None,
            error="HTTP 403 for https://science.example/x",
            failure_kind="blocked",
            policy_mode="blocked",
            domain="science.example",
        )
        config = _make_config(blocked={"science.example": ""})
        item = _make_rss_item(url="https://science.example/article")

        result = build_rss_note_item(item=item, profile_name="ai-robotics", config=config)

        tags = cast(list[str], result["tags"])
        assert "influx:archive-blocked" in tags
        assert "influx:archive-missing" in tags
        assert "influx:archive-terminal" in tags

    @patch("influx.sources.rss.download_archive")
    def test_rate_limited_policy_emits_rate_limited_tag(
        self, mock_dl: object
    ) -> None:
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=False,
            rel_posix_path=None,
            error="HTTP 429 for https://slow.example/x",
            failure_kind="rate_limited",
            policy_mode="rate_limited",
            domain="slow.example",
        )
        config = _make_config(rate_limited={"slow.example": ""})
        item = _make_rss_item(url="https://slow.example/article")

        result = build_rss_note_item(item=item, profile_name="ai-robotics", config=config)

        tags = cast(list[str], result["tags"])
        assert "influx:archive-rate-limited" in tags
        assert "influx:archive-missing" in tags
        # Rate-limited retries on cool-down — must NOT be flipped terminal.
        assert "influx:archive-terminal" not in tags

    @patch("influx.sources.rss.download_archive")
    def test_generic_failure_keeps_archive_missing_only(
        self, mock_dl: object
    ) -> None:
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=False,
            rel_posix_path=None,
            error="HTTP 404 for https://example.com/x",
            failure_kind="http_404",
            policy_mode="attempt",
            domain="example.com",
        )
        config = _make_config()
        item = _make_rss_item(url="https://example.com/article")

        result = build_rss_note_item(item=item, profile_name="ai-robotics", config=config)

        tags = cast(list[str], result["tags"])
        assert "influx:archive-missing" in tags
        # No policy-driven tag.
        assert "influx:archive-blocked" not in tags
        assert "influx:archive-rate-limited" not in tags
        assert "influx:archive-skipped-by-policy" not in tags
        # Not flipped terminal.
        assert "influx:archive-terminal" not in tags

    @patch("influx.sources.rss.download_archive")
    def test_successful_archive_emits_no_policy_tag(
        self, mock_dl: object
    ) -> None:
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=True,
            rel_posix_path="rss/2026/04/example-feed-2026-04-25-abc.html",
            error="",
            failure_kind="",
            policy_mode="attempt",
            domain="example.com",
        )
        config = _make_config()
        item = _make_rss_item(url="https://example.com/article")

        result = build_rss_note_item(item=item, profile_name="ai-robotics", config=config)

        tags = cast(list[str], result["tags"])
        assert "influx:archive-missing" not in tags
        assert "influx:archive-blocked" not in tags
        assert "influx:archive-rate-limited" not in tags
        assert "influx:archive-skipped-by-policy" not in tags


# ── arXiv adapter × policy ────────────────────────────────────────────


def _make_arxiv_item() -> ArxivItem:
    return ArxivItem(
        arxiv_id="2601.12345",
        title="Test Paper",
        abstract="An abstract.",
        published=datetime(2026, 4, 25, tzinfo=UTC),
        categories=["cs.AI"],
    )


class TestArxivArchivePolicyTags:
    """``build_arxiv_note_item`` applies the policy tag when set.

    arXiv itself is not in the staging-defaults blocked list, so these
    tests use operator-supplied overrides to exercise the policy
    integration end-to-end without relying on a particular default.
    """

    @patch("influx.sources.arxiv.download_archive")
    def test_blocked_policy_for_arxiv_marks_terminal(
        self, mock_dl: object
    ) -> None:
        # Hypothetical operator override: pretend arxiv.org is blocked
        # in this profile.  Exercises that the arxiv adapter honours
        # the policy registry instead of treating every 403 as generic.
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=False,
            rel_posix_path=None,
            error="HTTP 403 for https://arxiv.org/pdf/2601.12345.pdf",
            failure_kind="blocked",
            policy_mode="blocked",
            domain="arxiv.org",
        )
        config = _make_config(blocked={"arxiv.org": "operator override"})

        result = build_arxiv_note_item(
            item=_make_arxiv_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        tags = cast(list[str], result["tags"])
        assert "influx:archive-blocked" in tags
        assert "influx:archive-missing" in tags
        assert "influx:archive-terminal" in tags

    @patch("influx.sources.arxiv.download_archive")
    def test_rate_limited_policy_for_arxiv_does_not_terminalise(
        self, mock_dl: object
    ) -> None:
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=False,
            rel_posix_path=None,
            error="HTTP 429 for https://arxiv.org/pdf/2601.12345.pdf",
            failure_kind="rate_limited",
            policy_mode="rate_limited",
            domain="arxiv.org",
        )
        config = _make_config(rate_limited={"arxiv.org": "operator override"})

        result = build_arxiv_note_item(
            item=_make_arxiv_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        tags = cast(list[str], result["tags"])
        assert "influx:archive-rate-limited" in tags
        assert "influx:archive-missing" in tags
        # Must NOT be flipped terminal — rate-limited retries on cool-down.
        assert "influx:archive-terminal" not in tags

    @patch("influx.sources.arxiv.download_archive")
    def test_generic_404_unchanged_arxiv_shape(self, mock_dl: object) -> None:
        # Regression: with policy enabled but for unrelated domains,
        # an arxiv 404 keeps its existing shape.
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=False,
            rel_posix_path=None,
            error="HTTP 404 for https://arxiv.org/pdf/2601.12345.pdf",
            failure_kind="http_404",
            policy_mode="attempt",
            domain="arxiv.org",
        )
        config = _make_config(blocked={"science.example": ""})

        result = build_arxiv_note_item(
            item=_make_arxiv_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        tags = cast(list[str], result["tags"])
        assert "influx:archive-missing" in tags
        assert "influx:archive-blocked" not in tags
        assert "influx:archive-rate-limited" not in tags
        assert "influx:archive-skipped-by-policy" not in tags
        assert "influx:archive-terminal" not in tags

    @patch("influx.sources.arxiv.download_archive")
    def test_arxiv_calls_through_with_policy_registry(
        self, mock_dl: object
    ) -> None:
        """Smoke: ``download_archive`` receives a ``policy_registry``
        argument so the call site honours operator overrides.
        """
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=True,
            rel_posix_path="arxiv/2026/04/2601.12345.pdf",
            error="",
            policy_mode="attempt",
            domain="arxiv.org",
        )
        config = _make_config()

        build_arxiv_note_item(
            item=_make_arxiv_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        call = mock_dl.call_args  # type: ignore[attr-defined]
        assert "policy_registry" in call.kwargs
        # Registry constructed from the empty config — has no
        # operator overrides and ``include_defaults=False`` per
        # ``_make_config`` so the registry is empty (and arxiv.org
        # falls through to attempt).
        assert call.kwargs["policy_registry"].domains() == ()


# ── Suffix entry: parametrised cross-product over policy modes ─────────


@pytest.mark.parametrize(
    "policy_mode, failure_kind, expected_tag, terminalises",
    [
        ("blocked", "blocked", "influx:archive-blocked", True),
        ("rate_limited", "rate_limited", "influx:archive-rate-limited", False),
        (
            "skip",
            "missing_by_policy",
            "influx:archive-skipped-by-policy",
            True,
        ),
    ],
)
class TestPolicyModeTagCrossProduct:
    """Cross-product matrix: policy mode × tag emission × terminal flag.

    Mirrors the AC list in #149:

    * Known-blocked domains classify as ``archive_blocked`` instead of
      generic failures.
    * Known rate-limited domains use bounded retry distinct from
      hard-blocked.
    * Run diagnostics distinguish at least: blocked, rate-limited,
      missing-by-policy, and generic archive failure.
    """

    @patch("influx.sources.rss.download_archive")
    def test_rss_emits_expected_tag(
        self,
        mock_dl: object,
        policy_mode: str,
        failure_kind: str,
        expected_tag: str,
        terminalises: bool,
    ) -> None:
        mock_dl.return_value = ArchiveResult(  # type: ignore[union-attr]
            ok=False,
            rel_posix_path=None,
            error=f"{failure_kind}: synthetic",
            failure_kind=failure_kind,
            policy_mode=policy_mode,
            domain="example.com",
        )
        config = _make_config()

        result = build_rss_note_item(
            item=_make_rss_item(url="https://example.com/x"),
            profile_name="ai-robotics",
            config=config,
        )
        tags = cast(list[str], result["tags"])

        assert expected_tag in tags
        assert "influx:archive-missing" in tags
        if terminalises:
            assert "influx:archive-terminal" in tags
        else:
            assert "influx:archive-terminal" not in tags
