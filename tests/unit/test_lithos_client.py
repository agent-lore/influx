"""Unit tests for lithos_client — construction-time validation.

The in-process stub (PRD 04) has been replaced by the real SSE-backed
``LithosClient`` wrapper (PRD 05).  Connection-lifecycle tests live in
``tests/contract/test_lithos_client.py``.  LCMA wrapper contract tests
live in ``tests/contract/test_lcma_calls.py`` (PRD 08).
"""

from __future__ import annotations

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
