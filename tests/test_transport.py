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

from mcp_youtube import server as server_module
from mcp_youtube.config import Settings, load_settings
from mcp_youtube.logging_setup import configure_logging
from mcp_youtube.server import _parse_args, _resolve_transport

#: Every setting this module asserts a default for. Exported in a developer's
#: shell, any one of them turns a green suite red for reasons unrelated to the
#: change under test.
_SETTING_VARS = (
    "MCP_TRANSPORT",
    "MCP_HOST",
    "MCP_PORT",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "DEFAULT_LANGUAGE",
    "FALLBACK_LANGUAGES",
    "RATE_LIMIT_MIN_SECONDS",
    "RATE_LIMIT_MAX_SECONDS",
)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Neutralise BOTH config sources: the ambient dotenv and the real environment.

    A developer who followed the README's Docker instructions has a .env in the
    repo root, and one who is debugging has MCP_TRANSPORT exported. Either used
    to fail these assertions locally while CI stayed green.
    """
    monkeypatch.chdir(tmp_path)
    for name in _SETTING_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def clean_settings(isolated_env):
    """Build Settings with neither a dotenv nor an inherited environment."""
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


def test_load_settings_can_skip_dotenv(isolated_env) -> None:
    """A project .env must not be able to turn a stdio server into an http one."""
    (isolated_env / ".env").write_text("MCP_TRANSPORT=http\nMCP_HOST=0.0.0.0\n")

    assert load_settings().mcp_transport == "http"  # http path still reads it
    assert load_settings(env_file=None).mcp_transport == "stdio"  # stdio ignores it


def test_unrelated_dotenv_cannot_crash_stdio(isolated_env) -> None:
    """LOG_LEVEL=debug is ordinary in other projects and is invalid here."""
    (isolated_env / ".env").write_text("LOG_LEVEL=debug\n")

    with pytest.raises(ValueError):
        load_settings()
    assert load_settings(env_file=None).log_level == "INFO"


def test_resolve_transport_ignores_dotenv(isolated_env) -> None:
    """Transport is decided before any dotenv is read, or the guard is circular."""
    (isolated_env / ".env").write_text("MCP_TRANSPORT=http\n")

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


# ---------------------------------------------------------------------------
# main() wiring
#
# The helpers above are individually correct; these assert that main() actually
# combines them. Without this, the dotenv guard and the LOG_FORMAT fix can both
# regress silently while every other test stays green.
# ---------------------------------------------------------------------------


@pytest.fixture
def run_main(monkeypatch: pytest.MonkeyPatch):
    """Call main() without starting a server, capturing what it decided."""

    def _run(argv):
        captured: dict[str, object] = {}

        class StubServer:
            def run(self, **kwargs):
                captured["run_kwargs"] = kwargs

        real_load = server_module.load_settings

        def spy_load(**kwargs):
            captured["env_file"] = kwargs.get("env_file", ".env")
            captured["overrides"] = {k: v for k, v in kwargs.items() if k != "env_file"}
            return real_load(**kwargs)

        monkeypatch.setattr(server_module, "load_settings", spy_load)
        monkeypatch.setattr(server_module, "build_server", lambda settings: StubServer())
        monkeypatch.setattr(
            server_module,
            "configure_logging",
            lambda level, fmt: captured.update(log_level=level, log_fmt=fmt),
        )
        server_module.main(argv)
        return captured

    return _run


def test_main_skips_dotenv_on_stdio(run_main, isolated_env) -> None:
    """The whole stdio install story rests on this one argument."""
    assert run_main([])["env_file"] is None


def test_main_reads_dotenv_on_http(run_main, isolated_env) -> None:
    assert run_main(["--transport", "http"])["env_file"] == ".env"


def test_main_defaults_stdio_logs_to_text(run_main, isolated_env) -> None:
    """A human is watching the terminal; JSON lines are the wrong default there."""
    assert run_main([])["log_fmt"] == "text"


def test_main_honours_explicit_log_format_from_dotenv(run_main, isolated_env) -> None:
    """The regression guard for the os.environ-vs-dotenv bug.

    A dotenv value never reaches os.environ, so checking os.environ here would
    silently override a LOG_FORMAT the user configured on purpose.
    """
    (isolated_env / ".env").write_text("LOG_FORMAT=json\n")
    assert run_main(["--transport", "http"])["log_fmt"] == "json"


def test_main_honours_explicit_log_format_from_environ(
    run_main, isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    assert run_main([])["log_fmt"] == "json"


def test_main_passes_cli_overrides_through_pydantic(run_main, isolated_env) -> None:
    captured = run_main(["--transport", "http", "--host", "127.0.0.1", "--port", "9999"])
    assert captured["overrides"] == {"mcp_host": "127.0.0.1", "mcp_port": 9999}
    assert captured["run_kwargs"]["host"] == "127.0.0.1"
    assert captured["run_kwargs"]["port"] == 9999


def test_main_stdio_run_takes_no_host_or_port(run_main, isolated_env) -> None:
    """Passing host/port to a stdio run is meaningless and FastMCP may reject it."""
    kwargs = run_main([])["run_kwargs"]
    assert kwargs["transport"] == "stdio"
    assert "host" not in kwargs and "port" not in kwargs


@pytest.mark.parametrize(
    ("value", "expected"),
    [("HTTP", "streamable-http"), (" http ", "streamable-http"), ("Stdio", "stdio")],
)
def test_main_canonicalises_near_miss_transport(
    run_main, isolated_env, monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    """These pass the friendly resolver, so they must not then die in pydantic."""
    monkeypatch.setenv("MCP_TRANSPORT", value)
    captured = run_main([])
    assert captured["run_kwargs"]["transport"] == expected


def test_main_treats_empty_transport_as_unset(
    run_main, isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MCP_TRANSPORT=` in an env_file and `MCP_TRANSPORT: ""` in compose both yield this."""
    monkeypatch.setenv("MCP_TRANSPORT", "")
    assert run_main([])["run_kwargs"]["transport"] == "stdio"


def test_main_rejects_garbage_transport_without_traceback(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "smoke-signal")
    with pytest.raises(SystemExit) as exc:
        server_module.main([])
    assert "smoke-signal" in str(exc.value)


def test_main_reports_bad_config_as_a_message(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid env var should be one line on stderr, not a pydantic traceback."""
    monkeypatch.setenv("LOG_LEVEL", "debug")
    with pytest.raises(SystemExit) as exc:
        server_module.main([])
    assert "invalid configuration" in str(exc.value)


def test_port_flag_is_range_checked() -> None:
    """Assigning onto the model bypassed ge/le and failed deep inside uvicorn."""
    for bad in ["70000", "0", "-1", "notanumber"]:
        with pytest.raises(SystemExit):
            _parse_args(["--port", bad])
