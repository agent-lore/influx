"""Unit tests for ``[inbox]`` config (Inbox v1 slice 1).

Covers the ``InboxConfig`` schema: default-disabled, the fixed task-tag
constant, cron validation, and the positive ``max_items_per_tick`` guard.
"""

from __future__ import annotations

import pytest

from influx.config import AppConfig, InboxConfig, PromptEntryConfig, PromptsConfig
from influx.errors import ConfigError


def _prompts() -> PromptsConfig:
    return PromptsConfig(
        filter=PromptEntryConfig(text="x"),
        tier1_enrich=PromptEntryConfig(text="x"),
        tier3_extract=PromptEntryConfig(text="x"),
    )


def test_inbox_defaults_disabled() -> None:
    """The inbox is opt-in: an AppConfig without ``[inbox]`` is disabled."""
    config = AppConfig(prompts=_prompts())
    assert config.inbox.enabled is False
    assert config.inbox.poll_cron == "*/5 * * * *"
    assert config.inbox.max_items_per_tick == 20
    assert config.inbox.agent_id == "influx-inbox"


def test_task_tag_is_fixed_constant() -> None:
    """The task tag is a protocol constant, not an operator-tunable field."""
    assert InboxConfig.TASK_TAG == "influx:inbox"
    # It is a ClassVar, so it is not part of the model's fields.
    assert "TASK_TAG" not in InboxConfig.model_fields


def test_poll_cron_validates_as_cron() -> None:
    """A malformed cron expression is rejected at config-build time."""
    with pytest.raises(ConfigError, match="poll_cron is not a valid cron"):
        InboxConfig(poll_cron="not a cron")


def test_poll_cron_accepts_valid_cron() -> None:
    InboxConfig(poll_cron="*/10 * * * *")  # no raise


def test_max_items_per_tick_must_be_positive() -> None:
    with pytest.raises(ConfigError, match="max_items_per_tick must be >= 1"):
        InboxConfig(max_items_per_tick=0)
