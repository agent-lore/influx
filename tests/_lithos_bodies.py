"""Builders for ``lithos_cache_lookup`` response bodies in the real envelope.

Lithos returns ``{"hit", "document": {"id", "title", "source_url", "tags",
...}, "stale_exists", "stale_id"}`` (``lithos/cognitive_memory.py``
``cache_lookup``).  Test fixtures used to fake ``{"hit": True}`` or put
``id`` / ``source_url`` at the top level, which hid two bugs (Lithos task
e4300784).  Build fixtures through these helpers so they match the wire.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


def cache_hit_body(
    source_url: str,
    *,
    note_id: str = "note-1",
    title: str = "Existing note",
    tags: Sequence[str] = (),
) -> dict[str, Any]:
    """A Lithos cache hit whose document is stored under *source_url*."""
    return {
        "hit": True,
        "document": {
            "id": note_id,
            "title": title,
            "content": "",
            "confidence": 1.0,
            "updated_at": "2026-10-07T00:00:00+00:00",
            "expires_at": None,
            "tags": list(tags),
            "source_url": source_url,
        },
        "stale_exists": False,
        "stale_id": None,
    }


def cache_miss_body() -> dict[str, Any]:
    """A clean Lithos cache miss."""
    return {"hit": False, "document": None, "stale_exists": False, "stale_id": None}


def cache_hit_json(source_url: str, **kwargs: Any) -> str:
    """:func:`cache_hit_body` serialised for the fake MCP server queue."""
    return json.dumps(cache_hit_body(source_url, **kwargs))
