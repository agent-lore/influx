"""Feedback ingestion — negative-example injection (FR-FB-1..3).

Pulls recent ``influx:rejected:<profile>`` items via ``lithos_list``
and formats their titles into the ``{negative_examples}`` block
consumed by the filter prompt (§6.3).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from influx.config import AppConfig
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
    body: dict[str, Any] = await client.list_notes_body(
        tags=[f"influx:rejected:{profile}"],
        limit=limit,
    )
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


async def build_filter_prompt(
    config: AppConfig,
    client: LithosClient,
    *,
    profile: str,
) -> str:
    """Render the per-profile filter prompt (profile description +
    negative-feedback examples + ``min_score_in_results``).

    Shared by the Run's Stage-2 Feedback (``influx.run._run_feedback_stage``)
    and the inbox tick (``influx.inbox``) so both filter against the same
    prompt shape, including negative-feedback examples.  Falls back to the
    raw template when the ``.format`` placeholders are absent.
    """
    profile_cfg = next((p for p in config.profiles if p.name == profile), None)
    neg_block = await build_negative_examples_block(
        client,
        profile=profile,
        limit=config.feedback.negative_examples_per_profile,
        max_title_chars=config.filter.negative_example_max_title_chars,
    )
    prompt_text = config.prompts.filter.text or ""
    try:
        return prompt_text.format(
            profile_description=(profile_cfg.description if profile_cfg else profile),
            negative_examples=neg_block,
            min_score_in_results=config.filter.min_score_in_results,
        )
    except (KeyError, IndexError):
        return prompt_text
