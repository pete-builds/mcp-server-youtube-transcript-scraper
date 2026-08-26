"""Every tool declares what it does, and which ones leave this process.

This server is stateless by design: fetch_transcript returns a string and
format_transcript_as_research returns a string, and the CALLER writes to disk in
their own workspace. Saying so in the manifest is what lets a client tell this
apart from a server that would have written the file itself.

The two tools differ in exactly one hint, and it is the interesting one.
format_transcript_as_research does no network I/O, no rate limiting, and no
disk access -- given the same arguments it returns the same markdown forever.
Marking it closed-world tells a client it can be called freely without
reaching anyone or counting against anything, which is precisely the property
that makes it safe to call after a rate-limited fetch.
"""

from __future__ import annotations

import pytest

from mcp_youtube.server import build_server
from tests.test_server_tools import StubClient, settings  # noqa: F401


@pytest.fixture
async def tools(settings):  # noqa: F811  (the imported fixture is the point)
    """The live manifest, not the source. What a client would receive.

    Reuses test_server_tools' StubClient and settings so no network call is
    made and no rate limiter runs; the manifest comes from the decorators.
    """
    server = build_server(settings, client=StubClient(behavior="ok"))
    return {t.name: t for t in await server.list_tools()}


async def test_every_tool_is_present(tools):
    """Guards the guard: an empty manifest would pass everything below.

    Pinned as an exact set rather than a count, so a tool appearing or vanishing
    has to be acknowledged here.
    """
    assert set(tools) == {
        "fetch_transcript",
        "fetch_video_metadata",
        "format_transcript_as_research",
    }


async def test_every_tool_is_annotated(tools):
    assert sorted(n for n, t in tools.items() if t.annotations is None) == []


async def test_neither_tool_writes_anything(tools):
    """Stateless by design: both return strings, the caller owns the disk."""
    assert sorted(n for n, t in tools.items() if not t.annotations.readOnlyHint) == []
    assert sorted(n for n, t in tools.items() if t.annotations.destructiveHint) == []


async def test_the_fetch_is_open_world(tools):
    """It reaches YouTube, so an answer can change when a caption track does."""
    assert tools["fetch_transcript"].annotations.openWorldHint is True


async def test_the_metadata_lookup_is_open_world(tools):
    """It calls YouTube's oEmbed endpoint, so it leaves this process too.

    Same source IP as the transcript fetch, and YouTube blocks per IP, so a
    closed-world hint here would invite exactly the free hammering that is not
    free.
    """
    assert tools["fetch_video_metadata"].annotations.openWorldHint is True


async def test_the_formatter_is_closed_world(tools):
    """Pure formatting: no network, no rate limit, no disk.

    This is the hint that carries information here. Marking it open-world
    alongside the fetch would be the easy uniform answer and would hide the one
    real difference between the two tools.
    """
    assert tools["format_transcript_as_research"].annotations.openWorldHint is False
