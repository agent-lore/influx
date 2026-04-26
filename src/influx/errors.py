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


class NetworkError(InfluxError):
    """Raised when an outbound HTTP request fails a guard or network constraint.

    Carries structured context for logging: the offending *url* and a
    *kind* tag describing which constraint was violated (e.g.
    ``"ssrf"``, ``"oversize"``, ``"timeout"``, ``"content_type_mismatch"``).
    An optional *reason* provides human-readable detail.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str,
        kind: str,
        reason: str = "",
    ) -> None:
        super().__init__(message)
        self.url = url
        self.kind = kind
        self.reason = reason


class LithosError(InfluxError):
    """Raised when a Lithos API call fails.

    Carries structured context for logging: *operation* identifies the
    API action, *status_code* the HTTP response code (if available), and
    *detail* any server-supplied message.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str = "",
        status_code: int | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.status_code = status_code
        self.detail = detail


class LCMAError(InfluxError):
    """Raised when an LCMA (LLM content/model analysis) call fails.

    Carries structured context for logging: *model* identifies the LLM
    slot, *stage* the pipeline step, and *detail* any provider message.
    """

    def __init__(
        self,
        message: str,
        *,
        model: str = "",
        stage: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.model = model
        self.stage = stage
        self.detail = detail


class ExtractionError(InfluxError):
    """Raised when content extraction from a fetched document fails.

    Carries structured context for logging: *url* of the source
    document, *stage* where extraction broke, and *detail*.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str = "",
        stage: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.url = url
        self.stage = stage
        self.detail = detail
