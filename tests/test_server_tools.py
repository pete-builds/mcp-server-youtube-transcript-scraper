"""Server-tool tests with a mocked YouTubeTranscriptClient.

Verifies the public tool contracts (success/error JSON shapes) without
hitting YouTube. Live smoke is in ``test_smoke_live.py``, gated by
``RUN_LIVE=1``.
"""

from __future__ import annotations

import json

import pytest

from mcp_youtube.clients.youtube import (
    FetchedTranscript,
    TranscriptNotFound,
    TranscriptRateLimited,
    YouTubeTranscriptClient,
)
from mcp_youtube.config import Settings
from mcp_youtube.server import build_server


class StubClient(YouTubeTranscriptClient):
    """In-memory replacement for the real client."""

    def __init__(self, *, behavior: str = "ok") -> None:
        # Skip the real __init__ — no rate-limiter needed for tests.
        self._behavior = behavior

    async def fetch(self, video_id: str, *, languages: list[str]) -> FetchedTranscript:  # type: ignore[override]
        if self._behavior == "ok":
            return FetchedTranscript(
                video_id=video_id,
                language="English",
                language_code="en",
                is_generated=False,
                snippets=[
                    {"text": "hello world", "start": 0.0, "duration": 1.0},
                    {"text": "and friends", "start": 65.5, "duration": 1.5},
                ],
            )
        if self._behavior == "blocked":
            raise TranscriptRateLimited("YouTube blocked this server's IP")
        if self._behavior == "missing":
            raise TranscriptNotFound("no transcript available")
        raise RuntimeError(f"unknown behavior {self._behavior!r}")


@pytest.fixture
def settings() -> Settings:
    # Pydantic-settings reads .env from cwd; we set values explicitly here
    # so tests don't depend on the dev environment.
    return Settings(
        rate_limit_min_seconds=0.0,
        rate_limit_max_seconds=0.0,
        default_language="en",
        fallback_languages="en-US",
        mcp_host="127.0.0.1",
        mcp_port=3716,
        log_level="INFO",
        log_format="text",
    )


async def _get_tool(server, name: str):
    """Pull a registered tool's callable from the FastMCP server.

    FastMCP 3.x exposes registered tools via ``await server._list_tools()``
    which returns FunctionTool objects with an ``.fn`` attribute.
    """
    tools = await server._list_tools()  # type: ignore[attr-defined]
    for tool in tools:
        if getattr(tool, "name", None) == name:
            return tool.fn
    raise LookupError(f"tool {name!r} not registered")


@pytest.mark.asyncio
async def test_fetch_transcript_success(settings: Settings) -> None:
    server = build_server(settings, client=StubClient(behavior="ok"))
    fetch = await _get_tool(server, "fetch_transcript")
    raw = await fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    payload = json.loads(raw)
    assert "data" in payload
    data = payload["data"]
    assert data["video_id"] == "dQw4w9WgXcQ"
    assert data["language"] == "English"
    assert data["language_code"] == "en"
    assert data["is_generated"] is False
    assert "[0:00] hello world" in data["transcript"]
    assert "[1:05] and friends" in data["transcript"]
    assert data["snippet_count"] == 2
    assert data["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.asyncio
async def test_fetch_transcript_invalid_input(settings: Settings) -> None:
    server = build_server(settings, client=StubClient(behavior="ok"))
    fetch = await _get_tool(server, "fetch_transcript")
    raw = await fetch("not-a-url")
    payload = json.loads(raw)
    assert payload["code"] == "INVALID_INPUT"
    assert "could not parse" in payload["error"]


@pytest.mark.asyncio
async def test_fetch_transcript_rate_limited(settings: Settings) -> None:
    server = build_server(settings, client=StubClient(behavior="blocked"))
    fetch = await _get_tool(server, "fetch_transcript")
    raw = await fetch("dQw4w9WgXcQ")
    payload = json.loads(raw)
    assert payload["code"] == "RATE_LIMITED"
    assert "blocked" in payload["error"].lower()


@pytest.mark.asyncio
async def test_fetch_transcript_not_found(settings: Settings) -> None:
    server = build_server(settings, client=StubClient(behavior="missing"))
    fetch = await _get_tool(server, "fetch_transcript")
    raw = await fetch("dQw4w9WgXcQ")
    payload = json.loads(raw)
    assert payload["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_format_tool_success(settings: Settings) -> None:
    server = build_server(settings, client=StubClient(behavior="ok"))
    fmt = await _get_tool(server, "format_transcript_as_research")
    raw = await fmt(
        transcript="[0:00] hello world",
        video_id="dQw4w9WgXcQ",
        title="Test Video",
        channel="Test Channel",
    )
    payload = json.loads(raw)
    assert "data" in payload
    assert payload["data"]["slug"] == "test-video"
    assert payload["data"]["suggested_path"] == "research-reports/test-video.md"
    md = payload["data"]["frontmatter_markdown"]
    assert "Test Video" in md
    assert "Test Channel" in md
    assert "[0:00] hello world" in md


@pytest.mark.asyncio
async def test_format_tool_validates_input(settings: Settings) -> None:
    server = build_server(settings, client=StubClient(behavior="ok"))
    fmt = await _get_tool(server, "format_transcript_as_research")
    raw = await fmt(transcript="", video_id="dQw4w9WgXcQ")
    payload = json.loads(raw)
    assert payload["code"] == "INVALID_INPUT"
