"""Transport selection and stdio-safety tests.

The stdio transport is how someone installs this on their own machine without
running a container. Three things have to hold for it: stdio is what you get by
default, nothing is ever written to stdout except JSON-RPC, and an unrelated
`.env` in the client's working directory cannot reconfigure the server.
"""

from __future__ import annotations

import io
import logging
import os
import sys

import pytest

from mcp_youtube import server as server_module
from mcp_youtube.config import Settings, load_settings
from mcp_youtube.logging_setup import configure_logging
from mcp_youtube.server import _parse_args, _recognised_dotenv_keys, _resolve_transport


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Move to an empty directory so no ambient .env is in reach.

    The real environment is already stripped for every test by the autouse
    fixture in conftest.py; this adds the other config source. A developer who
    followed the README's Docker instructions has a .env in the repo root, which
    used to fail these default assertions locally while CI stayed green.
    """
    monkeypatch.chdir(tmp_path)
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


def test_transport_reads_env(isolated_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    assert Settings(_env_file=None).mcp_transport == "http"


def test_transport_rejects_unknown_value(isolated_env, monkeypatch: pytest.MonkeyPatch) -> None:
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
    """A dotenv LOG_FORMAT must reach the formatter on the http path.

    Asserts ``text``, not the ``json`` default: asserting the default would pass
    whether or not the dotenv was read at all.
    """
    (isolated_env / ".env").write_text("LOG_FORMAT=text\n")
    assert run_main(["--transport", "http"])["log_fmt"] == "text"


def test_main_dotenv_log_format_is_actually_read(run_main, isolated_env) -> None:
    """Control for the test above: without the dotenv the same call yields json."""
    assert run_main(["--transport", "http"])["log_fmt"] == "json"


def test_main_honours_explicit_log_format_from_environ(
    run_main, isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    assert run_main([])["log_fmt"] == "json"


def test_main_passes_cli_overrides_through_pydantic(run_main, isolated_env) -> None:
    captured = run_main(["--transport", "http", "--host", "127.0.0.1", "--port", "9999"])
    assert captured["overrides"] == {
        "mcp_transport": "http",
        "mcp_host": "127.0.0.1",
        "mcp_port": 9999,
    }
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


class _NeverRuns:
    """Stand-in server whose run() fails loudly instead of binding a port."""

    def run(self, **kwargs):
        raise AssertionError(f"server should not have started: {kwargs}")


# ---------------------------------------------------------------------------
# Process-environment hygiene
# ---------------------------------------------------------------------------


def test_main_does_not_mutate_the_process_environment(run_main, isolated_env) -> None:
    """An earlier fix canonicalised the transport by writing it into os.environ.

    That leaked: a later main() in the same process (and the rest of a test
    session, and every child process) inherited it, so `main([])` with no flag
    and no user-set variable started an HTTP server.
    """
    run_main(["--transport", "http"])
    assert "MCP_TRANSPORT" not in os.environ


def test_main_is_not_influenced_by_a_previous_call(run_main, isolated_env) -> None:
    run_main(["--transport", "http"])
    assert run_main([])["run_kwargs"]["transport"] == "stdio"


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("DATABASE_URL=postgres://x\nSTRIPE_KEY=sk_test\n", False),
        ("", False),
        ("# MCP_PORT=1\n", False),
        ("MCP_PORT=9999\n", True),
        ("log_level=DEBUG\n", True),
        ("RATE_LIMIT_MIN_SECONDS=1\n", True),
    ],
)
def test_dotenv_warning_only_fires_for_settings_we_recognise(
    isolated_env, contents: str, expected: bool
) -> None:
    """Under stdio the cwd is the user's project, and most .env files are unrelated.

    Warning about every one of them would make the default path noisy for
    everybody, so only a file that actually sets something we would have used
    is worth a line.
    """
    env_path = isolated_env / ".env"
    env_path.write_text(contents)
    assert bool(_recognised_dotenv_keys(env_path)) is expected


def test_recognised_dotenv_keys_survives_an_unreadable_file(isolated_env) -> None:
    """This only drives a warning, so it must never be the thing that fails."""
    assert _recognised_dotenv_keys(isolated_env / "does-not-exist.env") == []


def test_main_survives_an_unreadable_dotenv_on_http(isolated_env, monkeypatch) -> None:
    """dotenv raises OSError here, which is neither ValidationError nor SettingsError.

    Covers main() only. The installed console script can still fail earlier and
    outside our control: fastmcp instantiates its own pydantic-settings object at
    import time, which reads the same .env before main() is reachable.
    """
    env_path = isolated_env / ".env"
    env_path.write_text("MCP_PORT=3716\n")
    env_path.chmod(0o000)
    if os.access(env_path, os.R_OK):  # running as root: the chmod means nothing
        pytest.skip("cannot make a file unreadable as this user")

    # os.access is not a reliable predictor of open(): with CAP_DAC_READ_SEARCH
    # the read succeeds, main() falls through to a real server, and an
    # unstubbed run would bind 3716 and block the suite forever.
    monkeypatch.setattr(server_module, "build_server", lambda settings: _NeverRuns())

    with pytest.raises(SystemExit) as exc:
        server_module.main(["--transport", "http"])
    assert "invalid configuration" in str(exc.value)


def test_text_formatter_renders_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Text is the default under stdio; dropping extras would hide the whole config."""
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    configure_logging(level="INFO", fmt="text")
    logging.getLogger("mcp_youtube.test").info("starting", extra={"version": "9.9.9"})
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "9.9.9" in err.getvalue()


def test_text_formatter_still_scrubs_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    configure_logging(level="INFO", fmt="text")
    logging.getLogger("mcp_youtube.test").info(
        "cfg", extra={"config": {"webshare_proxy_password": "hunter2"}}
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = err.getvalue()
    assert "hunter2" not in out
    assert "REDACTED" in out


# ---------------------------------------------------------------------------
# Text formatter safety
#
# Text became the default format under stdio, so this formatter now runs on the
# path most users are on. Everything below is a regression it introduced once.
# ---------------------------------------------------------------------------


def _text_log(monkeypatch: pytest.MonkeyPatch, message: str, **kwargs) -> str:
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    configure_logging(level="INFO", fmt="text")
    log = logging.getLogger("mcp_youtube.test")
    if kwargs.pop("as_exception", False):
        try:
            raise ZeroDivisionError("boom")
        except ZeroDivisionError:
            log.exception(message, **kwargs)
    else:
        log.info(message, **kwargs)
    for handler in logging.getLogger().handlers:
        handler.flush()
    return err.getvalue()


def test_text_formatter_scrubs_a_top_level_extra_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret named directly as the extra key, not nested inside a dict."""
    out = _text_log(monkeypatch, "cfg", extra={"webshare_proxy_password": "hunter2"})
    assert "hunter2" not in out
    assert "REDACTED" in out


def test_json_formatter_scrubs_a_top_level_extra_key(monkeypatch: pytest.MonkeyPatch) -> None:
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    configure_logging(level="INFO", fmt="json")
    logging.getLogger("mcp_youtube.test").info("cfg", extra={"webshare_proxy_password": "hunter2"})
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hunter2" not in err.getvalue()


def test_text_formatter_puts_extras_before_the_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Appending after super().format() glued them onto the last traceback line."""
    out = _text_log(monkeypatch, "failed", extra={"video_id": "abc"}, as_exception=True)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert 'video_id="abc"' in lines[0]
    assert "ZeroDivisionError" in lines[-1]
    assert "video_id" not in lines[-1]


def test_text_formatter_survives_an_unserialisable_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-string dict key made _scrub raise inside emit and dropped the record."""
    out = _text_log(monkeypatch, "odd", extra={"payload": {1: "a", (2, 3): "b"}})
    assert "odd" in out


def test_text_formatter_survives_a_self_referential_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RecursionError used to escape logging's handler and reach the caller."""
    loop: dict = {}
    loop["self"] = loop
    out = _text_log(monkeypatch, "cyclic", extra={"payload": loop})
    assert "cyclic" in out


# ---------------------------------------------------------------------------
# dotenv key detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("export MCP_PORT=9999\n", ["MCP_PORT"]),
        ("﻿MCP_PORT=9999\n", ["MCP_PORT"]),
        ("export  LOG_LEVEL=INFO\n", ["LOG_LEVEL"]),
        ("MCP_PORT=1\nMCP_PORT=2\n", ["MCP_PORT"]),
    ],
)
def test_recognised_dotenv_keys_handles_real_dotenv_forms(
    isolated_env, contents: str, expected: list[str]
) -> None:
    """python-dotenv honours `export KEY=` and a BOM; a silent miss under-warns."""
    path = isolated_env / ".env"
    path.write_bytes(contents.encode("utf-8"))
    assert _recognised_dotenv_keys(path) == expected


def test_startup_log_reports_the_format_actually_in_use(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stdio defaults the format to text while the settings field still says json.

    Inspects the emitted startup record rather than the value handed to
    configure_logging: stubbing the logger, as the other main() tests do, leaves
    this hunk untested, because the record is never rendered.
    """
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(server_module, "build_server", lambda settings: _NeverRuns())

    with pytest.raises(AssertionError):  # _NeverRuns fires instead of serving
        server_module.main([])

    startup = [ln for ln in err.getvalue().splitlines() if "MCP YouTube starting" in ln]
    assert startup, err.getvalue()
    assert '"log_format": "text"' in startup[0]
    assert '"log_format": "json"' not in startup[0]


# ---------------------------------------------------------------------------
# JSON formatter safety
#
# json is the default format, and only stdio flips it to text, so the container
# deployment runs entirely on this path. The safety work landed on the text
# formatter first and left this one unguarded.
# ---------------------------------------------------------------------------


def _json_log(monkeypatch: pytest.MonkeyPatch, message: str, **kwargs) -> str:
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    configure_logging(level="INFO", fmt="json")
    logging.getLogger("mcp_youtube.test").info(message, **kwargs)
    for handler in logging.getLogger().handlers:
        handler.flush()
    return err.getvalue()


def test_json_formatter_survives_a_self_referential_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StreamHandler.emit re-raises RecursionError instead of handling it."""
    loop: dict = {}
    loop["self"] = loop
    assert "cyclic" in _json_log(monkeypatch, "cyclic", extra={"payload": loop})


def test_json_formatter_survives_an_unserialisable_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "odd" in _json_log(monkeypatch, "odd", extra={"payload": {1: "a", (2, 3): "b"}})


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_one_bad_extra_does_not_discard_the_good_ones(
    fmt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The salvageable half is usually the identifier you need to debug the failure."""
    loop: dict = {}
    loop["self"] = loop
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    configure_logging(level="INFO", fmt=fmt)
    logging.getLogger("mcp_youtube.test").info(
        "partial", extra={"video_id": "abc", "payload": loop}
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = err.getvalue()
    assert "abc" in out
    assert "unrenderable" in out


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_secrets_are_scrubbed_in_both_formats(fmt: str, monkeypatch: pytest.MonkeyPatch) -> None:
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    configure_logging(level="INFO", fmt=fmt)
    logging.getLogger("mcp_youtube.test").info(
        "cfg",
        extra={
            "webshare_proxy_password": "top-level",
            "config": {"webshare_proxy_password": "nested"},
        },
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = err.getvalue()
    assert "top-level" not in out
    assert "nested" not in out


def test_recognised_dotenv_keys_handles_tab_after_export(isolated_env) -> None:
    """python-dotenv's binding is `(?:export\\s+)?`, so a tab counts too."""
    path = isolated_env / ".env"
    path.write_bytes(b"export\tMCP_PORT=9999\n")
    assert _recognised_dotenv_keys(path) == ["MCP_PORT"]
