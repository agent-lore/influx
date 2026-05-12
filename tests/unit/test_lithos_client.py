"""Unit tests for lithos_client — construction-time validation.

The in-process stub (PRD 04) has been replaced by the real SSE-backed
``LithosClient`` wrapper (PRD 05).  Connection-lifecycle tests live in
``tests/contract/test_lithos_client.py``.  LCMA wrapper contract tests
live in ``tests/contract/test_lcma_calls.py`` (PRD 08).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from influx.errors import ConfigError, LithosError
from influx.lithos_client import LithosClient


class TestLithosClientConstruction:
    """LithosClient validates transport and URL at construction."""

    def test_rejects_non_sse_transport(self) -> None:
        with pytest.raises(ConfigError, match="only 'sse' is supported"):
            LithosClient(url="http://localhost:1234/sse", transport="stdio")

    def test_rejects_empty_url(self) -> None:
        with pytest.raises(ConfigError, match="LITHOS_URL is required"):
            LithosClient(url="")

    def test_accepts_valid_sse_config(self) -> None:
        client = LithosClient(url="http://localhost:1234/sse")
        assert not client.connected


class TestLCMAStubsRemoved:
    """PRD 08 replaced LCMA stubs with real wrappers on LithosClient."""

    def test_no_not_implemented_stubs_remain(self) -> None:
        """``lithos_client`` no longer exports stub functions."""
        import influx.lithos_client as mod

        for name in (
            "lithos_retrieve",
            "lithos_edge_upsert",
            "lithos_task_create",
            "lithos_task_complete",
        ):
            assert not hasattr(mod, name), (
                f"{name} stub should have been removed by PRD 08"
            )

    def test_lcma_methods_exist_on_client(self) -> None:
        """LithosClient exposes async LCMA methods."""
        client = LithosClient(url="http://localhost:1234/sse")
        for method_name in ("retrieve", "edge_upsert", "task_create", "task_complete"):
            assert hasattr(client, method_name), (
                f"LithosClient.{method_name} should exist"
            )


class TestListNotes:
    """LithosClient.list_notes adapts Influx call shape to current Lithos."""

    async def test_does_not_forward_unsupported_ordering_args(self) -> None:
        client = LithosClient(url="http://localhost:1234/sse")
        client.call_tool = AsyncMock(return_value=object())  # type: ignore[method-assign]

        await client.list_notes(
            tags=["influx:repair-needed", "profile:staging-ai"],
            limit=25,
            order_by="updated_at",
            order="asc",
        )

        client.call_tool.assert_awaited_once_with(
            "lithos_list",
            {"tags": ["influx:repair-needed", "profile:staging-ai"], "limit": 25},
        )


def _tool_result(payload: object, *, is_error: bool = False) -> MagicMock:
    text_content = MagicMock()
    text_content.text = payload if isinstance(payload, str) else json.dumps(payload)
    result = MagicMock()
    result.content = [text_content]
    result.isError = is_error
    return result


class TestDecodedBodies:
    """Centralized decode helpers raise consistent LithosError shapes."""

    async def test_list_notes_body_decodes_json_object(self) -> None:
        client = LithosClient(url="http://localhost:1234/sse")
        client.list_notes = AsyncMock(  # type: ignore[method-assign]
            return_value=_tool_result({"items": [{"id": "note-1"}]})
        )

        body = await client.list_notes_body(tags=["foo"], limit=10)

        assert body == {"items": [{"id": "note-1"}]}

    async def test_list_notes_body_rejects_invalid_json(self) -> None:
        client = LithosClient(url="http://localhost:1234/sse")
        client.list_notes = AsyncMock(  # type: ignore[method-assign]
            return_value=_tool_result("{not-json")
        )

        with pytest.raises(LithosError, match="malformed_tool_response"):
            await client.list_notes_body(tags=["foo"], limit=10)


class TestClassifySquatter:
    """#31 squatter-shape dispatch is a pure function — exhaustively cover."""

    def _make_doc(
        self,
        *,
        tags: list[str] | None = None,
        source_url: str | None = None,
        content: str = "",
        title: str = "Some Title",
    ) -> dict[str, object]:
        doc: dict[str, object] = {
            "id": "doc-x",
            "title": title,
            "content": content,
            "tags": list(tags or []),
        }
        if source_url is not None:
            doc["source_url"] = source_url
        return doc

    def test_arxiv_id_match_classifies_as_duplicate(self) -> None:
        from influx.lithos_client import _classify_squatter

        doc = self._make_doc(
            tags=["arxiv-id:2604.28197", "source:arxiv"],
            content="real body text",
        )
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://arxiv.org/abs/2604.28197",
        )
        assert result.kind == "duplicate"
        assert "arxiv-id:2604.28197" in result.reason

    def test_source_url_match_classifies_as_duplicate(self) -> None:
        from influx.lithos_client import _classify_squatter

        doc = self._make_doc(
            tags=["source:rss"],
            source_url="https://example.com/article-x",
            content="real body",
        )
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://example.com/article-x",
        )
        assert result.kind == "duplicate"
        assert "source_url" in result.reason

    def test_empty_residue_classifies_as_reclaimable(self) -> None:
        from influx.lithos_client import _classify_squatter

        doc = self._make_doc(tags=[], content="")
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://arxiv.org/abs/2604.28197",
        )
        assert result.kind == "reclaimable"
        assert "stale residue" in result.reason

    def test_residue_with_any_tag_is_distinct_not_reclaimable(self) -> None:
        """Conservative: a single tag (e.g. an operator-added one) is
        enough to refuse reclaim, even with no source_url and empty body.
        """
        from influx.lithos_client import _classify_squatter

        doc = self._make_doc(tags=["bookmark"], content="")
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://arxiv.org/abs/2604.28197",
        )
        assert result.kind == "distinct"

    def test_residue_with_body_is_distinct_not_reclaimable(self) -> None:
        from influx.lithos_client import _classify_squatter

        doc = self._make_doc(tags=[], content="user notes here")
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://arxiv.org/abs/2604.28197",
        )
        assert result.kind == "distinct"

    def test_different_arxiv_id_classifies_as_distinct(self) -> None:
        """Same slug, different arxiv id = different paper that happens
        to slugify the same.  Suffix retry territory.
        """
        from influx.lithos_client import _classify_squatter

        doc = self._make_doc(
            tags=["arxiv-id:9999.99999"],
            content="real body",
        )
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://arxiv.org/abs/2604.28197",
        )
        assert result.kind == "distinct"

    def test_metadata_nested_tags_are_recognised(self) -> None:
        """Tags can live under ``metadata.tags`` per lithos_read shape;
        the helper must read both top-level and nested.
        """
        from influx.lithos_client import _classify_squatter

        doc = {
            "id": "doc-x",
            "title": "T",
            "content": "real",
            "metadata": {"tags": ["arxiv-id:2604.28197"]},
        }
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://arxiv.org/abs/2604.28197",
        )
        assert result.kind == "duplicate"

    def test_canonical_url_match_classifies_as_duplicate(self) -> None:
        """Issue #148: squatter and incoming URLs differ only in shape
        (scheme case, trailing slash, tracking params) — canonical
        equality must still classify as duplicate.
        """
        from influx.lithos_client import _classify_squatter

        doc = self._make_doc(
            tags=["source:rss"],
            source_url="HTTPS://Example.com/Article-X/?utm_source=feedreader",
            content="real body",
        )
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://example.com/Article-X",
        )
        assert result.kind == "duplicate"
        assert "canonical match" in result.reason

    def test_arxiv_id_extracted_from_squatter_source_url(self) -> None:
        """Issue #148: squatter has no ``arxiv-id:`` tag but its
        ``source_url`` names the same arxiv id — treat as duplicate.
        """
        from influx.lithos_client import _classify_squatter

        doc = self._make_doc(
            tags=["source:arxiv"],
            source_url="https://arxiv.org/abs/2604.28197",
            content="body without arxiv-id tag",
        )
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://arxiv.org/abs/2604.28197",
        )
        assert result.kind == "duplicate"
        assert "arxiv-id:2604.28197" in result.reason

    def test_malformed_url_falls_back_safely(self) -> None:
        """Malformed URLs must not crash the classifier; fall back to
        the exact-equality check.
        """
        from influx.lithos_client import _classify_squatter

        doc = self._make_doc(
            tags=["source:rss"],
            source_url="not a url at all",
            content="real",
        )
        result = _classify_squatter(
            doc,
            squatter_id="doc-x",
            incoming_source_url="https://example.com/article-x",
        )
        # Not exact, not canonical (different shapes) → distinct.
        assert result.kind == "distinct"


class TestExistingIdParsing:
    """``_existing_id_from_detail`` extracts the squatter id from PR-#30 detail."""

    def test_parses_uuid_form(self) -> None:
        from influx.lithos_client import _existing_id_from_detail

        detail = (
            "existing_id=006bbcb8-ee01-4616-aa43-473f292eba0e; "
            "Slug 'omnirobothome-…' already in use"
        )
        assert (
            _existing_id_from_detail(detail) == "006bbcb8-ee01-4616-aa43-473f292eba0e"
        )

    def test_parses_friendly_test_id(self) -> None:
        from influx.lithos_client import _existing_id_from_detail

        assert (
            _existing_id_from_detail("existing_id=doc-test-1; Slug 'x' in use")
            == "doc-test-1"
        )

    def test_returns_none_for_missing(self) -> None:
        from influx.lithos_client import _existing_id_from_detail

        assert _existing_id_from_detail("") is None
        assert _existing_id_from_detail("no id here") is None

    def test_prefers_retry_id_in_issue32_form(self) -> None:
        """Issue #32: when both ids are present, return the retry id."""
        from influx.lithos_client import _existing_id_from_detail

        detail = (
            "first_existing_id=doc-a; first_slug='Title'; "
            "retry_existing_id=doc-b; retry_slug='Title [arXiv 1.2]'"
        )
        assert _existing_id_from_detail(detail) == "doc-b"


class TestUnresolvedDetailFormat:
    """Issue #32: ``_format_unresolved_detail`` enumerates both squatters."""

    def test_includes_both_ids_and_slugs(self) -> None:
        from influx.lithos_client import _format_unresolved_detail

        out = _format_unresolved_detail(
            first_existing_id="doc-a",
            first_slug="Paper",
            retry_existing_id="doc-b",
            retry_slug="Paper [arXiv 1.2]",
            retry_detail="existing_id=doc-b; Slug 'paper-arxiv-1-2' in use",
        )
        assert "first_existing_id=doc-a" in out
        assert "first_slug='Paper'" in out
        assert "retry_existing_id=doc-b" in out
        assert "retry_slug='Paper [arXiv 1.2]'" in out
        # The retry envelope's human-readable message tail is preserved.
        assert "Slug 'paper-arxiv-1-2' in use" in out
        # And not duplicated as a second existing_id.
        assert out.count("existing_id=doc-b") == 1

    def test_emits_placeholder_when_id_missing(self) -> None:
        from influx.lithos_client import _format_unresolved_detail

        out = _format_unresolved_detail(
            first_existing_id=None,
            first_slug="Paper",
            retry_existing_id="doc-b",
            retry_slug="Paper [host]",
            retry_detail="",
        )
        assert "first_existing_id=<missing>" in out
        assert "retry_existing_id=doc-b" in out
