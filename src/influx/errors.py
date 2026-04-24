"""Exception hierarchy for Influx.

All Influx-raised exceptions derive from ``InfluxError`` so callers can catch
a single base type.
"""

from __future__ import annotations


class InfluxError(Exception):
    """Base class for all Influx exceptions."""


class ConfigError(InfluxError):
    """Raised when required configuration is missing or invalid."""


class PromptValidationError(ConfigError):
    """Raised when a prompt template has invalid or missing variables."""
