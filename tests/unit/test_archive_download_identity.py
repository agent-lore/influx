"""Tests for ``Source.archive_download_identity`` (finding 3b).

The archive re-download identity reconstruction moved off ``repair_hooks``
onto the Source adapters that own each scheme
(``ArxivSource.archive_download_identity`` /
``RssSource.archive_download_identity``) plus the shared note-envelope
readers in :mod:`influx.source`.  The adapter that *builds* a note's
identity at acquire time now also owns the *inverse* that rebuilds it, so
the two cannot drift.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from influx.source import year_month_from_created_at, year_month_from_note_path
from influx.sources.arxiv import ArxivSource, _year_month_from_arxiv_id
from influx.sources.rss import RssSource, _rss_item_id_from_note
from influx.urls import url_hash


def _arxiv() -> ArxivSource:
    # archive_download_identity reads only the note, never config.
    return ArxivSource(MagicMock())


def _rss() -> RssSource:
    return RssSource(MagicMock())


class TestYearMonthHelpers:
    """#223: year/month must resolve when the read_note envelope has no path."""

    def test_year_month_from_arxiv_id(self) -> None:
        assert _year_month_from_arxiv_id("2605.10178") == (2026, 5)
        assert _year_month_from_arxiv_id("2412.00001") == (2024, 12)
        assert _year_month_from_arxiv_id("2613.00001") is None  # month 13
        assert _year_month_from_arxiv_id("not-an-id") is None

    def test_year_month_from_created_at(self) -> None:
        assert year_month_from_created_at(
            {"created_at": "2026-05-31T09:22:58+00:00"}
        ) == (2026, 5)
        assert year_month_from_created_at({"created_at": "2026-13-01"}) is None  # mo 13
        assert year_month_from_created_at({"created_at": "garbage"}) is None
        # nested under metadata
        assert year_month_from_created_at(
            {"metadata": {"created_at": "2026-04-02T00:00:00+00:00"}}
        ) == (2026, 4)
        assert year_month_from_created_at({"id": "x"}) is None

    def test_year_month_from_note_path(self) -> None:
        assert year_month_from_note_path({"path": "papers/arxiv/2024/03"}) == (2024, 3)
        assert year_month_from_note_path({"path": None}) is None
        assert year_month_from_note_path({}) is None


class TestArxivArchiveDownloadIdentity:
    def test_resolves_from_envelope_shape(self) -> None:
        note = {
            "id": "56868609-29d7-46c2-9325-2dc56fb1f108",
            "source_url": "https://arxiv.org/abs/2605.10178",
            "path": None,
            "created_at": "2026-05-12T02:24:14+00:00",
            "tags": ["arxiv-id:2605.10178", "source:arxiv", "influx:repair-needed"],
        }
        identity = _arxiv().archive_download_identity(note)
        assert identity is not None
        assert identity.url == "https://arxiv.org/pdf/2605.10178.pdf"
        assert identity.item_id == "2605.10178"
        # year/month from the arxiv id's YYMM, since path is absent.
        assert identity.published_year == 2026
        assert identity.published_month == 5
        assert identity.ext == ".pdf"
        assert identity.expected_content_type == "pdf"

    def test_prefers_path_over_arxiv_id_yymm(self) -> None:
        note = {
            "path": "papers/arxiv/2024/03",
            "created_at": "2026-05-01",
            "tags": ["arxiv-id:2605.10178", "source:arxiv"],
        }
        identity = _arxiv().archive_download_identity(note)
        assert identity is not None
        assert (identity.published_year, identity.published_month) == (2024, 3)

    def test_falls_back_to_created_at_for_legacy_id(self) -> None:
        # legacy arxiv id (no YYMM prefix), no path -> created_at
        note = {
            "path": None,
            "created_at": "2026-04-02T00:00:00+00:00",
            "tags": ["arxiv-id:math.GT/0309136", "source:arxiv"],
        }
        identity = _arxiv().archive_download_identity(note)
        assert identity is not None
        assert (identity.published_year, identity.published_month) == (2026, 4)

    def test_none_when_no_arxiv_id_tag(self) -> None:
        note = {"id": "x", "path": "papers/arxiv/2024/03", "tags": ["source:arxiv"]}
        assert _arxiv().archive_download_identity(note) is None

    def test_none_when_no_year_month(self) -> None:
        # legacy id (no YYMM), path without year/month, no created_at
        note = {
            "id": "x",
            "path": "papers/arxiv/",
            "tags": ["arxiv-id:math.GT/0309136", "source:arxiv"],
        }
        assert _arxiv().archive_download_identity(note) is None


class TestRssArchiveDownloadIdentity:
    def test_resolves_from_envelope_shape(self) -> None:
        url = "https://www.alignmentforum.org/posts/Cmk/advice"
        note = {
            "id": "4c9c8175-605e-41fe-807c-5438ca40d1d7",  # Lithos UUID, no rss-
            "source_url": url,
            "path": None,
            "created_at": "2026-05-31T09:22:58+00:00",
            "tags": [
                "source:rss",
                "feed-slug:ai-alignment-forum",
                "influx:archive-missing",
                "influx:repair-needed",
            ],
        }
        identity = _rss().archive_download_identity(note)
        assert identity is not None
        assert identity.url == url
        # The retry item_id is the date-free ``{feed-slug}-{url-hash}`` — it
        # equals the note *id* minus its ``rss-`` prefix, NOT acquisition's
        # dated archive item_id (see test_retry_item_id_omits_acquisition_date).
        assert identity.item_id == f"ai-alignment-forum-{url_hash(url)}"
        assert identity.published_year == 2026
        assert identity.published_month == 5
        assert identity.ext == ".html"
        assert identity.expected_content_type == "html"

    def test_retry_item_id_omits_acquisition_date(self) -> None:
        # Honest divergence (finding 3b review): acquisition archives RSS
        # HTML under item_id ``{feed-slug}-{YYYY-MM-DD}-{url-hash}``
        # (build_rss_note_item), but read_note does not persist that
        # publication *day*.  The retry identity is the deterministic,
        # date-free ``{feed-slug}-{url-hash}``, so re-download lands at a
        # different (still deterministic) archive path than the original —
        # fine, since the archive is missing and nothing lives at the
        # original dated path.  The co-located, drift-proof part is the
        # shared feed-slug + url_hash *scheme*, not the day.
        url = "https://example.com/post"
        note = {
            "id": f"rss-techcrunch-{url_hash(url)}",
            "source_url": url,
            "path": "articles/rss-techcrunch/2026/05",
            "tags": ["source:rss", "feed-slug:techcrunch"],
        }
        identity = _rss().archive_download_identity(note)
        assert identity is not None
        assert identity.item_id == f"techcrunch-{url_hash(url)}"
        # Explicitly NOT acquisition's dated ``{feed-slug}-2026-05-DD-{hash}``.
        assert "2026-05" not in identity.item_id

    def test_none_when_no_source_url(self) -> None:
        note = {"id": "rss-x-abc", "tags": ["source:rss"]}
        assert _rss().archive_download_identity(note) is None

    def test_none_when_item_id_unrecoverable(self) -> None:
        # UUID id (no rss- prefix), no feed-slug tag -> cannot reconstruct item_id.
        note = {
            "id": "uuid",
            "source_url": "https://example.com/a",
            "created_at": "2026-05-01",
            "tags": ["source:rss"],
        }
        assert _rss().archive_download_identity(note) is None

    def test_none_when_no_year_month(self) -> None:
        note = {
            "id": "rss-techcrunch-abc123",
            "source_url": "https://example.com/a",
            "path": None,
            "tags": ["source:rss", "feed-slug:techcrunch"],
        }
        assert _rss().archive_download_identity(note) is None


class TestRssItemIdReconstruction:
    """#223: reconstruct the RSS archive item_id from the read_note envelope."""

    def test_strips_rss_prefix_when_present(self) -> None:
        assert (
            _rss_item_id_from_note({"id": "rss-techcrunch-abc123"})
            == "techcrunch-abc123"
        )

    def test_reconstructs_from_feed_slug_and_source_url(self) -> None:
        url = "https://www.alignmentforum.org/posts/Cmk/advice"
        note = {
            "id": "4c9c8175-605e-41fe-807c-5438ca40d1d7",  # Lithos UUID, no rss-
            "source_url": url,
            "tags": ["source:rss", "feed-slug:ai-alignment-forum"],
        }
        assert _rss_item_id_from_note(note) == f"ai-alignment-forum-{url_hash(url)}"

    def test_none_when_no_prefix_and_no_reconstruction_inputs(self) -> None:
        # UUID id, no feed-slug tag -> cannot reconstruct.
        assert _rss_item_id_from_note({"id": "uuid", "tags": ["source:rss"]}) is None
