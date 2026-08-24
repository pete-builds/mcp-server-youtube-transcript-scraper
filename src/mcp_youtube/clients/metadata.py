"""Video metadata via YouTube's oEmbed endpoint.

`format_transcript_as_research` needs a title and a channel to build usable
frontmatter. Without them the caller has to supply both, which in practice means
an agent guessing at a video's title, and a research document with a guessed
title is worse than none.

oEmbed is chosen over yt-dlp deliberately. It is a public, documented,
unauthenticated endpoint that returns exactly the two fields needed, so it costs
no new dependency: the standard library covers it. yt-dlp would add a large,
fast-moving scraper to the image to fetch two strings.

It is also a different endpoint from the one `YouTubeTranscriptClient` scrapes,
so it does not share that client's 5-10 second anti-ban throttle. Making a
metadata lookup wait five seconds would make the research flow painful for no
gain, and this endpoint exists to be called by embedding pages. It still gets a
short timeout and no retry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from mcp_youtube.clients.youtube import (
    TranscriptNotFound,
    TranscriptRateLimited,
    TranscriptUpstreamDown,
)

logger = logging.getLogger("mcp_youtube.clients.metadata")

OEMBED_ENDPOINT = "https://www.youtube.com/oembed"

#: oEmbed answers in well under a second when it answers at all. A long timeout
#: here would just hold an MCP client's tool call open for no benefit.
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class VideoMetadata:
    """The subset of oEmbed's response that frontmatter actually uses."""

    video_id: str
    title: str
    channel: str
    url: str
    thumbnail_url: str = ""


class YouTubeMetadataClient:
    """Fetches title and channel for a video id.

    Stateless and unthrottled. See the module docstring for why this does not
    share the transcript client's rate limiter.
    """

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds

    async def fetch(self, video_id: str) -> VideoMetadata:
        """Return metadata for ``video_id``, or raise the shared error contract.

        Raises the same exception types the transcript client raises, so both
        tools report failures identically to the caller.
        """
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        query = urllib.parse.urlencode({"url": watch_url, "format": "json"})
        request_url = f"{OEMBED_ENDPOINT}?{query}"

        # Blocking urllib in a worker thread, matching how the transcript client
        # wraps its own blocking library.
        payload = await asyncio.to_thread(self._get, request_url, video_id)

        return VideoMetadata(
            video_id=video_id,
            title=str(payload.get("title") or "").strip(),
            channel=str(payload.get("author_name") or "").strip(),
            url=watch_url,
            thumbnail_url=str(payload.get("thumbnail_url") or "").strip(),
        )

    def _get(self, request_url: str, video_id: str) -> dict:
        """Perform the request and map transport failures onto the error contract."""
        try:
            with urllib.request.urlopen(request_url, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # oEmbed answers 400 for a malformed or unknown id and 401/403/404
            # for private, removed, or embedding-disabled videos. All of those
            # mean the same thing to a caller: no metadata for this video.
            if exc.code in (400, 401, 403, 404):
                raise TranscriptNotFound(
                    f"no metadata available for {video_id} (oEmbed returned {exc.code}); "
                    "the video may be private, removed, or have embedding disabled"
                ) from exc
            if exc.code == 429:
                raise TranscriptRateLimited(
                    f"YouTube rate-limited the metadata request for {video_id}"
                ) from exc
            raise TranscriptUpstreamDown(f"oEmbed returned HTTP {exc.code} for {video_id}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TranscriptUpstreamDown(f"could not reach oEmbed for {video_id}: {exc}") from exc

        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise TranscriptUpstreamDown(f"oEmbed returned a non-JSON body for {video_id}") from exc

        if not isinstance(payload, dict):
            raise TranscriptUpstreamDown(f"oEmbed returned an unexpected shape for {video_id}")
        return payload
