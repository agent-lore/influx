"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

# Realistic multi-section v0.7 TOML fixture usable by later PRDs.
# Covers every schema section defined in PRD 01 §5 and docs/REQUIREMENTS.md §4.2.
# Provider api_key_env values use test-only env vars set by the fixture.
_REALISTIC_V07_TOML = dedent("""\
    [influx]
    note_schema_version = 1

    [schedule]
    cron = "0 6 * * *"
    timezone = "UTC"
    misfire_grace_seconds = 3600

    [storage]
    archive_dir = "/archive"
    retain_days = 3650
    max_download_bytes = 52_428_800
    download_timeout_seconds = 30

    [notifications]
    webhook_url = ""
    timeout_seconds = 5

    [security]
    allow_private_ips = false

    [[profiles]]
    name = "ai-robotics"
    description = "Multi-agent systems, humanoid robotics, LLM reasoning."

    [profiles.thresholds]
    relevance = 7
    full_text = 8
    deep_extract = 9
    notify_immediate = 8
    lcma_edge_score = 0.75

    [profiles.sources.arxiv]
    enabled = true
    categories = ["cs.AI", "cs.RO"]
    max_results_per_category = 100
    lookback_days = 1

    [[profiles.sources.rss]]
    name = "Test Blog"
    url = "https://example.com/feed.xml"
    source_tag = "blog"

    [[profiles]]
    name = "web-tech"
    description = "Browser internals, JS engines, web standards."

    [profiles.sources.arxiv]
    enabled = false

    [[profiles.sources.rss]]
    name = "Mozilla Hacks"
    url = "https://hacks.mozilla.org/feed/"
    source_tag = "rss"

    [providers.test-provider]
    base_url = "https://api.test.example.com/v1"
    api_key_env = "TEST_PROVIDER_API_KEY"

    [models.filter]
    provider = "test-provider"
    model = "test-model"
    temperature = 0.0
    max_tokens = 2048
    request_timeout = 30
    max_retries = 2
    json_mode = true

    [models.enrich]
    provider = "test-provider"
    model = "test-model"
    temperature = 0.2
    request_timeout = 30
    max_retries = 2
    json_mode = true

    [models.extract]
    provider = "test-provider"
    model = "test-model"
    temperature = 0.2
    request_timeout = 60
    max_retries = 2
    json_mode = true

    [prompts.filter]
    text = "Filter: {profile_description} {negative_examples} {min_score_in_results}"

    [prompts.tier1_enrich]
    text = "Enrich: {title} {abstract} {profile_summary}"

    [prompts.tier3_extract]
    text = "Extract: {title} {full_text}"

    [filter]
    batch_size = 25
    min_score_in_results = 6
    negative_example_max_title_chars = 200

    [extraction]
    min_html_chars = 1000
    min_web_chars = 500
    strip_tags = ["script", "iframe", "object", "embed"]

    [resilience]
    max_retries = 3
    backoff_base_seconds = 1
    arxiv_request_min_interval_seconds = 3
    arxiv_429_backoff_seconds = 10
    lithos_write_conflict_max_retries = 1

    [feedback]
    negative_examples_per_profile = 20
    recalibrate_after_runs = 7

    [repair]
    max_items_per_run = 100

    [telemetry]
    enabled = false
    console_fallback = false
    service_name = "influx"
    export_interval_ms = 30000
""")


@pytest.fixture
def influx_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a realistic multi-section v0.7 influx.toml and point INFLUX_CONFIG at it.

    Sets test-only provider API-key env vars so that ``load_config()``
    succeeds.  Clears real provider keys to prevent leakage from the
    developer's shell.
    """
    config_path = tmp_path / "influx.toml"
    config_path.write_text(_REALISTIC_V07_TOML)
    monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
    # Provide the test-only API key required by the fixture's provider.
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "test-key-value")
    return config_path
