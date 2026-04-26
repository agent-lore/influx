"""Tests for archive URL-hash disambiguation (PRD 09 AC-M3-4 sub-tests 2 & 3).

Verifies that ``url_hash`` produces a deterministic, collision-free
10-char hex SHA-256 digest of the normalised ``source_url``, suitable
for use as the ``{url-hash}`` segment in RSS archive filenames.
"""

from __future__ import annotations

from influx.urls import url_hash


class TestUrlHashDeterminism:
    """AC-M3-4 sub-test 3 / AC-09-C: same URL always yields same hash."""

    def test_same_url_twice_yields_same_hash(self) -> None:
        url = "https://feed-x.example/post-a"
        assert url_hash(url) == url_hash(url)

    def test_hash_is_10_hex_chars(self) -> None:
        result = url_hash("https://feed-x.example/post-a")
        assert len(result) == 10
        assert all(c in "0123456789abcdef" for c in result)

    def test_equivalent_urls_yield_same_hash(self) -> None:
        """URLs that normalise to the same canonical form produce the same hash."""
        raw = "https://Feed-X.Example:443/post-a?utm_source=twitter"
        canonical = "https://feed-x.example/post-a"
        assert url_hash(raw) == url_hash(canonical)

    def test_trailing_slash_normalised_before_hashing(self) -> None:
        assert url_hash("https://feed-x.example/post-a/") == url_hash(
            "https://feed-x.example/post-a"
        )


class TestUrlHashCollision:
    """AC-M3-4 sub-test 2 / AC-09-B: distinct URLs yield distinct hashes."""

    def test_different_path_segments_yield_different_hashes(self) -> None:
        """Two URLs differing only by path produce distinct hash suffixes."""
        hash_a = url_hash("https://feed-x.example/post-a")
        hash_b = url_hash("https://feed-x.example/post-b")
        assert hash_a != hash_b

    def test_same_feed_same_date_different_articles(self) -> None:
        """AC-09-B scenario: two items from feed-X published on 2026-04-23."""
        hash_a = url_hash("https://feed-x.example/post-a")
        hash_b = url_hash("https://feed-x.example/post-b")
        assert hash_a != hash_b
        # Both are valid 10-char hex
        for h in (hash_a, hash_b):
            assert len(h) == 10
            assert all(c in "0123456789abcdef" for c in h)

    def test_different_hosts_yield_different_hashes(self) -> None:
        hash_a = url_hash("https://blog-a.example/post")
        hash_b = url_hash("https://blog-b.example/post")
        assert hash_a != hash_b

    def test_different_query_params_yield_different_hashes(self) -> None:
        """Non-tracking query params affect the hash."""
        hash_a = url_hash("https://feed-x.example/post?id=1")
        hash_b = url_hash("https://feed-x.example/post?id=2")
        assert hash_a != hash_b
