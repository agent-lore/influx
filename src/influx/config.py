"""Configuration and environment loading.

Influx is configured by a TOML file (``influx.toml``). This module defines
the in-memory representation of that file, validates it on load, and
applies a small set of environment-variable overrides so that env beats
file beats built-in default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import load_dotenv

from influx.errors import ConfigError

__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_ENVIRONMENT",
    "DEFAULT_GREETING",
    "DEFAULT_LOG_LEVEL",
    "ConfigError",
    "InfluxConfig",
    "LogLevel",
    "LoggingConfig",
    "StorageConfig",
    "find_config_path",
    "load_config",
    "parse_log_level",
]

# ── Literal types + validators ─────────────────────────────────────────

LogLevel = Literal["debug", "info", "warning", "error"]

_VALID_LOG_LEVEL: set[str] = {"debug", "info", "warning", "error"}


# ── Defaults ───────────────────────────────────────────────────────────

DEFAULT_DATA_DIR = Path.home() / ".influx" / "data"
DEFAULT_ENVIRONMENT = "dev"
DEFAULT_GREETING = "Hello"
DEFAULT_LOG_LEVEL: LogLevel = "info"


def parse_log_level(value: str) -> LogLevel:
    """Validate and narrow a string to a ``LogLevel`` literal."""
    if value not in _VALID_LOG_LEVEL:
        raise ConfigError(
            f"Invalid log level {value!r}. Valid values: {sorted(_VALID_LOG_LEVEL)}"
        )
    return cast(LogLevel, value)


# ── Dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path = DEFAULT_DATA_DIR


@dataclass(frozen=True)
class LoggingConfig:
    level: LogLevel = DEFAULT_LOG_LEVEL


@dataclass(frozen=True)
class InfluxConfig:
    environment: str
    greeting: str
    storage: StorageConfig
    logging: LoggingConfig


# ── Discovery and loading ──────────────────────────────────────────────


def _default_config_candidates() -> list[Path]:
    """Return the filesystem candidates checked when INFLUX_CONFIG is unset.

    Exposed as a helper so tests can monkeypatch the search locations
    without having to override HOME and /etc.
    """
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


def load_config(path: Path | None = None) -> InfluxConfig:
    """Load, validate, and return an ``InfluxConfig``.

    When ``path`` is ``None`` the config file is located via
    :func:`find_config_path`. Env-var overrides (``INFLUX_ENVIRONMENT``,
    ``INFLUX_DATA_DIR``, ``INFLUX_LOG_LEVEL``) are applied after file
    parsing so that env beats file beats built-in default.
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

    influx_section = raw.get("influx", {})
    if not isinstance(influx_section, dict):
        raise ConfigError(f"{config_path}: 'influx' must be a table")

    environment = _optional_str(
        influx_section, "environment", DEFAULT_ENVIRONMENT, config_path, "influx"
    )
    greeting = _optional_str(
        influx_section, "greeting", DEFAULT_GREETING, config_path, "influx"
    )
    storage = _parse_storage(influx_section.get("storage", {}), config_path)
    logging_cfg = _parse_logging(influx_section.get("logging", {}), config_path)

    cfg = InfluxConfig(
        environment=environment,
        greeting=greeting,
        storage=storage,
        logging=logging_cfg,
    )
    return _apply_env_overrides(cfg)


# ── Internal parsing helpers ───────────────────────────────────────────


def _parse_storage(data: Any, config_path: Path) -> StorageConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [influx.storage] must be a table")
    data_dir = _optional_path(
        data, "data_dir", DEFAULT_DATA_DIR, config_path, "influx.storage"
    )
    return StorageConfig(data_dir=data_dir)


def _parse_logging(data: Any, config_path: Path) -> LoggingConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [influx.logging] must be a table")
    level_raw = data.get("level", DEFAULT_LOG_LEVEL)
    if not isinstance(level_raw, str):
        raise ConfigError(f"{config_path}: [influx.logging].level must be a string")
    try:
        level = parse_log_level(level_raw)
    except ConfigError as exc:
        raise ConfigError(f"{config_path}: [influx.logging]: {exc}") from exc
    return LoggingConfig(level=level)


def _optional_str(
    data: dict[str, Any],
    key: str,
    default: str,
    config_path: Path,
    section: str,
) -> str:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a string")
    return value


def _optional_path(
    data: dict[str, Any],
    key: str,
    default: Path,
    config_path: Path,
    section: str,
) -> Path:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a string path")
    return Path(value).expanduser()


def _apply_env_overrides(cfg: InfluxConfig) -> InfluxConfig:
    env_override = os.environ.get("INFLUX_ENVIRONMENT", "")
    data_dir_override = os.environ.get("INFLUX_DATA_DIR", "")
    log_level_override = os.environ.get("INFLUX_LOG_LEVEL", "")

    if not env_override and not data_dir_override and not log_level_override:
        return cfg

    new_cfg = cfg
    if env_override:
        new_cfg = replace(new_cfg, environment=env_override)
    if data_dir_override:
        new_storage = replace(
            new_cfg.storage, data_dir=Path(data_dir_override).expanduser()
        )
        new_cfg = replace(new_cfg, storage=new_storage)
    if log_level_override:
        new_logging = replace(
            new_cfg.logging, level=parse_log_level(log_level_override)
        )
        new_cfg = replace(new_cfg, logging=new_logging)
    return new_cfg
