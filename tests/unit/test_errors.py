"""Tests for the InfluxError hierarchy in ``influx.errors``."""

from __future__ import annotations

import pytest

from influx.errors import (
    ExtractionError,
    InfluxError,
    LCMAError,
    LithosError,
    NetworkError,
)


class TestNetworkError:
    """NetworkError is an InfluxError and carries url/kind/reason context."""

    def test_isinstance_chain(self) -> None:
        err = NetworkError("boom", url="http://example.com", kind="ssrf")
        assert isinstance(err, InfluxError)
        assert isinstance(err, NetworkError)

    def test_structured_context(self) -> None:
        err = NetworkError(
            "SSRF blocked",
            url="http://169.254.169.254/metadata",
            kind="ssrf",
            reason="link-local address",
        )
        assert err.url == "http://169.254.169.254/metadata"
        assert err.kind == "ssrf"
        assert err.reason == "link-local address"
        assert str(err) == "SSRF blocked"

    def test_reason_defaults_to_empty(self) -> None:
        err = NetworkError("timeout", url="http://slow.example.com", kind="timeout")
        assert err.reason == ""


class TestLithosError:
    """LithosError is an InfluxError and carries operation/status_code/detail."""

    def test_isinstance_chain(self) -> None:
        err = LithosError("api failure")
        assert isinstance(err, InfluxError)
        assert isinstance(err, LithosError)

    def test_structured_context(self) -> None:
        err = LithosError(
            "write conflict",
            operation="create_note",
            status_code=409,
            detail="version mismatch",
        )
        assert err.operation == "create_note"
        assert err.status_code == 409
        assert err.detail == "version mismatch"

    def test_defaults(self) -> None:
        err = LithosError("generic")
        assert err.operation == ""
        assert err.status_code is None
        assert err.detail == ""


class TestLCMAError:
    """LCMAError is an InfluxError and carries model/stage/detail."""

    def test_isinstance_chain(self) -> None:
        err = LCMAError("llm failed")
        assert isinstance(err, InfluxError)
        assert isinstance(err, LCMAError)

    def test_structured_context(self) -> None:
        err = LCMAError(
            "rate limited",
            model="filter",
            stage="scoring",
            detail="429 Too Many Requests",
        )
        assert err.model == "filter"
        assert err.stage == "scoring"
        assert err.detail == "429 Too Many Requests"

    def test_defaults(self) -> None:
        err = LCMAError("generic")
        assert err.model == ""
        assert err.stage == ""
        assert err.detail == ""


class TestExtractionError:
    """ExtractionError is an InfluxError and carries url/stage/detail."""

    def test_isinstance_chain(self) -> None:
        err = ExtractionError("parse failed")
        assert isinstance(err, InfluxError)
        assert isinstance(err, ExtractionError)

    def test_structured_context(self) -> None:
        err = ExtractionError(
            "empty body",
            url="http://example.com/article",
            stage="html_parse",
            detail="no content after strip",
        )
        assert err.url == "http://example.com/article"
        assert err.stage == "html_parse"
        assert err.detail == "no content after strip"

    def test_defaults(self) -> None:
        err = ExtractionError("generic")
        assert err.url == ""
        assert err.stage == ""
        assert err.detail == ""


class TestAllSubclassesCatchable:
    """All new subclasses are catchable via InfluxError."""

    @pytest.mark.parametrize(
        "exc",
        [
            NetworkError("n", url="http://x", kind="k"),
            LithosError("l"),
            LCMAError("m"),
            ExtractionError("e"),
        ],
        ids=["NetworkError", "LithosError", "LCMAError", "ExtractionError"],
    )
    def test_catch_as_influx_error(self, exc: InfluxError) -> None:
        with pytest.raises(InfluxError):
            raise exc
