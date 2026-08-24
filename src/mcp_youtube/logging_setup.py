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
    """Return the record's ``extra`` payload, scrubbed.

    Passes the whole mapping through ``_scrub`` rather than scrubbing only the
    values, so a sensitive name used as a top-level extra key (``extra={
    "webshare_proxy_password": ...}``) is redacted like a nested one.
    """
    return _scrub(
        {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_LOGRECORD_FIELDS and not key.startswith("_")
        }
    )


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
        """Render the extras, and never raise while doing it.

        A formatter that throws loses the record and, for a RecursionError,
        escapes logging's own error handling into the caller. Diagnostics are
        not worth that, so anything awkward degrades to a repr.
        """
        try:
            extras = _collect_extras(record)
            if not extras:
                return ""
            return " ".join(f"{k}={json.dumps(v, default=str)}" for k, v in extras.items())
        except Exception:
            try:
                return f"<unrenderable extra: {type(record.__dict__).__name__}>"
            except Exception:
                return "<unrenderable extra>"


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
