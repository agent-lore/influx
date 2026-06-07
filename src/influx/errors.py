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


#: Cap on the ``detail`` fragment in ledger-bound error strings.  The
#: per-item telemetry lists truncate to 300 (``influx.telemetry``); the
#: single run-level error string can afford a little more without
#: bloating ``runs.jsonl``.
_LEDGER_DETAIL_MAX_CHARS = 500


def format_exception_for_ledger(exc: BaseException) -> str:
    """Render an exception for the run-failure log and ledger ``error`` field.

    ``str(exc)`` is only the message; structured errors in this module
    (``LithosError``, ``LCMAError``, ``ExtractionError``) carry the
    server-supplied failure text in a ``detail`` attribute that was
    previously dropped (#234) — diagnosing a Lithos-side crash required
    reading the Lithos container logs.  Appends any non-empty
    ``operation``/``status_code``/``detail`` context, truncating
    ``detail`` to :data:`_LEDGER_DETAIL_MAX_CHARS`.

    Deliberately NOT ``LithosError.__str__``: callers parse raw
    ``.detail`` strings (slug-collision recovery) and tests match exact
    messages, so the message itself must stay untouched.
    """
    base = f"{type(exc).__name__}: {exc}"
    parts: list[str] = []
    operation = getattr(exc, "operation", "")
    if operation:
        parts.append(f"operation={operation}")
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        parts.append(f"status={status_code}")
    detail = getattr(exc, "detail", "")
    if detail:
        if len(detail) > _LEDGER_DETAIL_MAX_CHARS:
            detail = detail[:_LEDGER_DETAIL_MAX_CHARS] + "…"
        parts.append(f"detail={detail}")
    if not parts:
        return base
    return f"{base} ({', '.join(parts)})"


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
