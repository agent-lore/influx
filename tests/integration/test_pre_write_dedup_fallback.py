"""Integration tests for pre-write dedup source-URL fallback (issue #128).

The pre-write dedup contract has two stages:

1. **Primary lookup** — title + first sentence of abstract, sent through
   :func:`influx.dedup.compose_dedup_query`.
2. **Fallback lookup** — exact ``source_url`` only, used as a defensive
   second chance when the primary misses.

Either hit treats the item as a cache hit for the rest of
:func:`influx.run._run_ingest_stage` (skip on backfill, multi-profile
merge otherwise).  These tests exercise that wiring end-to-end against
:class:`tests.contract.test_lithos_client.FakeLithosServer`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

from influx.coordinator import RunKind
from influx.lcma_wiring import LcmaWiringDeps
from influx.lithos_client import LithosClient
from influx.run import RunPlan, _run_ingest_stage
from tests.contract.test_lithos_client import FakeLithosServer

PROFILE = "alpha"
ARXIV_URL = "https://arxiv.org/abs/2601.00001"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def fake_lithos() -> Generator[FakeLithosServer, None, None]:
    server = FakeLithosServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def fake_lithos_url(fake_lithos: FakeLithosServer) -> str:
    return f"http://127.0.0.1:{fake_lithos.port}/sse"


@pytest.fixture(autouse=True)
def clear_fakes(fake_lithos: FakeLithosServer) -> None:
    fake_lithos.calls.clear()
    fake_lithos.write_responses.clear()
    fake_lithos.read_responses.clear()
    fake_lithos.cache_lookup_responses.clear()
    fake_lithos.list_responses.clear()
    fake_lithos.retrieve_responses.clear()
    fake_lithos.edge_upsert_responses.clear()
    fake_lithos.task_create_responses.clear()
    fake_lithos.task_complete_responses.clear()


# ── Helpers ──────────────────────────────────────────────────────────


def _make_item(*, source_url: str = ARXIV_URL) -> dict[str, Any]:
    """Build a minimal ProfileItem suitable for the ingest stage."""
    return {
        "id": "note-1",
        "title": "Attention Is All You Need",
        "source_url": source_url,
        "source": "arxiv",
        "abstract_or_summary": (
            "We propose a new architecture based solely on attention."
        ),
        "content": "# Attention Is All You Need\n\nbody",
        "path": f"papers/{source_url.rsplit('/', 1)[-1]}.md",
        "tags": ["arxiv-id:2601.00001"],
        "score": 7,
        "confidence": 0.9,
        "filter_tags": [],
    }


def _run_ingest(
    *,
    fake_lithos_url: str,
    items: tuple[dict[str, Any], ...],
    skip_cache_hits: bool = False,
) -> Any:
    """Drive ``_run_ingest_stage`` against a fresh client for one profile.

    Builds and closes the :class:`LithosClient` inside a single
    ``asyncio.run`` call so the SSE task group's cancel scope stays
    inside one event loop (mcp's anyio plumbing breaks otherwise).
    """
    plan = RunPlan(
        profile=PROFILE,
        kind=RunKind.BACKFILL if skip_cache_hits else RunKind.SCHEDULED,
        skip_cache_hits=skip_cache_hits,
    )

    async def _run() -> Any:
        client = LithosClient(url=fake_lithos_url)
        try:
            lcma_deps = LcmaWiringDeps(
                client=client,
                profile=PROFILE,
                run_task_id="task-1",
                lcma_edge_score=0.75,
            )
            return await _run_ingest_stage(
                plan,
                items=items,
                client=client,
                lcma_deps=lcma_deps,
                ledger=None,
            )
        finally:
            await client.close()

    return asyncio.run(_run())


def _cache_lookup_calls(fake_lithos: FakeLithosServer) -> list[dict[str, str]]:
    return [c[1] for c in fake_lithos.calls if c[0] == "lithos_cache_lookup"]


def _write_calls(fake_lithos: FakeLithosServer) -> list[dict[str, Any]]:
    return [c[1] for c in fake_lithos.calls if c[0] == "lithos_write"]


# ── Tests ────────────────────────────────────────────────────────────


class TestPreWriteDedupFallback:
    """Pre-write dedup falls back to source_url-only lookup on primary miss."""

    def test_url_fallback_hit_detected_by_pre_write_path(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Primary miss + URL-fallback hit → RPC sequence + metric increment.

        Reproduces the issue #128 false-negative shape: title-based
        dedup misses but the source_url is already in Lithos.  The
        fallback path catches it pre-write — the duplicate is no longer
        silently discovered only when ``write_note`` is attempted.

        The downstream effect (skip on backfill, merge on scheduled)
        is covered by the dedicated tests below.
        """
        # Primary text-query lookup misses; URL-only fallback hits.
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": False, "stale_exists": False})
        )
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        with patch("influx.run.metrics.cache_hits_via_url_fallback") as fallback_metric:
            _run_ingest(
                fake_lithos_url=fake_lithos_url,
                items=(_make_item(),),
            )
            fallback_metric.assert_called()
            fallback_metric.return_value.add.assert_called_once_with(
                1, {"profile": PROFILE, "source": "arxiv"}
            )

        lookup_calls = _cache_lookup_calls(fake_lithos)
        # Both lookups happen — primary text-based, then URL-only fallback.
        assert len(lookup_calls) == 2
        # Primary uses the composed dedup query (not equal to source_url).
        assert lookup_calls[0]["source_url"] == ARXIV_URL
        assert lookup_calls[0]["query"] != ARXIV_URL
        # Fallback uses source_url as both query and source_url.
        assert lookup_calls[1] == {"query": ARXIV_URL, "source_url": ARXIV_URL}

    def test_double_miss_proceeds_to_write(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Genuine cache miss (both lookups) → exactly one write attempt."""
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": False, "stale_exists": False})
        )
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": False, "stale_exists": False})
        )

        _run_ingest(
            fake_lithos_url=fake_lithos_url,
            items=(_make_item(),),
        )

        assert len(_cache_lookup_calls(fake_lithos)) == 2
        # Genuine miss — we proceed to write.
        writes = _write_calls(fake_lithos)
        assert len(writes) == 1
        assert writes[0]["source_url"] == ARXIV_URL

    def test_primary_hit_does_not_invoke_fallback(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Primary lookup hits → fallback RPC is skipped (no extra cost)."""
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        with patch("influx.run.metrics.cache_hits_via_url_fallback") as fallback_metric:
            _run_ingest(
                fake_lithos_url=fake_lithos_url,
                items=(_make_item(),),
            )
            # Primary hit means the fallback metric is never bumped.
            fallback_metric.return_value.add.assert_not_called()

        # Exactly one cache_lookup — the primary one — and no fallback.
        assert len(_cache_lookup_calls(fake_lithos)) == 1
        # Primary hit on a SCHEDULED run still falls through to write
        # (multi-profile merge); we just want to confirm no fallback RPC.
        assert len(_write_calls(fake_lithos)) == 1

    def test_url_fallback_hit_skipped_on_backfill(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """On a backfill, a URL-fallback hit causes the item to be skipped."""
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": False, "stale_exists": False})
        )
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        _run_ingest(
            fake_lithos_url=fake_lithos_url,
            items=(_make_item(),),
            skip_cache_hits=True,
        )

        assert len(_cache_lookup_calls(fake_lithos)) == 2
        # Backfill + cache hit (any reason) → skip write entirely (FR-BF-2).
        assert _write_calls(fake_lithos) == []

    def test_url_fallback_hit_merges_on_normal_run(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Normal run with URL-fallback hit still falls through to write.

        Pins the AC: the fix does not regress legitimate multi-profile
        merge behavior.  A cache hit (whether primary or URL-fallback)
        on a non-backfill run means the item is merged into the
        existing note via ``write_note`` — not skipped.
        """
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": False, "stale_exists": False})
        )
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        _run_ingest(
            fake_lithos_url=fake_lithos_url,
            items=(_make_item(),),
            skip_cache_hits=False,
        )

        assert len(_cache_lookup_calls(fake_lithos)) == 2
        # Non-backfill cache hit → merge-profile write proceeds.
        writes = _write_calls(fake_lithos)
        assert len(writes) == 1
        assert writes[0]["source_url"] == ARXIV_URL
