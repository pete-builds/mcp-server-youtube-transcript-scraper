"""Transport selection and stdio-safety tests.

The stdio transport is how someone installs this on their own machine without
running a container. Three things have to hold for it: stdio is what you get by
default, nothing is ever written to stdout except JSON-RPC, and an unrelated
`.env` in the client's working directory cannot reconfigure the server.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest

from mcp_youtube.config import Settings, load_settings
from mcp_youtube.logging_setup import configure_logging
from mcp_youtube.server import _parse_args, _resolve_transport


@pytest.fixture
def clean_settings(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Build Settings with no dotenv in play.

    Without this, a developer who followed the README's Docker instructions
    (`cp .env.example .env`) has a .env in the repo root, and these
    default-value assertions fail on their machine but pass in CI.
    """
    monkeypatch.chdir(tmp_path)
    return lambda: Settings(_env_file=None)


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """configure_logging mutates the global root logger; put it back."""
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    yield
    root.handlers[:] = saved
    root.setLevel(level)


def test_transport_defaults_to_stdio(clean_settings) -> None:
    """A bare install with no env and no flags must be client-spawnable."""
    assert clean_settings().mcp_transport == "stdio"


def test_host_default_is_unchanged_from_0_1(clean_settings) -> None:
    """Flipping this would silently unreach existing bare-metal http deployments."""
    assert clean_settings().mcp_host == "0.0.0.0"


def test_transport_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    assert Settings(_env_file=None).mcp_transport == "http"


def test_transport_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "carrier-pigeon")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


# ---------------------------------------------------------------------------
# dotenv isolation — the stdio install story depends on this
# ---------------------------------------------------------------------------


def test_load_settings_can_skip_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A project .env must not be able to turn a stdio server into an http one."""
    (tmp_path / ".env").write_text("MCP_TRANSPORT=http\nMCP_HOST=0.0.0.0\n")
    monkeypatch.chdir(tmp_path)

    assert load_settings().mcp_transport == "http"  # http path still reads it
    assert load_settings(env_file=None).mcp_transport == "stdio"  # stdio ignores it


def test_unrelated_dotenv_cannot_crash_stdio(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """LOG_LEVEL=debug is ordinary in other projects and is invalid here."""
    (tmp_path / ".env").write_text("LOG_LEVEL=debug\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError):
        load_settings()
    assert load_settings(env_file=None).log_level == "INFO"


def test_resolve_transport_ignores_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Transport is decided before any dotenv is read, or the guard is circular."""
    (tmp_path / ".env").write_text("MCP_TRANSPORT=http\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)

    assert _resolve_transport(_parse_args([])) == "stdio"


def test_resolve_transport_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag beats real env beats default."""
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    assert _resolve_transport(_parse_args([])) == "stdio"

    monkeypatch.setenv("MCP_TRANSPORT", "http")
    assert _resolve_transport(_parse_args([])) == "http"
    assert _resolve_transport(_parse_args(["--transport", "stdio"])) == "stdio"


def test_resolve_transport_rejects_garbage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "smoke-signal")
    with pytest.raises(SystemExit):
        _resolve_transport(_parse_args([]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_parse_args_defaults_to_none() -> None:
    """No flag means 'defer to env/config', not 'force stdio'."""
    args = _parse_args([])
    assert args.transport is None
    assert args.host is None and args.port is None


@pytest.mark.parametrize("value", ["stdio", "http"])
def test_parse_args_accepts_transport(value: str) -> None:
    assert _parse_args(["--transport", value]).transport == value


def test_parse_args_rejects_bad_transport() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--transport", "smoke-signal"])


def test_parse_args_host_and_port() -> None:
    args = _parse_args(["--host", "127.0.0.1", "--port", "9999"])
    assert args.host == "127.0.0.1"
    assert args.port == 9999


# ---------------------------------------------------------------------------
# stdout purity
# ---------------------------------------------------------------------------


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
