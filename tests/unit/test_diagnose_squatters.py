"""Unit tests for the ``influx-diagnose squatters`` log-scan helper.

The helper is the only piece of the subcommand that does not require
docker or a live Lithos connection; covering it here means the
deduplication, regex matching, and first/last-seen aggregation are
locked down independent of the I/O paths exercised end-to-end during
operator use.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def _load_script() -> Any:
    """Load ``scripts/influx-diagnose.py`` as a module for direct testing."""
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "influx_diagnose",
        repo_root / "scripts" / "influx-diagnose.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DIAGNOSE = _load_script()
_extract = _DIAGNOSE._extract_squatters_from_logs
_normalise_since = _DIAGNOSE._normalise_since


class TestNormaliseSince:
    """Docker rejects day units in ``--since``; we translate at the boundary."""

    def test_days_translated_to_hours(self) -> None:
        assert _normalise_since("7d") == "168h"
        assert _normalise_since("1d") == "24h"
        assert _normalise_since("30d") == "720h"

    def test_passes_through_native_docker_units(self) -> None:
        assert _normalise_since("24h") == "24h"
        assert _normalise_since("90m") == "90m"
        assert _normalise_since("45s") == "45s"

    def test_passes_through_absolute_timestamp(self) -> None:
        # docker accepts RFC3339 / unix-epoch timestamps too — leave alone.
        assert _normalise_since("2026-04-25T00:00:00") == "2026-04-25T00:00:00"

    def test_handles_none_and_empty(self) -> None:
        assert _normalise_since(None) is None
        assert _normalise_since("") == ""

    def test_tolerates_whitespace_and_case(self) -> None:
        assert _normalise_since(" 7d ") == "168h"
        assert _normalise_since("7D") == "168h"


def _record(
    *,
    timestamp: str,
    detail: str,
    source_url: str = "",
    title: str = "",
    level: str = "WARNING",
    status: str = "slug_collision",
    message: str = "article write skipped",
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "level": level,
        "logger": "influx.scheduler",
        "message": message,
        "status": status,
        "detail": detail,
        "source_url": source_url,
        "title": title,
    }


class TestExtractSquatters:
    def test_returns_empty_dict_when_no_warnings(self) -> None:
        assert _extract([]) == {}

    def test_skips_non_warning_records(self) -> None:
        records = [
            _record(
                timestamp="2026-05-02T06:00:48+00:00",
                detail=(
                    "existing_id=006bbcb8-ee01-4616-aa43-473f292eba0e; "
                    "Slug 'omnirobothome-…-arxiv-260428197' already in use"
                ),
                level="INFO",
            ),
        ]
        assert _extract(records) == {}

    def test_extracts_doc_id_and_slug_from_single_warning(self) -> None:
        records = [
            _record(
                timestamp="2026-05-02T06:00:48+00:00",
                detail=(
                    "existing_id=006bbcb8-ee01-4616-aa43-473f292eba0e; "
                    "Slug 'omnirobothome-test-arxiv-260428197' already in use"
                ),
                source_url="https://arxiv.org/abs/2604.28197",
                title="OmniRobotHome: …",
            ),
        ]
        result = _extract(records)
        assert list(result.keys()) == ["006bbcb8-ee01-4616-aa43-473f292eba0e"]
        entry = result["006bbcb8-ee01-4616-aa43-473f292eba0e"]
        assert entry["slugs"] == ["omnirobothome-test-arxiv-260428197"]
        assert entry["source_urls"] == {"https://arxiv.org/abs/2604.28197"}
        assert entry["titles"] == {"OmniRobotHome: …"}
        assert entry["count"] == 1

    def test_dedups_same_doc_id_across_multiple_runs(self) -> None:
        # Same squatter blocking the same paper across two consecutive runs.
        records = [
            _record(
                timestamp="2026-05-02T00:00:48+00:00",
                detail=(
                    "existing_id=006bbcb8-ee01-4616-aa43-473f292eba0e; "
                    "Slug 'omnirobothome' already in use"
                ),
                source_url="https://arxiv.org/abs/2604.28197",
                title="OmniRobotHome",
            ),
            _record(
                timestamp="2026-05-02T06:00:48+00:00",
                detail=(
                    "existing_id=006bbcb8-ee01-4616-aa43-473f292eba0e; "
                    "Slug 'omnirobothome' already in use"
                ),
                source_url="https://arxiv.org/abs/2604.28197",
                title="OmniRobotHome",
            ),
        ]
        result = _extract(records)
        assert len(result) == 1
        entry = result["006bbcb8-ee01-4616-aa43-473f292eba0e"]
        assert entry["count"] == 2
        assert entry["first_seen"] == "2026-05-02T00:00:48+00:00"
        assert entry["last_seen"] == "2026-05-02T06:00:48+00:00"

    def test_handles_multiple_distinct_squatters(self) -> None:
        records = [
            _record(
                timestamp="2026-05-02T06:00:48+00:00",
                detail=(
                    "existing_id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa; "
                    "Slug 'paper-a' already in use"
                ),
            ),
            _record(
                timestamp="2026-05-02T06:00:49+00:00",
                detail=(
                    "existing_id=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb; "
                    "Slug 'paper-b' already in use"
                ),
            ),
        ]
        result = _extract(records)
        assert set(result.keys()) == {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        }

    def test_handles_double_squatter_detail_after_issue_32(self) -> None:
        """Forward-compat: when #32 lands and a single WARNING enumerates
        both the unsuffixed and suffix-retry squatters, both ids must be
        captured from one record.
        """
        records = [
            _record(
                timestamp="2026-05-02T06:00:48+00:00",
                detail=(
                    "first_existing_id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa; "
                    "Slug 'omnirobothome' already in use; "
                    "retry_existing_id=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb; "
                    "Slug 'omnirobothome-arxiv-260428197' already in use"
                ),
            ),
        ]
        result = _extract(records)
        assert len(result) == 2
        a = result["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
        b = result["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]
        assert a["slugs"] == ["omnirobothome"]
        assert b["slugs"] == ["omnirobothome-arxiv-260428197"]

    def test_falls_back_to_message_when_detail_absent(self) -> None:
        # Records without a ``detail`` extra still surface squatters
        # if the message itself contains the diagnostic.  Robustness
        # against future log-shape tweaks.
        rec = _record(
            timestamp="2026-05-02T06:00:48+00:00",
            detail="",
            message=(
                "slug_collision: existing_id=cccccccc-cccc-cccc-cccc-cccccccccccc; "
                "Slug 'paper-c' already in use"
            ),
        )
        rec.pop("detail")
        result = _extract([rec])
        assert "cccccccc-cccc-cccc-cccc-cccccccccccc" in result
