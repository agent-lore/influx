"""Prompt loading and template-variable validation.

Each canonical prompt key (``filter``, ``tier1_enrich``, ``tier3_extract``)
has a fixed set of required template variables.  This module loads prompt
text from inline strings or file paths and validates the variable set.
"""

from __future__ import annotations

import re
from pathlib import Path

from influx.errors import ConfigError, PromptValidationError

# ── Required variable sets per canonical key ─────────────────────────

REQUIRED_VARIABLES: dict[str, frozenset[str]] = {
    "filter": frozenset(
        {"profile_description", "negative_examples", "min_score_in_results"}
    ),
    "tier1_enrich": frozenset({"title", "abstract", "profile_summary"}),
    "tier3_extract": frozenset({"title", "full_text"}),
}

# Matches ``{name}`` but not ``{{escaped}}``
_TEMPLATE_VAR_RE = re.compile(r"(?<!\{)\{(\w+)\}(?!\})")


def extract_variables(template: str) -> set[str]:
    """Return the set of template variable names found in *template*."""
    return set(_TEMPLATE_VAR_RE.findall(template))


def load_prompt(
    *,
    text: str | None = None,
    path: str | None = None,
    config_dir: Path | None = None,
) -> str:
    """Load prompt content from *text* or *path*.

    Exactly one of *text* or *path* must be provided.  When *path* is
    relative it is resolved against *config_dir* (which defaults to the
    current working directory when unset).

    Raises :class:`~influx.errors.ConfigError` when both or neither
    source is given, or when the file cannot be read.
    """
    if text is not None and path is not None:
        raise ConfigError(
            "Prompt specifies both 'text' and 'path'; exactly one is required"
        )
    if text is None and path is None:
        raise ConfigError(
            "Prompt specifies neither 'text' nor 'path'; exactly one is required"
        )

    if text is not None:
        return text

    assert path is not None  # narrowing for type checker
    resolved = Path(path)
    if not resolved.is_absolute():
        base = config_dir if config_dir is not None else Path.cwd()
        resolved = base / resolved
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read prompt file {resolved}: {exc}") from exc


def validate_prompt_variables(prompt_key: str, template: str) -> None:
    """Validate that *template* uses exactly the required variables for *prompt_key*.

    Raises :class:`~influx.errors.PromptValidationError` when:

    * the template contains a variable not in the required set (AC-01-A), or
    * a required variable is absent from the template (AC-01-B).
    """
    required = REQUIRED_VARIABLES.get(prompt_key)
    if required is None:
        raise ConfigError(f"Unknown prompt key {prompt_key!r}")

    found = extract_variables(template)
    unknown = found - required
    missing = required - found

    if unknown:
        names = ", ".join(sorted(unknown))
        raise PromptValidationError(
            f"Prompt '{prompt_key}' contains unknown variable(s): {names}"
        )
    if missing:
        names = ", ".join(sorted(missing))
        raise PromptValidationError(
            f"Prompt '{prompt_key}' is missing required variable(s): {names}"
        )
