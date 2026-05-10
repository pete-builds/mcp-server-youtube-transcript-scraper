"""Live smoke test (manual gate).

Fires only when ``RUN_LIVE=1`` is set; pings YouTube exactly once. Intended
for one-off verification after a deploy, not for CI. Honour the rate-limit;
this test counts toward your throttle budget if you run it back-to-back.
"""

from __future__ import annotations

import json
import os

import pytest

from mcp_youtube.clients.youtube import YouTubeTranscriptClient
from mcp_youtube.config import Settings
from mcp_youtube.server import build_server

LIVE = os.getenv("RUN_LIVE") == "1"
SAMPLE_URL = os.getenv("LIVE_SAMPLE_URL", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")


async def _get_tool(server, name: str):
    tools = await server._list_tools()  # type: ignore[attr-defined]
    for tool in tools:
        if getattr(tool, "name", None) == name:
            return tool.fn
    raise LookupError(f"tool {name!r} not registered")


@pytest.mark.skipif(not LIVE, reason="set RUN_LIVE=1 to enable")
@pytest.mark.asyncio
async def test_fetch_transcript_live() -> None:
    settings = Settings(
        rate_limit_min_seconds=1.0,
        rate_limit_max_seconds=2.0,
        log_format="text",
    )
    client = YouTubeTranscriptClient(
        rate_limit_min_seconds=settings.rate_limit_min_seconds,
        rate_limit_max_seconds=settings.rate_limit_max_seconds,
    )
    server = build_server(settings, client=client)
    fetch = await _get_tool(server, "fetch_transcript")
    raw = await fetch(SAMPLE_URL)
    payload = json.loads(raw)
    if "error" in payload and payload.get("code") == "RATE_LIMITED":
        pytest.skip("YouTube is currently blocking this IP — re-run later")
    assert "data" in payload, payload
    assert payload["data"]["transcript"]
    assert payload["data"]["snippet_count"] > 0
