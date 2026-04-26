"""Feedback ingestion — negative-example injection (FR-FB-1..3).

Pulls recent ``influx:rejected:<profile>`` items via ``lithos_list``
and formats their titles into the ``{negative_examples}`` block
consumed by the filter prompt (§6.3).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from influx.lithos_client import LithosClient

logger = logging.getLogger(__name__)


async def fetch_rejection_titles(
    client: LithosClient,
    *,
    profile: str,
    limit: int,
) -> list[str]:
    """Fetch up to *limit* rejection titles for *profile* (FR-FB-1).

    Calls ``lithos_list(tags=[f"influx:rejected:{profile}"], limit=limit)``
    and extracts the title from each returned item.  Items that already
    carry a ``title`` field are used directly; items missing a title
    trigger a ``lithos_read(id=...)`` fallback to fetch the title
    (FR-FB-2).

    Returns a list of title strings (possibly empty).
    """
    result = await client.list_notes(
        tags=[f"influx:rejected:{profile}"],
        limit=limit,
    )
    text = result.content[0].text  # type: ignore[union-attr]
    body: dict[str, Any] = json.loads(text)
    items: list[dict[str, Any]] = body.get("items", [])

    titles: list[str] = []
    for item in items:
        title = item.get("title")
        if title:
            titles.append(title)
        elif item.get("id"):
            note = await client.read_note(note_id=item["id"])
            fallback_title = note.get("title", "")
            if fallback_title:
                titles.append(fallback_title)
            else:
                logger.warning(
                    "Skipping rejection item %s: no title available",
                    item["id"],
                )
        else:
            logger.warning("Skipping rejection item with no id or title")
    return titles


def format_negative_examples(
    titles: list[str],
    *,
    max_title_chars: int = 200,
) -> str:
    """Render *titles* into the §6.3 ``negative_examples`` block.

    Each title is formatted as::

        - "{title}" (rejected)

    Titles longer than *max_title_chars* are truncated.  An empty
    *titles* list returns an empty string.
    """
    lines: list[str] = []
    for title in titles:
        truncated = title[:max_title_chars] if len(title) > max_title_chars else title
        lines.append(f'- "{truncated}" (rejected)')
    return "\n".join(lines)


async def build_negative_examples_block(
    client: LithosClient,
    *,
    profile: str,
    limit: int,
    max_title_chars: int = 200,
) -> str:
    """Fetch + format the ``{negative_examples}`` block for *profile*.

    This is the documented seam the filter-prompt builder consumes.
    Combines :func:`fetch_rejection_titles` and
    :func:`format_negative_examples` into a single async call.
    """
    titles = await fetch_rejection_titles(
        client,
        profile=profile,
        limit=limit,
    )
    return format_negative_examples(titles, max_title_chars=max_title_chars)
