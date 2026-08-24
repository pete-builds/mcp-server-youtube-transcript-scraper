"""Structured logging configuration for mcp-youtube.

JSON formatter for production (one record per line, parseable by Loki/etc).
Plain text fallback for local dev. Sensitive keys (proxy password) get
scrubbed from any ``extra`` dicts a caller passes through.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "proxy_password",
        "webshare_proxy_password",
        "authorization",
    }
)

_RESERVED_LOGRECORD_FIELDS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def _scrub(value: Any) -> Any:
    """Recursively replace sensitive values with ``[REDACTED]``."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if k.lower() in _SENSITIVE_KEYS else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _collect_extras(record: logging.LogRecord) -> dict[str, Any]:
    """Return the record's ``extra`` payload, scrubbed, and never raise.

    Scrubs the top-level key as well as the value, so a sensitive name used
    directly as an extra key (``extra={"webshare_proxy_password": ...}``) is
    redacted like a nested one.

    Every value is scrubbed under its own guard. A formatter that throws costs
    you the log record, and for ``RecursionError`` it costs you the caller too:
    ``StreamHandler.emit`` re-raises that one instead of routing it to
    ``handleError``. Isolating each key also means one awkward value cannot take
    the rest of the line with it, which matters when the salvageable half is the
    identifier you need to debug the failure.
    """
    extras: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_LOGRECORD_FIELDS or key.startswith("_"):
            continue
        if key.lower() in _SENSITIVE_KEYS:
            extras[key] = "[REDACTED]"
            continue
        try:
            extras[key] = _scrub(value)
        except Exception:
            extras[key] = f"<unrenderable {type(value).__name__}>"
    return extras


class JsonFormatter(logging.Formatter):
    """Serialise each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        extras = _collect_extras(record)
        if extras:
            payload["extra"] = extras
        try:
            return json.dumps(payload, default=str)
        except Exception:
            payload["extra"] = {"error": "extras could not be serialised"}
            return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable single line, including the ``extra`` payload.

    The stock formatter renders only ``%(message)s``, which silently drops every
    ``extra`` dict. Since text is the default under the stdio transport, that
    would make the startup line read ``MCP YouTube starting`` with no version,
    transport, or port anywhere.
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)
        line = self.formatMessage(record)

        # Before the traceback, not after: the stock format() appends exception
        # text first, which would glue these onto the last line of the stack.
        rendered = self._render_extras(record)
        if rendered:
            line += " " + rendered

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line += "\n" + record.exc_text
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line

    @staticmethod
    def _render_extras(record: logging.LogRecord) -> str:
        """Render the extras one key at a time, degrading only the bad ones."""
        parts = []
        for key, value in _collect_extras(record).items():
            try:
                rendered = json.dumps(value, default=str)
            except Exception:
                rendered = f'"<unrenderable {type(value).__name__}>"'
            parts.append(f"{key}={rendered}")
        return " ".join(parts)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure the root logger. Idempotent — safe to call multiple times."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # stderr, never stdout. Under the stdio transport, stdout IS the JSON-RPC
    # channel — a single log line written there corrupts the framing and the
    # client drops the connection. Docker captures stderr the same as stdout,
    # so the HTTP/container path loses nothing by this.
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)

    # Silence the urllib/requests INFO chatter that youtube-transcript-api
    # produces internally; we don't want raw URLs (which can leak query
    # params) at INFO level.
    for noisy in ("urllib3", "requests", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
