"""Tests for the lithos_client stub (PRD 04 seam — replaced by PRD 05)."""

from __future__ import annotations

from influx import lithos_client


class TestWrite:
    """lithos_client.write records calls for test assertions."""

    def setup_method(self) -> None:
        lithos_client.clear_write_log()

    def test_write_records_call(self) -> None:
        lithos_client.write(
            source_url="https://arxiv.org/abs/2601.00001",
            note_content="# Test Note\n",
            metadata={"tags": ["source:arxiv"]},
        )
        log = lithos_client.get_write_log()
        assert len(log) == 1
        assert log[0].source_url == "https://arxiv.org/abs/2601.00001"
        assert log[0].note_content == "# Test Note\n"
        assert log[0].metadata == {"tags": ["source:arxiv"]}

    def test_write_records_multiple_calls(self) -> None:
        lithos_client.write(
            source_url="https://arxiv.org/abs/2601.00001",
            note_content="note-1",
        )
        lithos_client.write(
            source_url="https://arxiv.org/abs/2601.00002",
            note_content="note-2",
        )
        log = lithos_client.get_write_log()
        assert len(log) == 2
        assert log[0].source_url == "https://arxiv.org/abs/2601.00001"
        assert log[1].source_url == "https://arxiv.org/abs/2601.00002"

    def test_write_metadata_defaults_to_empty_dict(self) -> None:
        lithos_client.write(
            source_url="https://example.com",
            note_content="x",
        )
        assert lithos_client.get_write_log()[0].metadata == {}

    def test_clear_write_log(self) -> None:
        lithos_client.write(
            source_url="https://example.com",
            note_content="x",
        )
        lithos_client.clear_write_log()
        assert lithos_client.get_write_log() == []


class TestCacheLookup:
    """lithos_client.cache_lookup always returns 'not cached'."""

    def test_returns_not_cached(self) -> None:
        result = lithos_client.cache_lookup(
            source_url="https://arxiv.org/abs/2601.00001",
        )
        assert result.cached is False

    def test_returns_not_cached_for_any_url(self) -> None:
        for url in [
            "https://arxiv.org/abs/2601.99999",
            "https://example.com/article/1",
            "https://blog.example.org/post",
        ]:
            result = lithos_client.cache_lookup(source_url=url)
            assert result.cached is False
