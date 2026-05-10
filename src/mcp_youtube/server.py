"""MCP YouTube — fetch transcripts and shape them as research-ready markdown.

Two tools, single-user, stateless. Self-throttles to keep YouTube from
banning the server's IP (1 request per 5-10 seconds with random jitter).

Transport: Streamable HTTP via FastMCP (current MCP spec).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastmcp import FastMCP

from mcp_youtube import __version__
from mcp_youtube.clients.youtube import (
    TranscriptError,
    YouTubeTranscriptClient,
)
from mcp_youtube.config import Settings, load_settings
from mcp_youtube.formatters import (
    parse_video_id,
    render_research_markdown,
    render_transcript_text,
)
from mcp_youtube.logging_setup import configure_logging

logger = logging.getLogger("mcp_youtube.server")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(data: Any) -> str:
    return json.dumps({"data": data}, default=str)


def _err(message: str, code: str, **details: Any) -> str:
    payload: dict[str, Any] = {"error": message, "code": code}
    if details:
        payload["details"] = details
    return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_server(
    settings: Settings,
    *,
    client: YouTubeTranscriptClient | None = None,
) -> FastMCP:
    """Construct a FastMCP instance with both tools wired up.

    Tests can pass a mocked ``YouTubeTranscriptClient`` to bypass the
    upstream library entirely.
    """
    if client is None:
        client = YouTubeTranscriptClient(
            rate_limit_min_seconds=settings.rate_limit_min_seconds,
            rate_limit_max_seconds=settings.rate_limit_max_seconds,
            webshare_proxy_username=settings.webshare_proxy_username,
            webshare_proxy_password=settings.webshare_proxy_password,
        )

    mcp = FastMCP("YouTube")

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def fetch_transcript(url_or_id: str, language: str = "") -> str:
        """Fetch the transcript for a YouTube video.

        Self-throttled: each call sleeps a random 5-10 seconds (configurable
        via env) since the last call to avoid IP bans. If YouTube blocks
        this server's IP, the tool returns ``RATE_LIMITED`` and refuses to
        retry — wait, then try again later.

        Args:
            url_or_id: Either a YouTube URL (``https://youtu.be/<id>``,
                ``https://www.youtube.com/watch?v=<id>``,
                ``/shorts/<id>``, ``/embed/<id>``) or a bare 11-character
                video ID.
            language: Preferred language code (ISO-639-1, e.g. ``"en"``,
                ``"de"``). Empty (default) uses ``DEFAULT_LANGUAGE`` plus
                ``FALLBACK_LANGUAGES`` from server config.

        Returns:
            Success: ``{"data": {"video_id": str, "language": str,
                "language_code": str, "is_generated": bool,
                "transcript": str (with [MM:SS] markers), "snippet_count": int,
                "duration_seconds": float, "url": str}}``.
            Failure: ``{"error", "code", "details"}`` with code in
            ``{INVALID_INPUT, NOT_FOUND, RATE_LIMITED, UPSTREAM_DOWN, INTERNAL}``.

        Example:
            ``fetch_transcript("https://www.youtube.com/watch?v=dQw4w9WgXcQ")``
        """
        video_id = parse_video_id(url_or_id)
        if not video_id:
            return _err(
                f"could not parse a YouTube video ID from {url_or_id!r}",
                "INVALID_INPUT",
                input=url_or_id,
            )

        primary = (language or settings.default_language).strip()
        fallback = [code for code in settings.fallback_language_list() if code != primary]
        languages = [primary, *fallback] if primary else settings.fallback_language_list()
        if not languages:
            languages = ["en"]

        try:
            fetched = await client.fetch(video_id, languages=languages)
        except TranscriptError as exc:
            logger.warning(
                "fetch_transcript failed",
                extra={
                    "video_id": video_id,
                    "code": exc.code,
                    "reason": str(exc),
                },
            )
            return _err(str(exc), exc.code, video_id=video_id, languages=languages)
        except Exception as exc:
            logger.exception("fetch_transcript unexpected", extra={"video_id": video_id})
            return _err(f"unexpected error: {exc}", "INTERNAL", video_id=video_id)

        transcript_text = render_transcript_text(fetched.snippets, with_timestamps=True)
        last_start = fetched.snippets[-1].get("start", 0.0) if fetched.snippets else 0.0
        last_duration = fetched.snippets[-1].get("duration", 0.0) if fetched.snippets else 0.0
        return _ok(
            {
                "video_id": fetched.video_id,
                "language": fetched.language,
                "language_code": fetched.language_code,
                "is_generated": fetched.is_generated,
                "transcript": transcript_text,
                "snippet_count": len(fetched.snippets),
                "duration_seconds": round(float(last_start) + float(last_duration), 2),
                "url": f"https://www.youtube.com/watch?v={fetched.video_id}",
            }
        )

    @mcp.tool()
    async def format_transcript_as_research(
        transcript: str,
        video_id: str,
        title: str = "",
        channel: str = "",
        url: str = "",
        language: str = "",
        is_generated: bool = False,
    ) -> str:
        """Wrap a fetched transcript in research-report frontmatter.

        Pure formatting — no network I/O, no rate limiting. Intended to be
        called after ``fetch_transcript`` (or with a transcript you already
        have). Returns the markdown as a string; the caller writes it to
        disk in their own workspace (this server is stateless).

        Args:
            transcript: The transcript body (already concatenated, with or
                without ``[MM:SS]`` markers).
            video_id: 11-character YouTube ID. Used to build the canonical
                URL when ``url`` is empty.
            title: Video title. Defaults to ``"YouTube transcript: <video_id>"``.
                Drives the slug.
            channel: Channel name (free-form, optional).
            url: Canonical URL. Defaults to the watch URL for ``video_id``.
            language: Human-readable language label (e.g. ``"English"``).
            is_generated: ``True`` if this is YouTube's auto-generated track.

        Returns:
            Success: ``{"data": {"slug": str, "suggested_path":
                "research-reports/<slug>.md", "frontmatter_markdown": str}}``.
            Failure: ``{"error", "code"}`` with ``INVALID_INPUT`` if
            ``video_id`` or ``transcript`` is empty.

        Example:
            ``format_transcript_as_research(transcript="[0:00] hello\\n...",
                video_id="dQw4w9WgXcQ", title="Never Gonna Give You Up",
                channel="Rick Astley")``
        """
        if not (video_id or "").strip():
            return _err("video_id is required", "INVALID_INPUT")
        if not (transcript or "").strip():
            return _err("transcript is empty", "INVALID_INPUT", video_id=video_id)

        try:
            rendered = render_research_markdown(
                transcript=transcript,
                video_id=video_id,
                title=title,
                channel=channel,
                url=url,
                language=language,
                is_generated=is_generated,
            )
        except Exception as exc:
            logger.exception(
                "format_transcript_as_research failed",
                extra={"video_id": video_id},
            )
            return _err(f"failed to render markdown: {exc}", "INTERNAL", video_id=video_id)
        return _ok(rendered)

    return mcp


# ---------------------------------------------------------------------------
# Module-level entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint used by the Docker image."""
    settings = load_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    logger.info(
        "MCP YouTube starting",
        extra={"version": __version__, "config": settings.safe_repr()},
    )
    server = build_server(settings)
    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )


if __name__ == "__main__":
    main()
