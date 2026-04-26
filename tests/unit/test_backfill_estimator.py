"""Unit tests for the backfill estimator and confirm-required gate (US-008).

Covers:
- Estimator math: days × len(categories) × max_results_per_category (Q-3)
- AC-09-E: estimate of 35 items accepted without confirm
- AC-09-I: estimate of 1500 items rejected with confirm_required;
  resubmission with confirm=true accepted
- Multiple categories
- Both date-range forms (--days N and --from/--to)
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from influx.config import (
    AppConfig,
    ArxivSourceConfig,
    LithosConfig,
    ProfileConfig,
    ProfileSources,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.coordinator import Coordinator
from influx.http_api import estimate_backfill_items, router
from influx.probes import ProbeLoop
from influx.scheduler import InfluxScheduler

# ── Helpers ────────────────────────────────────────────────────────


def _make_config(
    *,
    categories: list[str] | None = None,
    max_results: int = 200,
    profiles: list[str] | None = None,
) -> AppConfig:
    """Build a minimal config with controllable arXiv source settings."""
    cats = categories if categories is not None else ["cs.AI"]
    profile_names = profiles if profiles is not None else ["test-profile"]
    profile_list = [
        ProfileConfig(
            name=name,
            sources=ProfileSources(
                arxiv=ArxivSourceConfig(
                    categories=cats,
                    max_results_per_category=max_results,
                ),
            ),
        )
        for name in profile_names
    ]
    return AppConfig(
        lithos=LithosConfig(url=""),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=profile_list,
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="test"),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
    )


def _make_app(config: AppConfig) -> FastAPI:
    """Create a FastAPI app with the /backfills route wired up."""
    app = FastAPI()
    app.include_router(router)
    coordinator = Coordinator()
    scheduler = InfluxScheduler(config, coordinator)
    probe_loop = ProbeLoop(config, interval=30.0)
    probe_loop.run_once()
    app.state.config = config
    app.state.coordinator = coordinator
    app.state.scheduler = scheduler
    app.state.probe_loop = probe_loop
    app.state.active_tasks = set()  # type: ignore[assignment]
    return app


# ── Estimator function tests ──────────────────────────────────────


class TestEstimatorMath:
    """Verify the naive formula: days × categories × max_results."""

    def test_single_category_7_days(self) -> None:
        """7 days × 1 category × 5 max_results = 35."""
        config = _make_config(categories=["cs.AI"], max_results=5)
        result = estimate_backfill_items(
            "test-profile", config, days=7,
        )
        assert result == 35

    def test_multiple_categories(self) -> None:
        """10 days × 3 categories × 50 max_results = 1500."""
        config = _make_config(
            categories=["cs.AI", "cs.RO", "cs.CL"],
            max_results=50,
        )
        result = estimate_backfill_items(
            "test-profile", config, days=10,
        )
        assert result == 1500

    def test_date_range_form(self) -> None:
        """--from/--to computes days from the date range."""
        config = _make_config(categories=["cs.AI"], max_results=100)
        result = estimate_backfill_items(
            "test-profile",
            config,
            date_from="2026-04-20",
            date_to="2026-04-27",
        )
        # 7 days × 1 category × 100 = 700
        assert result == 700

    def test_unknown_profile_returns_zero(self) -> None:
        """Unknown profile name yields 0 (no categories to count)."""
        config = _make_config(categories=["cs.AI"], max_results=200)
        result = estimate_backfill_items(
            "nonexistent", config, days=365,
        )
        assert result == 0

    def test_zero_days(self) -> None:
        """Zero days yields zero items."""
        config = _make_config(categories=["cs.AI"], max_results=200)
        result = estimate_backfill_items(
            "test-profile", config, days=0,
        )
        assert result == 0

    def test_same_day_range_returns_zero(self) -> None:
        """date_from == date_to → 0 days → 0 items."""
        config = _make_config(categories=["cs.AI"], max_results=100)
        result = estimate_backfill_items(
            "test-profile",
            config,
            date_from="2026-04-20",
            date_to="2026-04-20",
        )
        assert result == 0

    def test_six_categories_default_max(self) -> None:
        """Realistic: 7 × 6 × 200 = 8400."""
        config = _make_config(
            categories=["cs.AI", "cs.RO", "cs.MA", "cs.NE", "cs.CL", "cs.LO"],
            max_results=200,
        )
        result = estimate_backfill_items(
            "test-profile", config, days=7,
        )
        assert result == 8400


# ── Confirm-required gate (HTTP layer) ────────────────────────────


class TestConfirmRequiredGate:
    """HTTP-level confirm-required behaviour (AC-09-E, AC-09-I)."""

    def test_35_items_accepted_without_confirm(self) -> None:
        """AC-09-E: estimate of 35 items → 202 accepted, no confirm needed."""
        config = _make_config(categories=["cs.AI"], max_results=5)
        app = _make_app(config)
        with TestClient(app) as tc:
            resp = tc.post(
                "/backfills",
                json={"profile": "test-profile", "days": 7},
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["kind"] == "backfill"

    def test_1500_items_rejected_with_confirm_required(self) -> None:
        """AC-09-I: estimate of 1500 → 400 + reason=confirm_required."""
        config = _make_config(
            categories=["cs.AI", "cs.RO", "cs.CL"],
            max_results=50,
        )
        app = _make_app(config)
        with TestClient(app) as tc:
            resp = tc.post(
                "/backfills",
                json={"profile": "test-profile", "days": 10},
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["reason"] == "confirm_required"
        assert body["estimated_items"] == 1500

    def test_resubmission_with_confirm_true_accepted(self) -> None:
        """AC-09-I: resubmit with confirm=true → 202 accepted."""
        config = _make_config(
            categories=["cs.AI", "cs.RO", "cs.CL"],
            max_results=50,
        )
        app = _make_app(config)
        with TestClient(app) as tc:
            resp = tc.post(
                "/backfills",
                json={
                    "profile": "test-profile",
                    "days": 10,
                    "confirm": True,
                },
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["kind"] == "backfill"

    def test_date_range_form_confirm_required(self) -> None:
        """Confirm gate works with --from/--to range form (FR-BF-1)."""
        # 365 days × 1 cat × 200 max = 73000 > 1000
        config = _make_config(categories=["cs.AI"], max_results=200)
        app = _make_app(config)
        with TestClient(app) as tc:
            resp = tc.post(
                "/backfills",
                json={
                    "profile": "test-profile",
                    "from": "2025-04-01",
                    "to": "2026-04-01",
                },
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["reason"] == "confirm_required"
        assert body["estimated_items"] > 1000

    def test_date_range_form_accepted_without_confirm(self) -> None:
        """Short date range → accepted without confirm (FR-BF-1)."""
        # 7 days × 1 cat × 5 max = 35 ≤ 1000
        config = _make_config(categories=["cs.AI"], max_results=5)
        app = _make_app(config)
        with TestClient(app) as tc:
            resp = tc.post(
                "/backfills",
                json={
                    "profile": "test-profile",
                    "from": "2026-04-20",
                    "to": "2026-04-27",
                },
            )
        assert resp.status_code == 202

    def test_estimated_items_in_response(self) -> None:
        """confirm_required response includes estimated_items field."""
        # 30 × 6 × 200 = 36000
        config = _make_config(
            categories=["cs.AI", "cs.RO", "cs.MA", "cs.NE", "cs.CL", "cs.LO"],
            max_results=200,
        )
        app = _make_app(config)
        with TestClient(app) as tc:
            resp = tc.post(
                "/backfills",
                json={"profile": "test-profile", "days": 30},
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["estimated_items"] == 36000
