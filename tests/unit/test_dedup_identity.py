"""Identity checks on ``lithos_cache_lookup`` hits (Lithos e4300784).

Influx always looks notes up by ``source_url`` and only trusts a hit Lithos
found in its URL index (``match: "source_url"``, lithos-core d0392561).
``same_source_reason`` is the slug-collision identity check.
"""

from __future__ import annotations

from influx.dedup import cache_hit_document, same_source_reason, verify_cache_hit
from tests._lithos_bodies import cache_hit_body, cache_miss_body

ARXIV_URL = "https://arxiv.org/abs/2610.00710"


class TestSameSourceReason:
    def test_exact_url(self) -> None:
        url = "https://example.com/post"
        reason = same_source_reason(
            doc_tags=[], doc_source_url=url, incoming_source_url=url
        )
        assert reason is not None
        assert "source_url matches incoming" in reason

    def test_canonical_url(self) -> None:
        reason = same_source_reason(
            doc_tags=[],
            doc_source_url="https://example.com/post",
            incoming_source_url="HTTPS://Example.com/post/?utm_source=feed",
        )
        assert reason is not None
        assert "canonical match" in reason

    def test_arxiv_id_tag(self) -> None:
        reason = same_source_reason(
            doc_tags=["arxiv-id:2610.00710"],
            doc_source_url=None,
            incoming_source_url=ARXIV_URL,
        )
        assert reason == "carries arxiv-id:2610.00710"

    def test_arxiv_id_from_doc_url(self) -> None:
        reason = same_source_reason(
            doc_tags=[],
            doc_source_url="http://arxiv.org/abs/2610.00710#section",
            incoming_source_url=ARXIV_URL,
        )
        assert reason == "source_url names arxiv-id:2610.00710"

    def test_different_source_is_none(self) -> None:
        assert (
            same_source_reason(
                doc_tags=["arxiv-id:2601.00001"],
                doc_source_url="https://scazlab.yale.edu/sites/default/files/x.pdf",
                incoming_source_url=ARXIV_URL,
            )
            is None
        )

    def test_doc_without_source_url_is_none(self) -> None:
        assert (
            same_source_reason(
                doc_tags=[], doc_source_url=None, incoming_source_url=ARXIV_URL
            )
            is None
        )


class TestVerifyCacheHit:
    def test_source_url_match_returned_unchanged(self) -> None:
        body = cache_hit_body(ARXIV_URL, note_id="n-1")
        assert verify_cache_hit(body) == body

    def test_hit_without_match_is_a_miss(self) -> None:
        """The e4300784 incident: a Lithos older than d0392561 sends no
        ``match`` and its threshold-0.0 semantic fallback reported the
        nearest unrelated note as a hit."""
        body = cache_hit_body(
            "https://scazlab.yale.edu/to-help-or-not",
            note_id="8c0a21ae",
            title="To Help or Not to Help?",
            match=None,
        )
        verified = verify_cache_hit(body)
        assert verified["hit"] is False
        assert verified["document"] is None
        assert verified["ignored_neighbour"] == {
            "id": "8c0a21ae",
            "source_url": "https://scazlab.yale.edu/to-help-or-not",
            "title": "To Help or Not to Help?",
            "match": None,
        }

    def test_semantic_match_is_a_miss_even_for_the_same_paper(self) -> None:
        """Lithos answers a source_url lookup semantically only when asked
        (``semantic_fallback``), which Influx never does.  A similar note is
        not proof the URL is stored."""
        body = cache_hit_body(
            "https://www.arxiv.org/abs/2610.00710",
            tags=["arxiv-id:2610.00710"],
            match="semantic",
        )
        verified = verify_cache_hit(body)
        assert verified["hit"] is False
        assert verified["ignored_neighbour"]["match"] == "semantic"

    def test_input_body_not_mutated(self) -> None:
        body = cache_hit_body("https://other.example/x", match=None)
        verify_cache_hit(body)
        assert body["hit"] is True
        assert body["document"] is not None

    def test_hit_without_document_is_a_miss(self) -> None:
        verified = verify_cache_hit({"hit": True})
        assert verified["hit"] is False
        assert verified["ignored_neighbour"] == {
            "id": None,
            "source_url": None,
            "title": None,
            "match": None,
        }

    def test_miss_passes_through(self) -> None:
        body = cache_miss_body()
        assert verify_cache_hit(body) == body


class TestCacheHitDocument:
    def test_returns_document_on_hit(self) -> None:
        body = cache_hit_body(ARXIV_URL, note_id="n-1", tags=["profile:x"])
        doc = cache_hit_document(body)
        assert doc is not None
        assert doc["id"] == "n-1"
        assert doc["tags"] == ["profile:x"]

    def test_none_on_miss(self) -> None:
        assert cache_hit_document(cache_miss_body()) is None

    def test_ignores_top_level_fields(self) -> None:
        """Lithos never sends ``id`` at the top level; don't read it there."""
        assert cache_hit_document({"hit": True, "id": "n-1"}) is None
