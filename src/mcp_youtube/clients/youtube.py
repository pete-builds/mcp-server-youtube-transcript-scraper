"""Thin async wrapper over ``youtube-transcript-api`` with self-rate-limiting.

YouTube doesn't expose a free transcript API; we use jdepoix's
``youtube-transcript-api`` (community library, MIT). It hits YouTube's
internal caption endpoints from this server's IP — which means a single
server burning through requests can get its IP banned.

To avoid that:
- Self-throttle: sleep a random ``[min, max]`` seconds between calls.
- Surface ``IpBlocked`` / ``RequestBlocked`` distinctly so callers don't
  retry-spam.
- Reserve the Webshare proxy env vars (not wired in v0.1).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("mcp_youtube.client")


class TranscriptError(Exception):
    """Base class for transcript-fetch failures surfaced to MCP callers."""

    code: str = "INTERNAL"


class TranscriptNotFound(TranscriptError):
    code = "NOT_FOUND"


class TranscriptInvalidInput(TranscriptError):
    code = "INVALID_INPUT"


class TranscriptRateLimited(TranscriptError):
    """YouTube returned an IP-block / request-blocked response."""

    code = "RATE_LIMITED"


class TranscriptUpstreamDown(TranscriptError):
    code = "UPSTREAM_DOWN"


@dataclass
class FetchedTranscript:
    """Lightweight, JSON-serialisable view of an upstream FetchedTranscript."""

    video_id: str
    language: str
    language_code: str
    is_generated: bool
    snippets: list[dict[str, Any]]


class YouTubeTranscriptClient:
    """Self-rate-limited transcript fetcher.

    The underlying ``youtube_transcript_api`` is sync; we run it in an
    asyncio thread executor to keep the FastMCP event loop free.
    """

    def __init__(
        self,
        *,
        rate_limit_min_seconds: float,
        rate_limit_max_seconds: float,
        webshare_proxy_username: str = "",
        webshare_proxy_password: str = "",
    ) -> None:
        self._min = max(0.0, rate_limit_min_seconds)
        self._max = max(self._min, rate_limit_max_seconds)
        self._webshare_username = webshare_proxy_username
        self._webshare_password = webshare_proxy_password
        # Last-call timestamp shared across calls. Initialised to "long ago" so
        # the very first call doesn't sleep.
        self._last_call_monotonic: float = 0.0
        self._lock = asyncio.Lock()
        self._api: Any | None = None  # lazy import below
        if self._webshare_username and self._webshare_password:
            logger.warning(
                "webshare proxy env vars set but proxy support is NOT wired in v0.1; "
                "fetches will use the server's direct IP",
                extra={"webshare_proxy_user": self._webshare_username[:2] + "***"},
            )

    def _build_api(self) -> Any:
        """Lazy import to keep startup fast and tests cheap."""
        if self._api is not None:
            return self._api
        # Imported lazily so the module can be imported under a clean test
        # environment without the upstream library installed.
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore[import-not-found]

        self._api = YouTubeTranscriptApi()
        return self._api

    async def _await_rate_limit(self) -> None:
        """Sleep so we honour the ``[min, max]`` window since the last call."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call_monotonic
            wait_for = random.uniform(self._min, self._max)
            if elapsed < wait_for:
                sleep_seconds = wait_for - elapsed
                logger.info(
                    "rate-limited fetch — sleeping",
                    extra={"sleep_seconds": round(sleep_seconds, 2)},
                )
                await asyncio.sleep(sleep_seconds)
            self._last_call_monotonic = time.monotonic()

    async def fetch(
        self,
        video_id: str,
        *,
        languages: list[str],
    ) -> FetchedTranscript:
        """Fetch the transcript for ``video_id``, preferring ``languages`` in order.

        Raises one of the ``TranscriptError`` subclasses on failure.
        """
        await self._await_rate_limit()

        def _do_fetch() -> FetchedTranscript:
            from youtube_transcript_api import (
                _errors as ytt_errors,  # type: ignore[import-not-found]
            )

            api = self._build_api()
            try:
                fetched = api.fetch(video_id, languages=languages)
            except (ytt_errors.RequestBlocked, ytt_errors.IpBlocked) as exc:
                logger.error(
                    "youtube returned IP-block — refusing to retry; consider Webshare proxy",
                    extra={"video_id": video_id, "exception": type(exc).__name__},
                )
                raise TranscriptRateLimited(
                    "YouTube blocked this server's IP. Stop and wait — do not retry."
                ) from exc
            except ytt_errors.VideoUnavailable as exc:
                raise TranscriptNotFound(f"video {video_id} is unavailable") from exc
            except ytt_errors.NoTranscriptFound as exc:
                raise TranscriptNotFound(
                    f"no transcript available for {video_id} in {languages}"
                ) from exc
            except ytt_errors.TranscriptsDisabled as exc:
                raise TranscriptNotFound(f"transcripts are disabled for {video_id}") from exc
            except ytt_errors.InvalidVideoId as exc:
                raise TranscriptInvalidInput(f"invalid video id: {video_id}") from exc
            except ytt_errors.CouldNotRetrieveTranscript as exc:
                # Catch-all for any other library-defined failure.
                raise TranscriptUpstreamDown(f"upstream error fetching {video_id}: {exc}") from exc
            except Exception as exc:
                logger.exception("unexpected fetch error", extra={"video_id": video_id})
                raise TranscriptUpstreamDown(
                    f"unexpected error fetching {video_id}: {exc}"
                ) from exc

            snippets = [
                {
                    "text": getattr(s, "text", ""),
                    "start": float(getattr(s, "start", 0.0)),
                    "duration": float(getattr(s, "duration", 0.0)),
                }
                for s in fetched
            ]
            return FetchedTranscript(
                video_id=video_id,
                language=getattr(fetched, "language", ""),
                language_code=getattr(fetched, "language_code", ""),
                is_generated=bool(getattr(fetched, "is_generated", False)),
                snippets=snippets,
            )

        # youtube-transcript-api is sync (uses requests under the hood); run it
        # off the event loop so concurrent MCP calls don't block each other.
        return await asyncio.to_thread(_do_fetch)
