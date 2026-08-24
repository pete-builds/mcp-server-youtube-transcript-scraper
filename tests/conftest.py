"""Shared test setup.

Every setting in this project is env-driven, and `Settings()` reads the real
environment. A developer with `LOG_LEVEL=debug` exported (valid nearly
everywhere, uppercase-only here) or `MCP_PORT=99999` in their shell would watch
unrelated tests fail with pydantic validation errors, while CI stayed green
because its environment is bare. Isolate every test from that by default;
a test that wants a variable sets it explicitly with monkeypatch.
"""

from __future__ import annotations

import pytest

from mcp_youtube.config import Settings

#: Every variable `Settings` reads. Keep in sync with `mcp_youtube.config`.
SETTING_VARS = (
    "MCP_TRANSPORT",
    "MCP_HOST",
    "MCP_PORT",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "DEFAULT_LANGUAGE",
    "FALLBACK_LANGUAGES",
    "RATE_LIMIT_MIN_SECONDS",
    "RATE_LIMIT_MAX_SECONDS",
    "WEBSHARE_PROXY_USERNAME",
    "WEBSHARE_PROXY_PASSWORD",
)


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise both config sources for every test.

    Stripping the environment is only half of it: `Settings` also reads a `.env`
    from the working directory, so a bare `Settings()` in any test still picked
    up the repo-root `.env` that this project's own README tells people to
    create. Tests that mean to exercise dotenv loading pass `_env_file`
    explicitly, which still wins over this default.
    """
    for name in SETTING_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
