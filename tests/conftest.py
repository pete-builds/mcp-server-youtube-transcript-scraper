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
    """Strip this project's settings vars from the inherited environment."""
    for name in SETTING_VARS:
        monkeypatch.delenv(name, raising=False)
