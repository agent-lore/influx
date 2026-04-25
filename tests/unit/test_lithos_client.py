"""Unit tests for lithos_client — construction-time validation.

The in-process stub (PRD 04) has been replaced by the real SSE-backed
``LithosClient`` wrapper (PRD 05).  Connection-lifecycle tests live in
``tests/contract/test_lithos_client.py``.
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
