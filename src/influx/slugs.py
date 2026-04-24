"""Feed-name slugification helpers (FR-ST-2).

A valid FR-ST-2 slug matches ``^[a-z0-9]+(-[a-z0-9]+)*$`` and is at most
40 characters long.
"""

from __future__ import annotations

import re
import unicodedata

_FRST2_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_FRST2_MAX_LEN = 40


def slugify_feed_name(name: str) -> str:
    """Turn a free-form feed name into an FR-ST-2 slug.

    The function lowercases, strips accents, replaces non-alphanumeric runs
    with a single hyphen, and trims leading/trailing hyphens.  The result is
    truncated to 40 characters (trimming a trailing hyphen if truncation
    introduced one).

    Returns an empty string when the input yields no alphanumeric characters
    after normalisation — callers must treat that as an error.
    """
    text = unicodedata.normalize("NFKD", name)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    text = text[:_FRST2_MAX_LEN].rstrip("-")
    return text


def is_valid_slug(value: str) -> bool:
    """Return *True* if *value* is a valid FR-ST-2 slug."""
    return bool(_FRST2_RE.fullmatch(value)) and len(value) <= _FRST2_MAX_LEN
