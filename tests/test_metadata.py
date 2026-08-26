"""Tests for the oEmbed metadata client and the fetch_video_metadata tool.

The HTTP status mapping is hand-written, so it is tested against every branch
rather than through the happy path only. Live network calls are gated behind
RUN_LIVE, like the transcript smoke test.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error

import pytest

from mcp_youtube.clients.metadata import (
    OEMBED_ENDPOINT,
    VideoMetadata,
    YouTubeMetadataClient,
)
from mcp_youtube.clients.youtube import (
    TranscriptNotFound,
    TranscriptRateLimited,
    TranscriptUpstreamDown,
)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url=OEMBED_ENDPOINT, code=code, msg="boom", hdrs=None, fp=None)


class _FakeResponse(io.BytesIO):
    """Minimal stand-in for the context manager urlopen returns."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, behaviour):
    """Patch urlopen, handing the behaviour the request URL as a plain string.

    The client passes a urllib Request (it sets a User-Agent), so unwrap it here
    rather than making every behaviour know that.
    """
    import mcp_youtube.clients.metadata as mod

    def fake_urlopen(request, timeout=None):
        url = getattr(request, "full_url", request)
        return behaviour(url)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_fetch_returns_title_and_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "title": "Me at the zoo",
        "author_name": "jawed",
        "thumbnail_url": "https://i.ytimg.com/vi/x/hq.jpg",
        "type": "video",
    }
    _patch_urlopen(monkeypatch, lambda url: _FakeResponse(json.dumps(payload).encode()))

    meta = await YouTubeMetadataClient(min_interval_seconds=0).fetch("jNQXAC9IVRw")

    assert isinstance(meta, VideoMetadata)
    assert meta.title == "Me at the zoo"
    assert meta.channel == "jawed"
    assert meta.video_id == "jNQXAC9IVRw"
    assert meta.url == "https://www.youtube.com/watch?v=jNQXAC9IVRw"


async def test_fetch_requests_the_right_video(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong URL here would silently return another video's title."""
    seen: dict[str, str] = {}

    def behaviour(url):
        seen["url"] = url
        return _FakeResponse(json.dumps({"title": "t", "author_name": "c"}).encode())

    _patch_urlopen(monkeypatch, behaviour)
    await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")

    assert seen["url"].startswith(OEMBED_ENDPOINT)
    assert "dQw4w9WgXcQ" in seen["url"]
    assert "format=json" in seen["url"]


async def test_fetch_tolerates_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent keys must yield empty strings, never None, or frontmatter breaks."""
    _patch_urlopen(monkeypatch, lambda url: _FakeResponse(b"{}"))

    meta = await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")

    assert meta.title == "" and meta.channel == "" and meta.thumbnail_url == ""


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 404])
async def test_absent_video_maps_to_not_found(code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a genuinely missing video is NOT_FOUND.

    401/403 mean embedding is disabled, which is a soft miss rather than an
    error, because the transcript is usually still fetchable. See
    test_embedding_disabled_is_a_soft_miss_not_not_found.
    """

    def behaviour(url):
        raise _http_error(code)

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptNotFound):
        await YouTubeMetadataClient(min_interval_seconds=0).fetch("zzzzzzzzzzz")


async def test_429_maps_to_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    def behaviour(url):
        raise _http_error(429)

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptRateLimited):
        await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")


@pytest.mark.parametrize("code", [500, 502, 503])
async def test_server_errors_map_to_upstream_down(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def behaviour(url):
        raise _http_error(code)

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")


async def test_network_failure_maps_to_upstream_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def behaviour(url):
        raise urllib.error.URLError("no route to host")

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")


async def test_timeout_maps_to_upstream_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def behaviour(url):
        raise TimeoutError("timed out")

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")


async def test_non_json_body_maps_to_upstream_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTML error page with a 200 status must not crash the tool."""
    _patch_urlopen(monkeypatch, lambda url: _FakeResponse(b"<html>nope</html>"))

    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")


async def test_unexpected_json_shape_maps_to_upstream_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, lambda url: _FakeResponse(b"[1, 2, 3]"))

    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")


# ---------------------------------------------------------------------------
# Live smoke
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to enable")
async def test_live_metadata_for_a_stable_video() -> None:
    """'Me at the zoo' is the oldest video on YouTube and is not going anywhere."""
    meta = await YouTubeMetadataClient(min_interval_seconds=0).fetch("jNQXAC9IVRw")
    assert meta.title == "Me at the zoo"
    assert meta.channel == "jawed"


# ---------------------------------------------------------------------------
# The tool itself
#
# Everything above drives the client directly. These drive the registered tool,
# which is where a wiring bug lives: a dropped await, url_or_id passed where a
# video_id belongs, a renamed payload key. All of those ship green otherwise.
# ---------------------------------------------------------------------------


class _StubMetadataClient(YouTubeMetadataClient):
    """Returns a canned result without touching the network."""

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.seen: list[str] = []

    async def fetch(self, video_id: str) -> VideoMetadata:  # type: ignore[override]
        self.seen.append(video_id)
        if self._error is not None:
            raise self._error
        return self._result


async def _metadata_tool(stub):
    from mcp_youtube.config import Settings
    from mcp_youtube.server import build_server
    from tests.test_server_tools import _get_tool

    server = build_server(Settings(_env_file=None), metadata_client=stub)
    return await _get_tool(server, "fetch_video_metadata")


async def test_tool_returns_the_metadata_payload() -> None:
    stub = _StubMetadataClient(
        result=VideoMetadata(
            video_id="jNQXAC9IVRw",
            title="Me at the zoo",
            channel="jawed",
            url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            thumbnail_url="https://i.ytimg.com/vi/x/hq.jpg",
        )
    )
    tool = await _metadata_tool(stub)

    data = json.loads(await tool("https://www.youtube.com/watch?v=jNQXAC9IVRw"))["data"]

    assert data["title"] == "Me at the zoo"
    assert data["channel"] == "jawed"
    assert data["video_id"] == "jNQXAC9IVRw"
    assert data["metadata_available"] is True


async def test_tool_passes_the_parsed_id_not_the_raw_url() -> None:
    """Handing the client a full URL would make every oEmbed lookup malformed."""
    stub = _StubMetadataClient(
        result=VideoMetadata(video_id="jNQXAC9IVRw", title="t", channel="c", url="u")
    )
    tool = await _metadata_tool(stub)

    await tool("https://youtu.be/jNQXAC9IVRw")

    assert stub.seen == ["jNQXAC9IVRw"]


async def test_tool_rejects_unparseable_input_without_calling_out() -> None:
    stub = _StubMetadataClient(result=VideoMetadata("x", "t", "c", "u"))
    tool = await _metadata_tool(stub)

    payload = json.loads(await tool("not-a-video!!"))

    assert payload["code"] == "INVALID_INPUT"
    assert stub.seen == []


async def test_tool_surfaces_soft_miss_as_success_not_error() -> None:
    """An agent must be able to tell 'no title' from 'no video' and keep going."""
    stub = _StubMetadataClient(
        result=VideoMetadata(
            video_id="dQw4w9WgXcQ",
            title="",
            channel="",
            url="u",
            available=False,
            reason="embedding disabled",
        )
    )
    tool = await _metadata_tool(stub)

    payload = json.loads(await tool("dQw4w9WgXcQ"))

    assert "error" not in payload
    assert payload["data"]["metadata_available"] is False
    assert payload["data"]["reason"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TranscriptNotFound("gone"), "NOT_FOUND"),
        (TranscriptRateLimited("slow down"), "RATE_LIMITED"),
        (TranscriptUpstreamDown("oembed down"), "UPSTREAM_DOWN"),
    ],
)
async def test_tool_maps_client_errors_to_their_codes(error: Exception, code: str) -> None:
    tool = await _metadata_tool(_StubMetadataClient(error=error))

    assert json.loads(await tool("dQw4w9WgXcQ"))["code"] == code


async def test_tool_catches_an_unexpected_error_as_internal() -> None:
    tool = await _metadata_tool(_StubMetadataClient(error=RuntimeError("boom")))

    assert json.loads(await tool("dQw4w9WgXcQ"))["code"] == "INTERNAL"


# ---------------------------------------------------------------------------
# Soft-miss and politeness behaviour at the client level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
async def test_embedding_disabled_is_a_soft_miss_not_not_found(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """These videos usually still have fetchable transcripts."""

    def behaviour(url):
        raise _http_error(code)

    _patch_urlopen(monkeypatch, behaviour)
    meta = await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")

    assert meta.available is False
    assert meta.title == "" and meta.reason


async def test_empty_title_is_reported_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, lambda url: _FakeResponse(b"{}"))

    meta = await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")

    assert meta.available is False and meta.reason


async def test_oversized_response_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """read() without a cap would buffer whatever the far end sends."""
    from mcp_youtube.clients.metadata import MAX_RESPONSE_BYTES

    _patch_urlopen(monkeypatch, lambda url: _FakeResponse(b"x" * (MAX_RESPONSE_BYTES + 1)))

    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")


async def test_sends_an_honest_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default urllib announces itself as a bot on an endpoint meant for embeds."""
    import mcp_youtube.clients.metadata as mod

    seen: dict[str, str] = {}

    def fake_urlopen(request, timeout=None):
        seen["ua"] = request.get_header("User-agent", "")
        return _FakeResponse(json.dumps({"title": "t", "author_name": "c"}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    await YouTubeMetadataClient(min_interval_seconds=0).fetch("dQw4w9WgXcQ")

    assert "mcp-youtube" in seen["ua"]
    assert "python-urllib" not in seen["ua"].lower()


async def test_back_to_back_calls_are_spaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same source IP as the transcript scraper, and YouTube blocks per IP."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    import mcp_youtube.clients.metadata as mod

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    _patch_urlopen(
        monkeypatch,
        lambda url: _FakeResponse(json.dumps({"title": "t", "author_name": "c"}).encode()),
    )

    client = YouTubeMetadataClient(min_interval_seconds=5.0)
    await client.fetch("dQw4w9WgXcQ")
    await client.fetch("jNQXAC9IVRw")

    assert slept, "second call should have waited"
    assert 0 < slept[0] <= 5.0
