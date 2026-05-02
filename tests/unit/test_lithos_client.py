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


class TestExtractSlugSuffixAttempt:
    """``_extract_slug_suffix`` produces distinct slugs across attempts (#31)."""

    def test_attempt_1_is_bare_suffix(self) -> None:
        from influx.lithos_client import _extract_slug_suffix

        assert (
            _extract_slug_suffix("https://arxiv.org/abs/2604.28197", attempt=1)
            == " [arXiv 2604.28197]"
        )

    def test_attempt_2_appends_numeric_marker(self) -> None:
        from influx.lithos_client import _extract_slug_suffix

        assert (
            _extract_slug_suffix("https://arxiv.org/abs/2604.28197", attempt=2)
            == " [arXiv 2604.28197 (2)]"
        )

    def test_attempt_higher_appends_n(self) -> None:
        from influx.lithos_client import _extract_slug_suffix

        assert (
            _extract_slug_suffix("https://arxiv.org/abs/2604.28197", attempt=5)
            == " [arXiv 2604.28197 (5)]"
        )

    def test_non_arxiv_uses_host(self) -> None:
        from influx.lithos_client import _extract_slug_suffix

        assert (
            _extract_slug_suffix("https://example.com/article", attempt=1)
            == " [example.com]"
        )
        assert (
            _extract_slug_suffix("https://example.com/article", attempt=2)
            == " [example.com (2)]"
        )

    def test_default_attempt_is_one(self) -> None:
        from influx.lithos_client import _extract_slug_suffix

        # Backwards compat: callers that don't pass ``attempt`` get the
        # original AC-05-D behaviour.
        assert (
            _extract_slug_suffix("https://arxiv.org/abs/2604.28197")
            == " [arXiv 2604.28197]"
        )
