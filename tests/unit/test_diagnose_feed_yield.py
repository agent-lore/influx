"""Unit tests for ``./scripts/influx-diagnose.py feed-yield``.

The subcommand joins the configured RSS feed list (influx.toml) against
per-feed note counts derived from the ``feed-slug:`` tag the writer
stamps on every note, surfacing dead/broken, silent, and top-producing
feeds for curation.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import patch


def _load_script() -> Any:
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "influx_diagnose", repo_root / "scripts" / "influx-diagnose.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DIAGNOSE = _load_script()


def _write_note(articles: Path, rel: str, *, feed_slug: str, created: str) -> None:
    path = articles / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        "author: influx\n"
        f"created_at: '{created}T00:00:00+00:00'\n"
        "tags:\n"
        "- source:rss\n"
        f"- feed-slug:{feed_slug}\n"
        "---\n"
        "# Title\n\n## Summary\nbody\n"
    )
    path.write_text(fm, encoding="utf-8")


# ── _is_forum_feed ──────────────────────────────────────────────────


class TestIsForumFeed:
    def test_forum_in_name(self) -> None:
        assert _DIAGNOSE._is_forum_feed("Amiga.org Forum", "https://x/feed")

    def test_new_topics_marker(self) -> None:
        assert _DIAGNOSE._is_forum_feed("Lemon64 (new topics)", "https://x")

    def test_forum_in_url(self) -> None:
        assert _DIAGNOSE._is_forum_feed("SGI", "https://forums.sgi.sh/rss")

    def test_plain_blog_is_not_forum(self) -> None:
        assert not _DIAGNOSE._is_forum_feed(
            "Simon Willison's Weblog", "https://simonwillison.net/atom"
        )


# ── _configured_rss_feeds ───────────────────────────────────────────


class TestConfiguredRssFeeds:
    def test_maps_slug_to_profile_and_forum_flag(self) -> None:
        config = {
            "profiles": [
                {
                    "name": "retro",
                    "sources": {
                        "rss": [
                            {"name": "Amiga.org Forum", "url": "https://a/feed"},
                            {"name": "Amiga-News.de", "url": "https://b/feed"},
                        ]
                    },
                },
                {
                    "name": "ai",
                    "sources": {
                        "rss": [{"name": "Lilian Weng", "url": "https://l/feed"}]
                    },
                },
            ]
        }
        feeds = _DIAGNOSE._configured_rss_feeds(config)
        assert set(feeds) == {"amiga-org-forum", "amiga-news-de", "lilian-weng"}
        assert feeds["amiga-org-forum"]["profile"] == "retro"
        assert feeds["amiga-org-forum"]["forum"] is True
        assert feeds["amiga-news-de"]["forum"] is False
        assert feeds["lilian-weng"]["profile"] == "ai"

    def test_tolerates_missing_or_malformed_sections(self) -> None:
        assert _DIAGNOSE._configured_rss_feeds({}) == {}
        assert _DIAGNOSE._configured_rss_feeds({"profiles": "nope"}) == {}
        assert (
            _DIAGNOSE._configured_rss_feeds(
                {"profiles": [{"name": "p", "sources": {}}]}
            )
            == {}
        )


# ── _scan_feed_yield ────────────────────────────────────────────────


class TestScanFeedYield:
    def test_counts_recent_and_latest(self, tmp_path: Path) -> None:
        articles = tmp_path / "articles"
        _write_note(articles, "a.md", feed_slug="foo", created="2026-06-01")
        _write_note(articles, "b.md", feed_slug="foo", created="2026-04-01")
        _write_note(articles, "c.md", feed_slug="bar", created="2026-06-05")

        out = _DIAGNOSE._scan_feed_yield(articles, since_iso="2026-05-01")
        assert out["foo"]["count"] == 2
        assert out["foo"]["recent"] == 1  # only the 06-01 note is >= 05-01
        assert out["foo"]["latest"] == "2026-06-01"
        assert out["bar"]["count"] == 1
        assert out["bar"]["recent"] == 1

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        assert _DIAGNOSE._scan_feed_yield(tmp_path / "nope") == {}

    def test_notes_without_feed_slug_ignored(self, tmp_path: Path) -> None:
        articles = tmp_path / "articles"
        path = articles / "x.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\nauthor: influx\ntags:\n- source:arxiv\n---\n# T\n",
            encoding="utf-8",
        )
        assert _DIAGNOSE._scan_feed_yield(articles) == {}


# ── cmd_feed_yield ──────────────────────────────────────────────────


class TestCmdFeedYield:
    def _config_toml(self) -> str:
        return (
            "[[profiles]]\n"
            'name = "retro"\n'
            "[[profiles.sources.rss]]\n"
            'name = "Live Feed"\n'
            'url = "https://live/feed"\n'
            "[[profiles.sources.rss]]\n"
            'name = "Dead Feed"\n'
            'url = "https://dead/feed"\n'
        )

    def test_table_flags_zero_yield_and_orphans(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        # Config: Live Feed + Dead Feed. Corpus: notes for live-feed and an
        # orphan slug not in config.
        cfg = tmp_path / "influx.toml"
        cfg.write_text(self._config_toml(), encoding="utf-8")
        articles = tmp_path / "knowledge" / "articles"
        _write_note(articles, "a.md", feed_slug="live-feed", created="2026-06-01")
        _write_note(articles, "b.md", feed_slug="gone-feed", created="2026-05-01")

        args = argparse.Namespace(
            env="staging", profile=None, since_days=3650, sort="yield", json=False
        )
        with patch.multiple(
            _DIAGNOSE,
            _load_env=lambda env: {},
            _resolve_config_path=lambda env: cfg,
            _resolve_corpus_articles_path=lambda env: articles,
        ):
            rc = _DIAGNOSE.cmd_feed_yield(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Live Feed" in out and "Dead Feed" in out
        assert "zero-yield (never produced): 1" in out  # Dead Feed
        assert "gone-feed" in out  # orphan section

    def test_json_output(self, tmp_path: Path, capsys: Any) -> None:
        import json as _json

        cfg = tmp_path / "influx.toml"
        cfg.write_text(self._config_toml(), encoding="utf-8")
        articles = tmp_path / "knowledge" / "articles"
        _write_note(articles, "a.md", feed_slug="live-feed", created="2026-06-01")

        args = argparse.Namespace(
            env="staging", profile=None, since_days=30, sort="yield", json=True
        )
        with patch.multiple(
            _DIAGNOSE,
            _load_env=lambda env: {},
            _resolve_config_path=lambda env: cfg,
            _resolve_corpus_articles_path=lambda env: articles,
        ):
            _DIAGNOSE.cmd_feed_yield(args)
        payload = _json.loads(capsys.readouterr().out)
        assert payload["since_days"] == 30
        slugs = {f["name"]: f for f in payload["feeds"]}
        assert slugs["Live Feed"]["count"] == 1
        assert slugs["Dead Feed"]["count"] == 0
