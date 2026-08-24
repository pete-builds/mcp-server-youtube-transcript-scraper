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
    import mcp_youtube.clients.metadata as mod

    def fake_urlopen(url, timeout=None):
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

    meta = await YouTubeMetadataClient().fetch("jNQXAC9IVRw")

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
    await YouTubeMetadataClient().fetch("dQw4w9WgXcQ")

    assert seen["url"].startswith(OEMBED_ENDPOINT)
    assert "dQw4w9WgXcQ" in seen["url"]
    assert "format=json" in seen["url"]


async def test_fetch_tolerates_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent keys must yield empty strings, never None, or frontmatter breaks."""
    _patch_urlopen(monkeypatch, lambda url: _FakeResponse(b"{}"))

    meta = await YouTubeMetadataClient().fetch("dQw4w9WgXcQ")

    assert meta.title == "" and meta.channel == "" and meta.thumbnail_url == ""


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_absent_video_maps_to_not_found(code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Private, removed, and embedding-disabled all mean 'no metadata' to a caller."""

    def behaviour(url):
        raise _http_error(code)

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptNotFound):
        await YouTubeMetadataClient().fetch("zzzzzzzzzzz")


async def test_429_maps_to_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    def behaviour(url):
        raise _http_error(429)

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptRateLimited):
        await YouTubeMetadataClient().fetch("dQw4w9WgXcQ")


@pytest.mark.parametrize("code", [500, 502, 503])
async def test_server_errors_map_to_upstream_down(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def behaviour(url):
        raise _http_error(code)

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient().fetch("dQw4w9WgXcQ")


async def test_network_failure_maps_to_upstream_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def behaviour(url):
        raise urllib.error.URLError("no route to host")

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient().fetch("dQw4w9WgXcQ")


async def test_timeout_maps_to_upstream_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def behaviour(url):
        raise TimeoutError("timed out")

    _patch_urlopen(monkeypatch, behaviour)
    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient().fetch("dQw4w9WgXcQ")


async def test_non_json_body_maps_to_upstream_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTML error page with a 200 status must not crash the tool."""
    _patch_urlopen(monkeypatch, lambda url: _FakeResponse(b"<html>nope</html>"))

    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient().fetch("dQw4w9WgXcQ")


async def test_unexpected_json_shape_maps_to_upstream_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, lambda url: _FakeResponse(b"[1, 2, 3]"))

    with pytest.raises(TranscriptUpstreamDown):
        await YouTubeMetadataClient().fetch("dQw4w9WgXcQ")


# ---------------------------------------------------------------------------
# Live smoke
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to enable")
async def test_live_metadata_for_a_stable_video() -> None:
    """'Me at the zoo' is the oldest video on YouTube and is not going anywhere."""
    meta = await YouTubeMetadataClient().fetch("jNQXAC9IVRw")
    assert meta.title == "Me at the zoo"
    assert meta.channel == "jawed"
