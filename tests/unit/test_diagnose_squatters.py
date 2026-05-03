"""Unit tests for the ``influx-diagnose squatters`` log-scan helper.

The helper is the only piece of the subcommand that does not require
docker or a live Lithos connection; covering it here means the
deduplication, regex matching, and first/last-seen aggregation are
locked down independent of the I/O paths exercised end-to-end during
operator use.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch


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


class TestDirectInvocationImports:
    """``./scripts/influx-diagnose.py --apply`` must work without uv run.

    The squatter delete path imports ``influx.lithos_client``, which
    transitively pulls in ``mcp`` from the project venv.  When the
    script is invoked as ``./scripts/influx-diagnose.py …`` (system
    Python, no venv on sys.path), neither symbol is importable.

    ``_ensure_project_runtime_or_reexec`` detects the gap and
    ``os.execvp``-replaces this process under ``uv run`` so the
    operator's argv reaches the venv-backed interpreter unchanged.
    These tests pin the helper's contract so a future refactor cannot
    silently regress to the original ``ModuleNotFoundError`` shape that
    bit the 2026-05-02 incident.
    """

    def test_helper_exists_and_attempts_import_then_reexec(self) -> None:
        import inspect

        assert hasattr(_DIAGNOSE, "_ensure_project_runtime_or_reexec")
        source = inspect.getsource(_DIAGNOSE._ensure_project_runtime_or_reexec)
        # Must try the import first ...
        assert "import influx.lithos_client" in source
        # ... and re-exec via uv run when it fails.
        assert "os.execvp" in source
        assert '"uv"' in source and '"run"' in source

    def test_helper_is_called_from_apply_path_in_cmd_squatters(self) -> None:
        import inspect

        source = inspect.getsource(_DIAGNOSE.cmd_squatters)
        # The call must live AFTER the read-only return so dry-run
        # invocations never trigger a ``uv run`` re-exec.
        assert "_ensure_project_runtime_or_reexec" in source
        idx_dry_return = source.index("return 0")
        idx_reexec = source.index("_ensure_project_runtime_or_reexec")
        assert idx_reexec > idx_dry_return, (
            "_ensure_project_runtime_or_reexec must be called only on "
            "the --apply path, not for read-only scans"
        )

    def test_helper_avoids_infinite_reexec_loop(self) -> None:
        import inspect

        source = inspect.getsource(_DIAGNOSE._ensure_project_runtime_or_reexec)
        assert "INFLUX_DIAGNOSE_REEXECED" in source, (
            "the helper must guard against an infinite re-exec loop "
            "if uv run somehow yields an environment without the deps"
        )

    def test_lithos_client_importable_under_uv_run(self) -> None:
        # Sanity check that the import chain still works under the test
        # runner (``uv run pytest``).  Catches a future ``src/`` rename
        # or ``LithosClient`` symbol move that would defeat the re-exec.
        from influx.lithos_client import LithosClient  # noqa: F401


class TestRewriteUrlForHost:
    """``host.docker.internal`` is unresolvable from the host."""

    def test_rewrites_host_docker_internal_when_not_in_docker(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(_DIAGNOSE, "_running_inside_docker", lambda: False)
        assert (
            _DIAGNOSE._rewrite_url_for_host("http://host.docker.internal:8766/sse")
            == "http://127.0.0.1:8766/sse"
        )

    def test_no_rewrite_when_inside_docker(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_DIAGNOSE, "_running_inside_docker", lambda: True)
        assert (
            _DIAGNOSE._rewrite_url_for_host("http://host.docker.internal:8766/sse")
            == "http://host.docker.internal:8766/sse"
        )

    def test_no_rewrite_for_unrelated_urls(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(_DIAGNOSE, "_running_inside_docker", lambda: False)
        assert (
            _DIAGNOSE._rewrite_url_for_host("http://lithos.internal:8765/sse")
            == "http://lithos.internal:8765/sse"
        )
        assert (
            _DIAGNOSE._rewrite_url_for_host("http://127.0.0.1:8766/sse")
            == "http://127.0.0.1:8766/sse"
        )


class TestFormatExceptionChain:
    """Anyio TaskGroup exceptions hide the actual cause; we unwrap them."""

    def test_plain_exception(self) -> None:
        try:
            raise ValueError("nope")
        except ValueError as exc:
            rendered = _DIAGNOSE._format_exception_chain(exc)
        assert rendered == "ValueError: nope"

    def test_unwraps_exception_group(self) -> None:
        # Mimic the anyio TaskGroup wrap shape:
        #   ExceptionGroup(..., [ConnectionRefusedError(...), ...])
        cause = ConnectionRefusedError("connect failed: 111")
        group = BaseExceptionGroup(
            "unhandled errors in a TaskGroup",
            [cause],
        )
        rendered = _DIAGNOSE._format_exception_chain(group)
        assert "ConnectionRefusedError" in rendered
        assert "connect failed: 111" in rendered

    def test_unwraps_chained_cause(self) -> None:
        try:
            try:
                raise OSError("dns lookup failed: host.docker.internal")
            except OSError as inner:
                raise RuntimeError("transport setup failed") from inner
        except RuntimeError as exc:
            rendered = _DIAGNOSE._format_exception_chain(exc)
        assert "RuntimeError: transport setup failed" in rendered
        assert "OSError: dns lookup failed: host.docker.internal" in rendered

    def test_summarise_doc_for_refusal_renders_useful_signals(self) -> None:
        doc = {
            "title": "OmniRobotHome: A Multi-Camera Platform",
            "source_url": "https://arxiv.org/abs/2604.28197",
            "author": "operator@example.com",
            "tags": ["profile:staging-robotics", "source:arxiv", "ingested-by:loom"],
        }
        rendered = _DIAGNOSE._summarise_doc_for_refusal(doc)
        assert "OmniRobotHome" in rendered
        assert "operator@example.com" in rendered
        assert "ingested_by=loom" in rendered
        assert "https://arxiv.org/abs/2604.28197" in rendered

    def test_summarise_doc_for_refusal_handles_missing_fields(self) -> None:
        rendered = _DIAGNOSE._summarise_doc_for_refusal({})
        assert "(no title)" in rendered
        assert "(no ingested-by tag)" in rendered


class TestIsDocNotFound:
    """Match Lithos's ``doc_not_found`` message variants."""

    def test_matches_lithos_message(self) -> None:
        assert _DIAGNOSE._is_doc_not_found(
            "Document not found: 006bbcb8-ee01-4616-aa43-473f292eba0e"
        )

    def test_matches_envelope_code(self) -> None:
        assert _DIAGNOSE._is_doc_not_found("error code=doc_not_found")

    def test_negative_cases(self) -> None:
        assert not _DIAGNOSE._is_doc_not_found("")
        assert not _DIAGNOSE._is_doc_not_found("connection refused")
        assert not _DIAGNOSE._is_doc_not_found("authentication failed")


class TestDeleteOutcomes:
    """The three outcome constants are stable and distinct."""

    def test_outcome_constants_distinct(self) -> None:
        outcomes = {
            _DIAGNOSE.DELETE_OK,
            _DIAGNOSE.DELETE_ALREADY_GONE,
            _DIAGNOSE.DELETE_REFUSED,
        }
        # Three distinct string values; no accidental aliasing.
        assert len(outcomes) == 3
        # Operator-facing labels — guard the wire format.
        assert _DIAGNOSE.DELETE_OK == "deleted"
        assert _DIAGNOSE.DELETE_ALREADY_GONE == "already_gone"
        assert _DIAGNOSE.DELETE_REFUSED == "refused"

    def test_caps_long_chains(self) -> None:
        # Ten nested causes — output must stay readable.
        excs: list[BaseException] = [ValueError(f"layer-{i}") for i in range(10)]
        for i in range(1, len(excs)):
            excs[i].__cause__ = excs[i - 1]
        rendered = _DIAGNOSE._format_exception_chain(excs[-1])
        # Capped at 6 distinct entries by ``_format_exception_chain``.
        assert rendered.count("|") <= 5


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


class TestSlugCollisionBacklog:
    """``slug-collision-backlog`` reads the JSONL the daemon writes on chain exhaust."""

    def test_returns_empty_list_when_file_missing(self, tmp_path: Any) -> None:
        assert _DIAGNOSE._read_slug_collision_backlog(tmp_path) == []

    def test_reads_one_entry(self, tmp_path: Any) -> None:
        path = tmp_path / "unresolved-slug-collisions.jsonl"
        path.write_text(
            '{"timestamp": "2026-05-02T13:00:00+00:00", '
            '"run_id": "r-1", "profile": "staging-robotics", "source": "arxiv", '
            '"source_url": "https://arxiv.org/abs/2604.28197", '
            '"title": "OmniRobotHome", '
            '"detail": "existing_id=doc-A"}\n',
            encoding="utf-8",
        )
        entries = _DIAGNOSE._read_slug_collision_backlog(tmp_path)
        assert len(entries) == 1
        assert entries[0]["title"] == "OmniRobotHome"
        assert entries[0]["profile"] == "staging-robotics"

    def test_skips_malformed_lines(self, tmp_path: Any) -> None:
        path = tmp_path / "unresolved-slug-collisions.jsonl"
        path.write_text(
            '{"title": "ok"}\nthis is not json\n\n{"title": "also ok"}\n',
            encoding="utf-8",
        )
        entries = _DIAGNOSE._read_slug_collision_backlog(tmp_path)
        assert [e["title"] for e in entries] == ["ok", "also ok"]


# ── ``--id`` flag (issue #34) ───────────────────────────────────────


def _squatters_args(
    *,
    ids: list[str] | None = None,
    apply: bool = False,
    yes: list[str] | None = None,
    yes_to_all: bool = False,
    no_require_influx_authored: bool = False,
) -> argparse.Namespace:
    """Build a Namespace shaped like ``squatters`` argparse output.

    Covers every attribute ``cmd_squatters`` reads so a test can
    drive the subcommand without pulling in the real argparse setup.
    """
    return argparse.Namespace(
        env="staging",
        since="7d",
        tail=50000,
        apply=apply,
        yes=yes,
        yes_to_all=yes_to_all,
        agent="influx-diagnose",
        lithos_url="http://test.invalid:8765/sse",
        no_require_influx_authored=no_require_influx_authored,
        id=ids,
    )


def _docker_call_forbidden(*args: Any, **kwargs: Any) -> Any:
    """Helper that fails the test if docker is reached when --id is set."""
    raise AssertionError(
        "docker logs should not be invoked when --id is provided; "
        "--id must bypass the log scan entirely (issue #34 acceptance)"
    )


class TestSquattersFromIdList:
    """``--id`` builds a candidates dict bypassing the log-scan."""

    def test_returns_one_entry_per_id(self) -> None:
        result = _DIAGNOSE._squatters_from_id_list(
            ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bb"]
        )
        assert set(result.keys()) == {
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bb",
        }

    def test_dedupes_repeated_ids(self) -> None:
        # ``--id X --id X`` should not produce two entries
        result = _DIAGNOSE._squatters_from_id_list(["dup", "dup"])
        assert list(result.keys()) == ["dup"]

    def test_entry_has_doc_id_and_empty_metadata(self) -> None:
        result = _DIAGNOSE._squatters_from_id_list(["x"])
        entry = result["x"]
        assert entry["doc_id"] == "x"
        # The log-scan path populates these from log records; the --id
        # path has no log context, so they should be empty (not absent).
        assert entry.get("slugs") == []
        assert entry.get("source_urls") == set()
        assert entry.get("titles") == set()


class TestCmdSquattersIdFlagDryRun:
    """``--id`` without ``--apply`` previews via ``LithosClient.read_note``.

    Acceptance criterion: read-only ``--id`` performs ``lithos_read`` and
    reports title/tags/source_url plus the planned deletion command.
    No docker log scan is performed.
    """

    def test_dry_run_calls_read_note_and_skips_docker(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        # Hard-fail if docker is touched at all.
        monkeypatch.setattr(_DIAGNOSE, "_docker_logs_iter", _docker_call_forbidden)
        monkeypatch.setattr(_DIAGNOSE, "_load_env", lambda _name: {})
        monkeypatch.setattr(_DIAGNOSE, "_container_name", lambda _env: "influx-test")
        monkeypatch.setattr(
            _DIAGNOSE, "_read_lithos_url", lambda _args, _env: "http://t/sse"
        )

        # Build a mock client whose ``read_note`` returns a doc shape.
        mock_client = AsyncMock()
        mock_client.read_note.return_value = {
            "id": "doc-X",
            "title": "Squatting Paper",
            "tags": ["ingested-by:influx", "source:arxiv"],
            "source_url": "https://example.test/abs/1",
        }
        mock_client.close = AsyncMock()

        # Patch the LithosClient symbol used by the dry-run preview.
        with patch.object(_DIAGNOSE, "_make_lithos_client", return_value=mock_client):
            args = _squatters_args(ids=["doc-X"], apply=False)
            rc = _DIAGNOSE.cmd_squatters(args)

        assert rc == 0
        mock_client.read_note.assert_awaited_once_with(note_id="doc-X")
        out = capsys.readouterr().out
        # Operator-facing preview must surface the four signals from the
        # acceptance criteria (title, tags, source_url) plus the planned
        # deletion command.
        assert "doc-X" in out
        assert "Squatting Paper" in out
        assert "ingested-by:influx" in out
        assert "https://example.test/abs/1" in out
        assert "--apply" in out and "--id" in out

    def test_dry_run_with_multiple_ids_calls_read_note_each(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(_DIAGNOSE, "_docker_logs_iter", _docker_call_forbidden)
        monkeypatch.setattr(_DIAGNOSE, "_load_env", lambda _name: {})
        monkeypatch.setattr(_DIAGNOSE, "_container_name", lambda _env: "influx-test")
        monkeypatch.setattr(
            _DIAGNOSE, "_read_lithos_url", lambda _args, _env: "http://t/sse"
        )

        mock_client = AsyncMock()
        mock_client.read_note.return_value = {
            "title": "T",
            "tags": ["ingested-by:influx"],
        }
        mock_client.close = AsyncMock()

        with patch.object(_DIAGNOSE, "_make_lithos_client", return_value=mock_client):
            args = _squatters_args(ids=["a", "b", "c"], apply=False)
            rc = _DIAGNOSE.cmd_squatters(args)

        assert rc == 0
        # One read_note per --id.
        assert mock_client.read_note.await_count == 3
        called_ids = {
            call.kwargs.get("note_id") for call in mock_client.read_note.await_args_list
        }
        assert called_ids == {"a", "b", "c"}

    def test_dry_run_handles_missing_doc_gracefully(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        # ``read_note`` may raise if the doc is already gone.  The
        # preview path must still surface a useful line per id rather
        # than abort the whole batch.
        monkeypatch.setattr(_DIAGNOSE, "_docker_logs_iter", _docker_call_forbidden)
        monkeypatch.setattr(_DIAGNOSE, "_load_env", lambda _name: {})
        monkeypatch.setattr(_DIAGNOSE, "_container_name", lambda _env: "influx-test")
        monkeypatch.setattr(
            _DIAGNOSE, "_read_lithos_url", lambda _args, _env: "http://t/sse"
        )

        mock_client = AsyncMock()
        mock_client.read_note.side_effect = RuntimeError("Document not found: doc-X")
        mock_client.close = AsyncMock()

        with patch.object(_DIAGNOSE, "_make_lithos_client", return_value=mock_client):
            args = _squatters_args(ids=["doc-X"], apply=False)
            rc = _DIAGNOSE.cmd_squatters(args)

        assert rc == 0
        out = capsys.readouterr().out
        assert "doc-X" in out


class TestCmdSquattersIdFlagApply:
    """``--apply --id <X>`` deletes via the existing safety-check pipeline.

    Acceptance criterion: ``--apply --id <X>`` is treated as both
    "list this id" and "confirm deletion" — no extra ``--yes <X>``
    needed.  The pre-delete safety check (``ingested-by:influx`` tag)
    is preserved.
    """

    def test_apply_with_id_deletes_when_influx_authored(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        monkeypatch.setattr(_DIAGNOSE, "_docker_logs_iter", _docker_call_forbidden)
        monkeypatch.setattr(_DIAGNOSE, "_load_env", lambda _name: {})
        monkeypatch.setattr(_DIAGNOSE, "_container_name", lambda _env: "influx-test")
        monkeypatch.setattr(
            _DIAGNOSE, "_read_lithos_url", lambda _args, _env: "http://t/sse"
        )
        monkeypatch.setattr(
            _DIAGNOSE, "_ensure_project_runtime_or_reexec", lambda: None
        )

        # Stub out the delete to return DELETE_OK without touching the
        # network.  The function itself is exercised in TestDeleteOutcomes
        # via ``_format_exception_chain``; here we only need to confirm
        # the dispatcher feeds it the right id.
        delete_calls: list[dict[str, Any]] = []

        async def fake_delete(**kwargs: Any) -> tuple[str, str]:
            delete_calls.append(kwargs)
            return _DIAGNOSE.DELETE_OK, "deleted"

        monkeypatch.setattr(_DIAGNOSE, "_delete_squatter", fake_delete)

        args = _squatters_args(ids=["doc-X"], apply=True)
        rc = _DIAGNOSE.cmd_squatters(args)

        assert rc == 0
        assert len(delete_calls) == 1
        assert delete_calls[0]["doc_id"] == "doc-X"
        # Safety check stays on by default — caller must explicitly
        # opt out via --no-require-influx-authored.
        assert delete_calls[0]["require_influx_authored"] is True
        out = capsys.readouterr().out
        assert "DELETED" in out

    def test_apply_with_id_does_not_require_yes(self, monkeypatch: Any) -> None:
        # Per issue #34 rationale, --apply --id <X> already names the doc
        # to delete; no additional --yes <X> may be required.
        monkeypatch.setattr(_DIAGNOSE, "_docker_logs_iter", _docker_call_forbidden)
        monkeypatch.setattr(_DIAGNOSE, "_load_env", lambda _name: {})
        monkeypatch.setattr(_DIAGNOSE, "_container_name", lambda _env: "influx-test")
        monkeypatch.setattr(
            _DIAGNOSE, "_read_lithos_url", lambda _args, _env: "http://t/sse"
        )
        monkeypatch.setattr(
            _DIAGNOSE, "_ensure_project_runtime_or_reexec", lambda: None
        )

        async def fake_delete(**kwargs: Any) -> tuple[str, str]:
            return _DIAGNOSE.DELETE_OK, "deleted"

        monkeypatch.setattr(_DIAGNOSE, "_delete_squatter", fake_delete)

        # Note: ``yes`` is None here — must NOT trigger the
        # "requires --yes" sys.exit when --id is provided.
        args = _squatters_args(ids=["doc-X"], apply=True, yes=None)
        rc = _DIAGNOSE.cmd_squatters(args)
        assert rc == 0

    def test_apply_with_id_refused_when_safety_check_fails(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        monkeypatch.setattr(_DIAGNOSE, "_docker_logs_iter", _docker_call_forbidden)
        monkeypatch.setattr(_DIAGNOSE, "_load_env", lambda _name: {})
        monkeypatch.setattr(_DIAGNOSE, "_container_name", lambda _env: "influx-test")
        monkeypatch.setattr(
            _DIAGNOSE, "_read_lithos_url", lambda _args, _env: "http://t/sse"
        )
        monkeypatch.setattr(
            _DIAGNOSE, "_ensure_project_runtime_or_reexec", lambda: None
        )

        async def fake_delete(**kwargs: Any) -> tuple[str, str]:
            # Mirror the real path's refusal shape when the doc lacks
            # the ``ingested-by:influx`` tag.
            return _DIAGNOSE.DELETE_REFUSED, (
                "doc is not influx-authored (missing 'ingested-by:influx' tag)"
            )

        monkeypatch.setattr(_DIAGNOSE, "_delete_squatter", fake_delete)

        args = _squatters_args(ids=["doc-X"], apply=True)
        rc = _DIAGNOSE.cmd_squatters(args)
        # Refusal must surface as a non-zero exit so a wrapping shell
        # script can detect it.
        assert rc == 1
        out = capsys.readouterr().out
        assert "REFUSED" in out
        assert "ingested-by:influx" in out


class TestCmdSquattersLogScanUnchanged:
    """The pre-existing log-scan path must keep working when --id is absent."""

    def test_log_scan_path_still_invokes_docker(self, monkeypatch: Any) -> None:
        # Sentinel: docker iterator IS called when --id is absent.
        called: dict[str, bool] = {"docker": False}

        def fake_docker_iter(
            container: str, *, since: str, tail: int
        ) -> Iterable[dict[str, Any]]:
            called["docker"] = True
            return iter([])

        monkeypatch.setattr(_DIAGNOSE, "_docker_logs_iter", fake_docker_iter)
        monkeypatch.setattr(_DIAGNOSE, "_load_env", lambda _name: {})
        monkeypatch.setattr(_DIAGNOSE, "_container_name", lambda _env: "influx-test")

        args = _squatters_args(ids=None, apply=False)
        rc = _DIAGNOSE.cmd_squatters(args)
        assert rc == 0
        assert called["docker"] is True


class TestSquattersIdArgparse:
    """``--id`` must be repeatable and accepted alongside the existing flags."""

    def test_id_is_repeatable(self) -> None:
        parser = _DIAGNOSE._build_parser()
        args = parser.parse_args(
            [
                "squatters",
                "--id",
                "aaaa",
                "--id",
                "bbbb",
            ]
        )
        assert args.id == ["aaaa", "bbbb"]

    def test_id_default_is_none(self) -> None:
        # Default must be falsy so the dispatcher can use it as the
        # branch signal.
        parser = _DIAGNOSE._build_parser()
        args = parser.parse_args(["squatters"])
        assert not args.id


# ``Iterable`` is imported here (not at module top) so the original
# tests above keep their original import surface untouched.
from collections.abc import Iterable  # noqa: E402
