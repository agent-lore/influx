"""Configuration and environment loading.

Influx is configured by a TOML file (``influx.toml``).  This module
defines pydantic v2 schema models for the full v0.7 config structure
and a ``load_config()`` entry point that reads TOML, validates via
pydantic, and returns a typed ``AppConfig``.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from influx.errors import ConfigError
from influx.prompts import load_prompt, validate_prompt_variables
from influx.slugs import slugify_feed_name

__all__ = [
    # v0.7 pydantic schema models
    "AppConfig",
    "ArxivSourceConfig",
    "ExtractionConfig",
    "FeedbackConfig",
    "FilterTuningConfig",
    "InfluxSectionConfig",
    "LithosConfig",
    "ModelSlotConfig",
    "NotificationsConfig",
    "NotificationEventMode",
    "NotificationRunKind",
    "NotificationWebhookConfig",
    "NotificationWebhookType",
    "ProfileConfig",
    "ProfileSources",
    "ProfileThresholds",
    "PromptEntryConfig",
    "PromptsConfig",
    "ProviderConfig",
    "RepairConfig",
    "ResilienceConfig",
    "RssSourceEntry",
    "ScheduleConfig",
    "SecurityConfig",
    "StorageConfig",
    "TelemetryConfig",
    # Config loading API
    "find_config_path",
    "load_config",
]


# ══════════════════════════════════════════════════════════════════════
# v0.7 pydantic v2 schema models (US-004)
# ══════════════════════════════════════════════════════════════════════


class InfluxSectionConfig(BaseModel):
    """``[influx]`` top-level settings."""

    note_schema_version: int = 1


class ScheduleConfig(BaseModel):
    """``[schedule]`` cron schedule for ingestion runs."""

    cron: str = "0 6 * * *"
    timezone: str = "UTC"
    misfire_grace_seconds: int = 3600
    shutdown_grace_seconds: int = 30


class StorageConfig(BaseModel):
    """``[storage]`` archive storage settings."""

    archive_dir: str = "/archive"
    state_dir: str = "/state"
    retain_days: int = 3650
    max_download_bytes: int = 52_428_800
    download_timeout_seconds: int = 30


class NotificationsConfig(BaseModel):
    """``[notifications]`` webhook notification settings."""

    webhook_url: str = ""
    timeout_seconds: int = 5
    webhooks: list[NotificationWebhookConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_webhook_names(self) -> NotificationsConfig:
        names = [webhook.name for webhook in self.webhooks]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ConfigError(
                "Duplicate notification webhook name(s): "
                f"{', '.join(sorted(duplicates))}"
            )
        return self


class LithosConfig(BaseModel):
    """``[lithos]`` Lithos MCP connection settings (§19)."""

    url: str = ""
    transport: str = "sse"


class SecurityConfig(BaseModel):
    """``[security]`` SSRF guard and security settings."""

    allow_private_ips: bool = False
    allow_remote_admin: bool = False


NotificationWebhookType = Literal[
    "generic_digest",
    "agent_zero_message_async",
    "agent_zero_notification_create",
    "openclaw_agent",
]
NotificationEventMode = Literal["digest", "article"]
NotificationRunKind = Literal["scheduled", "manual", "backfill"]


_WEBHOOK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class NotificationWebhookConfig(BaseModel):
    """One configured outbound notification sink."""

    name: str
    type: NotificationWebhookType
    url: str
    enabled: bool = True
    notify_on: list[NotificationRunKind] = Field(
        default_factory=lambda: ["manual", "scheduled"]
    )
    event_mode: NotificationEventMode = "digest"
    min_score: int | None = None
    auth_token_env: str = ""
    context: str = ""
    deliver: bool = False
    channel: str = ""
    sender_name: str = "Influx"

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _WEBHOOK_NAME_RE.match(v):
            raise ConfigError(
                f"Notification webhook name {v!r} is invalid; "
                r"must match ^[a-z0-9][a-z0-9-]{0,63}$"
            )
        return v

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ConfigError(
                "Notification webhook URL must use http or https scheme, "
                f"got {parsed.scheme!r}"
            )
        return v

    @field_validator("notify_on")
    @classmethod
    def _notify_on_must_not_be_empty(
        cls, v: list[NotificationRunKind]
    ) -> list[NotificationRunKind]:
        if not v:
            raise ConfigError("Notification webhook notify_on must not be empty")
        return v

    @model_validator(mode="after")
    def _validate_type_specific_fields(self) -> NotificationWebhookConfig:
        if self.type == "generic_digest" and self.event_mode != "digest":
            raise ConfigError(
                f"Notification webhook {self.name!r}: generic_digest only supports "
                "event_mode='digest'"
            )
        if (
            self.type == "agent_zero_notification_create"
            and self.event_mode != "article"
        ):
            raise ConfigError(
                f"Notification webhook {self.name!r}: "
                "agent_zero_notification_create only supports event_mode='article'"
            )
        if self.type == "agent_zero_message_async" and self.context == "":
            raise ConfigError(
                f"Notification webhook {self.name!r}: context is required for "
                "agent_zero_message_async"
            )
        if self.type == "openclaw_agent" and not self.sender_name:
            raise ConfigError(
                f"Notification webhook {self.name!r}: sender_name must not be empty"
            )
        return self


# ── Profiles ─────────────────────────────────────────────────────────


class ProfileThresholds(BaseModel):
    """``[profiles.thresholds]`` score thresholds per profile."""

    relevance: int = 7
    full_text: int = 8
    deep_extract: int = 9
    notify_immediate: int = 8
    lcma_edge_score: float = 0.75


class ArxivSourceConfig(BaseModel):
    """``[profiles.sources.arxiv]`` arXiv source settings."""

    enabled: bool = True
    categories: list[str] = Field(
        default_factory=lambda: [
            "cs.AI",
            "cs.RO",
            "cs.MA",
            "cs.NE",
            "cs.CL",
            "cs.LO",
        ]
    )
    max_results_per_category: int = 200
    lookback_days: int = 1


class RssSourceEntry(BaseModel):
    """``[[profiles.sources.rss]]`` one RSS feed entry.

    Unknown fields are rejected via a ``mode='before'`` validator so
    that the error surfaces as ``ConfigError`` rather than a generic
    pydantic ``ValidationError``.
    """

    name: str
    url: str
    source_tag: Literal["rss", "blog"]

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            known = {"name", "url", "source_tag"}
            unknown = set(data.keys()) - known
            if unknown:
                raise ConfigError(
                    "Unknown field(s) on RSS source entry: "
                    f"{', '.join(sorted(unknown))}"
                )
        return data

    @field_validator("name")
    @classmethod
    def _name_must_slugify(cls, v: str) -> str:
        if not slugify_feed_name(v):
            raise ConfigError(
                f"RSS feed name {v!r} produces an empty slug "
                "after FR-ST-2 slugification"
            )
        return v

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ConfigError(
                f"RSS feed URL must use http or https scheme, got {parsed.scheme!r}"
            )
        return v


class ProfileSources(BaseModel):
    """``[profiles.sources]`` source configuration per profile."""

    arxiv: ArxivSourceConfig = Field(default_factory=ArxivSourceConfig)
    rss: list[RssSourceEntry] = Field(default_factory=list)


_PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class ProfileConfig(BaseModel):
    """``[[profiles]]`` one interest profile."""

    name: str
    description: str = ""
    thresholds: ProfileThresholds = Field(default_factory=ProfileThresholds)
    sources: ProfileSources = Field(default_factory=ProfileSources)

    @field_validator("name")
    @classmethod
    def _validate_profile_name(cls, v: str) -> str:
        if not _PROFILE_NAME_RE.match(v):
            raise ConfigError(
                f"Profile name {v!r} is invalid; "
                r"must match ^[a-z][a-z0-9-]{0,31}$"
            )
        return v


# ── Providers & Models ───────────────────────────────────────────────


class ProviderConfig(BaseModel):
    """``[providers.*]`` LLM provider connection info."""

    base_url: str
    api_key_env: str = ""
    extra_headers: dict[str, str] = Field(default_factory=dict)


class ModelSlotConfig(BaseModel):
    """``[models.*]`` LLM model slot configuration (FR-CFG-6)."""

    provider: str
    model: str
    temperature: float = 0.0
    max_tokens: int | None = None
    request_timeout: int = 30
    max_retries: int = 2
    json_mode: bool = False


# ── Prompts ──────────────────────────────────────────────────────────


class PromptEntryConfig(BaseModel):
    """One prompt entry — inline ``text`` or file ``path``."""

    text: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def _check_exactly_one_source(self) -> PromptEntryConfig:
        if self.text is not None and self.path is not None:
            raise ConfigError(
                "Prompt specifies both 'text' and 'path'; exactly one is required"
            )
        if self.text is None and self.path is None:
            raise ConfigError(
                "Prompt specifies neither 'text' nor 'path'; exactly one is required"
            )
        return self


class PromptsConfig(BaseModel):
    """``[prompts]`` all three canonical prompt keys required (FR-CFG-7).

    Unknown top-level keys are rejected via a ``mode='before'``
    validator so that the error surfaces as ``ConfigError``.
    """

    filter: PromptEntryConfig
    tier1_enrich: PromptEntryConfig
    tier3_extract: PromptEntryConfig

    @model_validator(mode="before")
    @classmethod
    def _validate_prompt_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            known = {"filter", "tier1_enrich", "tier3_extract"}
            unknown = set(data.keys()) - known
            if unknown:
                raise ConfigError(
                    f"Unknown key(s) under [prompts]: {', '.join(sorted(unknown))}"
                )
            missing = known - set(data.keys())
            if missing:
                raise ConfigError(
                    f"Missing required prompt key(s): {', '.join(sorted(missing))}"
                )
        return data


# ── Tuning sections ──────────────────────────────────────────────────


class FilterTuningConfig(BaseModel):
    """``[filter]`` filter batch and scoring parameters."""

    batch_size: int = 25
    min_score_in_results: int = 6
    negative_example_max_title_chars: int = 200


class ExtractionConfig(BaseModel):
    """``[extraction]`` text extraction quality gates."""

    min_html_chars: int = 1000
    min_web_chars: int = 500
    strip_tags: list[str] = Field(
        default_factory=lambda: [
            "script",
            "iframe",
            "object",
            "embed",
        ]
    )


class ResilienceConfig(BaseModel):
    """``[resilience]`` retry and backoff settings."""

    max_retries: int = 3
    backoff_base_seconds: int = 1
    arxiv_request_min_interval_seconds: int = 3
    arxiv_429_backoff_seconds: int = 10
    lithos_write_conflict_max_retries: int = 1


class FeedbackConfig(BaseModel):
    """``[feedback]`` negative-example injection settings."""

    negative_examples_per_profile: int = 20
    recalibrate_after_runs: int = 7


class RepairConfig(BaseModel):
    """``[repair]`` repair/upgrade batch limits.

    Distinct from ``[feedback]``; controls repair pipeline limits.
    """

    max_items_per_run: int = 100


class TelemetryConfig(BaseModel):
    """``[telemetry]`` OTEL observability settings."""

    enabled: bool = False
    console_fallback: bool = False
    service_name: str = "influx"
    environment: str | None = None
    export_interval_ms: int = 30000


# ── Root model ───────────────────────────────────────────────────────


class AppConfig(BaseModel):
    """Root configuration model for the full v0.7 TOML schema."""

    influx: InfluxSectionConfig = Field(default_factory=InfluxSectionConfig)
    lithos: LithosConfig = Field(default_factory=LithosConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    profiles: list[ProfileConfig] = Field(default_factory=list)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, ModelSlotConfig] = Field(default_factory=dict)
    prompts: PromptsConfig
    filter: FilterTuningConfig = Field(default_factory=FilterTuningConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    repair: RepairConfig = Field(default_factory=RepairConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)


# ══════════════════════════════════════════════════════════════════════
# Config loading (US-005)
# ══════════════════════════════════════════════════════════════════════


def _default_config_candidates() -> list[Path]:
    """Return filesystem candidates checked when INFLUX_CONFIG is unset."""
    return [
        Path.cwd() / "influx.toml",
        Path.home() / ".influx" / "influx.toml",
        Path("/etc/influx/influx.toml"),
    ]


def find_config_path() -> Path:
    """Return the first existing ``influx.toml`` in the discovery order.

    Order: ``INFLUX_CONFIG`` env var, then ``./influx.toml``, then
    ``~/.influx/influx.toml``, then ``/etc/influx/influx.toml``.
    Raises ``ConfigError`` if none are found.
    """
    load_dotenv()
    explicit = os.environ.get("INFLUX_CONFIG", "")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise ConfigError(f"INFLUX_CONFIG points at {p}, but no file exists there")
        return p

    candidates = _default_config_candidates()
    for p in candidates:
        if p.exists():
            return p

    joined = "\n  ".join(str(p) for p in candidates)
    raise ConfigError(
        "No influx.toml found. Set INFLUX_CONFIG or create one of:\n  " + joined
    )


def _parse_bool_env(value: str) -> bool:
    """Parse a boolean environment variable string."""
    return value.lower() in ("true", "1", "yes")


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply supported environment variable overrides to the parsed TOML."""
    overrides: list[tuple[str, str, str, type]] = [
        ("LITHOS_URL", "lithos", "url", str),
        ("LITHOS_MCP_TRANSPORT", "lithos", "transport", str),
        ("INFLUX_ARCHIVE_DIR", "storage", "archive_dir", str),
        ("INFLUX_OTEL_ENABLED", "telemetry", "enabled", bool),
        ("INFLUX_OTEL_CONSOLE_FALLBACK", "telemetry", "console_fallback", bool),
        ("INFLUX_ENVIRONMENT", "telemetry", "environment", str),
    ]

    for env_var, section, key, value_type in overrides:
        value = os.environ.get(env_var)
        if value is None:
            continue
        raw.setdefault(section, {})
        if value_type is bool:
            raw[section][key] = _parse_bool_env(value)
        else:
            raw[section][key] = value

    return raw


def _validate_provider_api_keys(cfg: AppConfig) -> None:
    """Validate that provider api_key_env vars are set in the environment.

    For every configured ``[providers.*]`` block, if ``api_key_env`` is
    non-empty the named env var must be set and non-empty (FR-CFG-8,
    AC-01-E).  ``api_key_env = ''`` skips the check (keyless providers
    like Ollama).
    """
    for name, provider in cfg.providers.items():
        if provider.api_key_env and not os.environ.get(provider.api_key_env):
            raise ConfigError(
                f"Provider {name!r}: api_key_env={provider.api_key_env!r} "
                "is not set or empty in the environment"
            )


def _resolve_prompt_paths(raw: dict[str, Any], *, config_dir: Path) -> None:
    """Resolve relative prompt paths against the config directory in-place."""
    prompts = raw.get("prompts")
    if not isinstance(prompts, dict):
        return
    for entry in prompts.values():
        if not isinstance(entry, dict):
            continue
        prompt_path = entry.get("path")
        if not isinstance(prompt_path, str):
            continue
        p = Path(prompt_path).expanduser()
        if not p.is_absolute():
            p = config_dir / p
        entry["path"] = str(p)


def _validate_prompts(cfg: AppConfig) -> None:
    """Load every configured prompt and validate its template variables."""
    for key, prompt in (
        ("filter", cfg.prompts.filter),
        ("tier1_enrich", cfg.prompts.tier1_enrich),
        ("tier3_extract", cfg.prompts.tier3_extract),
    ):
        text = load_prompt(text=prompt.text, path=prompt.path)
        validate_prompt_variables(key, text)


def load_config(
    path: Path | None = None,
    *,
    check_api_keys: bool = True,
) -> AppConfig:
    """Load, validate, and return the v0.7 ``AppConfig``.

    Reads TOML via stdlib ``tomllib``, validates the raw dict through
    the pydantic schema, and returns a typed ``AppConfig`` instance.
    Environment variable overrides (§19) are applied before validation.

    Parameters
    ----------
    path:
        Explicit path to ``influx.toml``; ``None`` uses discovery.
    check_api_keys:
        When ``True`` (default), raise ``ConfigError`` if any
        provider's ``api_key_env`` is unset.  Set to ``False`` for
        long-running ``serve`` mode where the background probe loop
        (``probes.py``) monitors credential presence instead.
    """
    load_dotenv()
    config_path = path if path is not None else find_config_path()

    try:
        with config_path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: invalid TOML: {exc}") from exc

    _apply_env_overrides(raw)
    _resolve_prompt_paths(raw, config_dir=config_path.parent)

    try:
        cfg = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc

    if check_api_keys:
        _validate_provider_api_keys(cfg)
    _validate_prompts(cfg)

    return cfg
