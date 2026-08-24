"""Transport selection and stdio-safety tests.

The stdio transport is how someone installs this on their own machine without
running a container. Two things have to hold for it: stdio has to be what you
get by default, and nothing may ever be written to stdout except JSON-RPC.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest

from mcp_youtube.config import Settings
from mcp_youtube.logging_setup import configure_logging
from mcp_youtube.server import _parse_args


def test_transport_defaults_to_stdio() -> None:
    """A bare install with no env and no flags must be client-spawnable."""
    assert Settings().mcp_transport == "stdio"


def test_host_defaults_to_loopback() -> None:
    """Don't bind a stranger's laptop to every interface; the container overrides this."""
    assert Settings().mcp_host == "127.0.0.1"


def test_transport_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    assert Settings().mcp_transport == "http"


def test_transport_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "carrier-pigeon")
    with pytest.raises(ValueError):
        Settings()


def test_parse_args_defaults_to_none() -> None:
    """No flag means 'defer to env/config', not 'force stdio'."""
    assert _parse_args([]).transport is None


@pytest.mark.parametrize("value", ["stdio", "http"])
def test_parse_args_accepts_transport(value: str) -> None:
    assert _parse_args(["--transport", value]).transport == value


def test_parse_args_rejects_bad_transport() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--transport", "smoke-signal"])


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_logging_never_writes_to_stdout(fmt: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout is the JSON-RPC wire under stdio: one stray log line breaks framing."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    configure_logging(level="INFO", fmt=fmt)
    logging.getLogger("mcp_youtube.test").info("canary")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert out.getvalue() == ""
    assert "canary" in err.getvalue()
