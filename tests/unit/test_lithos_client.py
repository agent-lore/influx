"""Unit tests for lithos_client — construction-time validation.

The in-process stub (PRD 04) has been replaced by the real SSE-backed
``LithosClient`` wrapper (PRD 05).  Connection-lifecycle tests live in
``tests/contract/test_lithos_client.py``.  LCMA wrapper contract tests
live in ``tests/contract/test_lcma_calls.py`` (PRD 08).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from influx.errors import ConfigError
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
