"""Video metadata via YouTube's oEmbed endpoint.

`format_transcript_as_research` needs a title and a channel to build usable
frontmatter. Without them the caller has to supply both, which in practice means
an agent guessing at a video's title, and a research document with a guessed
title is worse than none.

oEmbed is chosen over yt-dlp deliberately. It is a public, documented,
unauthenticated endpoint that returns exactly the two fields needed, so it costs
no new dependency: the standard library covers it. yt-dlp would add a large,
fast-moving scraper to the image to fetch two strings.

**On throttling.** This is a different endpoint from the one
`YouTubeTranscriptClient` scrapes, but it is the same host and, more to the
point, the same source IP, and YouTube's blocking is per-IP. An agent walking a
50-video playlist would otherwise fire 50 unthrottled requests from the address
that `fetch_transcript` depends on, so a light minimum interval applies here
too. It is one second rather than the transcript client's five to ten, because
this endpoint exists to be called by embedding pages and is far less sensitive.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import time
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

#: Light politeness interval. See the module docstring on why this is not zero.
DEFAULT_MIN_INTERVAL_SECONDS = 1.0

#: An oEmbed document is a few hundred bytes. Anything past this is not a real
#: response, and read() without a cap would happily buffer it all.
MAX_RESPONSE_BYTES = 256 * 1024

#: Default urllib sends "Python-urllib/3.13", which is an obvious bot signature
#: on an endpoint meant for embedding pages. Identify honestly instead.
USER_AGENT = "mcp-youtube (+https://github.com/pete-builds/mcp-server-youtube-transcript-scraper)"


@dataclass(frozen=True)
class VideoMetadata:
    """The subset of oEmbed's response that frontmatter actually uses.

    ``available`` is False when YouTube answered but usable metadata could not
    be obtained: an embedding-disabled video, or a response with no title. The
    caller can still fetch that video's transcript, so this is reported as a
    successful call with a flag rather than as an error.
    """

    video_id: str
    title: str
    channel: str
    url: str
    thumbnail_url: str = ""
    available: bool = True
    reason: str = ""


class YouTubeMetadataClient:
    """Fetches title and channel for a video id.

    Stateless apart from the politeness clock. Raises the same exception types
    the transcript client raises, so both tools report failures identically.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._timeout = timeout_seconds
        self._min_interval = min_interval_seconds
        self._last_call: float | None = None

    async def _await_min_interval(self) -> None:
        if self._min_interval <= 0:
            return
        if self._last_call is not None:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    async def fetch(self, video_id: str) -> VideoMetadata:
        """Return metadata for ``video_id``, or raise the shared error contract."""
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        query = urllib.parse.urlencode({"url": watch_url, "format": "json"})
        request_url = f"{OEMBED_ENDPOINT}?{query}"

        await self._await_min_interval()

        # Blocking urllib in a worker thread, matching how the transcript client
        # wraps its own blocking library.
        payload = await asyncio.to_thread(self._get, request_url, video_id)

        if payload is None:
            return VideoMetadata(
                video_id=video_id,
                title="",
                channel="",
                url=watch_url,
                available=False,
                reason="this video has embedding disabled, so oEmbed will not describe it",
            )

        title = str(payload.get("title") or "").strip()
        channel = str(payload.get("author_name") or "").strip()
        return VideoMetadata(
            video_id=video_id,
            title=title,
            channel=channel,
            url=watch_url,
            thumbnail_url=str(payload.get("thumbnail_url") or "").strip(),
            # An empty title is the same failure the module docstring is about,
            # so say so rather than handing back blank frontmatter as a success.
            available=bool(title),
            reason="" if title else "oEmbed returned no title for this video",
        )

    def _get(self, request_url: str, video_id: str) -> dict | None:
        """Fetch and parse, or return None for a soft miss.

        None means "YouTube answered, but will not describe this video", which
        the caller reports as an unavailable-metadata success. Anything genuinely
        broken raises.
        """
        request = urllib.request.Request(request_url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            # HTTPError holds a live response; release the socket rather than
            # leaving it for the cycle collector via __cause__.
            exc.close()
            # 401/403 mean embedding is disabled. The video itself is usually
            # fine and its transcript is usually fetchable, so this must not
            # look like "video gone" or an agent will abandon the chain here.
            if code in (401, 403):
                return None
            if code in (400, 404):
                raise TranscriptNotFound(
                    f"no such video {video_id} (oEmbed returned {code})"
                ) from exc
            if code == 429:
                raise TranscriptRateLimited(
                    f"YouTube rate-limited the metadata request for {video_id}"
                ) from exc
            raise TranscriptUpstreamDown(f"oEmbed returned HTTP {code} for {video_id}") from exc
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as exc:
            # http.client.HTTPException covers IncompleteRead and BadStatusLine,
            # which derive from Exception rather than OSError and so would
            # otherwise escape this contract entirely during read().
            raise TranscriptUpstreamDown(f"could not reach oEmbed for {video_id}: {exc}") from exc

        if len(raw) > MAX_RESPONSE_BYTES:
            raise TranscriptUpstreamDown(
                f"oEmbed response for {video_id} exceeded {MAX_RESPONSE_BYTES} bytes"
            )

        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise TranscriptUpstreamDown(f"oEmbed returned a non-JSON body for {video_id}") from exc

        if not isinstance(payload, dict):
            raise TranscriptUpstreamDown(f"oEmbed returned an unexpected shape for {video_id}")
        return payload
