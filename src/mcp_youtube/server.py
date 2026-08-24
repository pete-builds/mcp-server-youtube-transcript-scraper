"""MCP YouTube — fetch transcripts and shape them as research-ready markdown.

Three tools, single-user, stateless. Self-throttles to keep YouTube from
banning the server's IP (1 request per 5-10 seconds with random jitter).

Transport: stdio by default (local, client-spawned); Streamable HTTP when
MCP_TRANSPORT=http, which is what the Docker image sets.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from pydantic import ValidationError
from pydantic_settings import SettingsError
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_youtube import __version__
from mcp_youtube.clients.metadata import YouTubeMetadataClient
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
    metadata_client: YouTubeMetadataClient | None = None,
) -> FastMCP:
    """Construct a FastMCP instance with both tools wired up.

    Tests can pass a mocked ``YouTubeTranscriptClient`` to bypass the
    upstream library entirely.
    """
    if metadata_client is None:
        metadata_client = YouTubeMetadataClient()
    if client is None:
        client = YouTubeTranscriptClient(
            rate_limit_min_seconds=settings.rate_limit_min_seconds,
            rate_limit_max_seconds=settings.rate_limit_max_seconds,
            webshare_proxy_username=settings.webshare_proxy_username,
            webshare_proxy_password=settings.webshare_proxy_password,
        )

    mcp = FastMCP("YouTube")

    @mcp.custom_route("/health", methods=["GET"])
    async def health_route(_request: Request) -> JSONResponse:
        """Lightweight liveness endpoint for the Docker HEALTHCHECK.

        A bare GET against ``/mcp`` makes the MCP SDK mint a transport
        session before it returns 400/405/406, and nothing reaps it, leaking
        ~40 KB per probe at the standard 30s interval. This route never
        touches the ``/mcp`` transport, so gate the HEALTHCHECK on the 200
        status code here, not the body.
        """
        return JSONResponse({"status": "ok"})

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
    async def fetch_video_metadata(url_or_id: str) -> str:
        """Fetch a YouTube video's title and channel.

        Uses YouTube's public oEmbed endpoint rather than the transcript
        scraper, so it carries a light one second interval instead of
        ``fetch_transcript``'s five to ten. It is the same source IP, and
        YouTube blocks per IP, so it is not free to hammer.

        Intended to run before ``format_transcript_as_research``, which needs a
        title and channel to produce usable frontmatter and cannot look them up
        itself.

        **Check ``metadata_available`` before giving up.** When it is false,
        YouTube answered but declined to describe the video, which usually means
        embedding is disabled. The transcript is normally still fetchable, so
        continue to ``fetch_transcript`` and pass an empty title through.

        Args:
            url_or_id: A YouTube URL (``https://youtu.be/<id>``,
                ``https://www.youtube.com/watch?v=<id>``, ``/shorts/<id>``,
                ``/embed/<id>``) or a bare 11-character video ID.

        Returns:
            Success: ``{"data": {"video_id": str, "title": str, "channel": str,
                "url": str, "thumbnail_url": str, "metadata_available": bool,
                "reason": str}}``. ``metadata_available`` is false, with ``title``
            and ``channel`` empty, when embedding is disabled or oEmbed returned
            no title; the video is usually still readable via ``fetch_transcript``.
            Failure: ``{"error", "code", "details"}`` with code in
            ``{INVALID_INPUT, NOT_FOUND, RATE_LIMITED, UPSTREAM_DOWN, INTERNAL}``.
            ``NOT_FOUND`` means no such video.

        Example:
            ``fetch_video_metadata("https://www.youtube.com/watch?v=dQw4w9WgXcQ")``
        """
        video_id = parse_video_id(url_or_id)
        if not video_id:
            return _err(
                f"could not parse a YouTube video ID from {url_or_id!r}",
                "INVALID_INPUT",
                input=url_or_id,
            )

        try:
            meta = await metadata_client.fetch(video_id)
        except TranscriptError as exc:
            logger.warning(
                "fetch_video_metadata failed",
                extra={"video_id": video_id, "code": exc.code, "reason": str(exc)},
            )
            return _err(str(exc), exc.code, video_id=video_id)
        except Exception as exc:
            logger.exception("fetch_video_metadata unexpected", extra={"video_id": video_id})
            return _err(f"unexpected error: {exc}", "INTERNAL", video_id=video_id)

        return _ok(
            {
                "video_id": meta.video_id,
                "title": meta.title,
                "channel": meta.channel,
                "url": meta.url,
                "thumbnail_url": meta.thumbnail_url,
                "metadata_available": meta.available,
                "reason": meta.reason,
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the CLI flags. Everything here also has an env var equivalent."""
    parser = argparse.ArgumentParser(
        prog="mcp-youtube",
        description=(
            "MCP server that fetches YouTube transcripts and shapes them as "
            "research-ready markdown."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=None,
        help=(
            "stdio (default): the MCP client spawns this process and talks over "
            "stdin/stdout. http: serve Streamable HTTP on MCP_HOST:MCP_PORT. "
            "Overrides the MCP_TRANSPORT env var."
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address for --transport http. Overrides MCP_HOST (default 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=_port_number,
        default=None,
        help="Port for --transport http. Overrides MCP_PORT (default 3716).",
    )
    parser.add_argument("--version", action="version", version=f"mcp-youtube {__version__}")
    return parser.parse_args(argv)


def _port_number(value: str) -> int:
    """argparse type for --port, matching the ge=1/le=65535 on the settings field."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"port must be a whole number, got {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between 1 and 65535, got {port}")
    return port


#: Prefixes and names this server actually reads. A project .env is only worth
#: mentioning if it sets one of these; most .env files are about something else
#: entirely, and warning about those would make the stdio path noisy for everyone.
_RECOGNISED_ENV = (
    "MCP_",
    "LOG_",
    "RATE_LIMIT_",
    "WEBSHARE_",
    "DEFAULT_LANGUAGE",
    "FALLBACK_LANGUAGES",
)


def _recognised_dotenv_keys(path: Path) -> list[str]:
    """Return the settings keys in ``path`` that this server would have honoured.

    Best effort and never fatal: an unreadable or malformed .env just means we
    stay quiet, because this only drives a warning message.
    """
    try:
        # utf-8-sig, because a BOM would otherwise become part of the first key.
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        # python-dotenv's binding is `(?:export\s+)?`, so any whitespace counts,
        # not just a space.
        parts = key.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "export":
            key = parts[1]
        key = key.upper()
        if key.startswith(_RECOGNISED_ENV) and key not in found:
            found.append(key)
    return found


def _resolve_transport(args: argparse.Namespace) -> str:
    """Decide the transport WITHOUT reading a dotenv file.

    This has to run before ``Settings`` is built, because the answer determines
    whether reading a ``.env`` is safe at all. Precedence is flag, then real
    environment, then the stdio default.

    Accepts what ``Settings`` would reject (``HTTP``, ``" http "``, and the
    empty string a bare ``MCP_TRANSPORT=`` line or a compose ``MCP_TRANSPORT: ""``
    produces) so the caller can canonicalise it. Anything that is not a
    near-miss gets a one-line message rather than a pydantic traceback.
    """
    if args.transport:
        return str(args.transport)
    raw = os.environ.get("MCP_TRANSPORT", "")
    normalized = raw.strip().lower()
    if not normalized:
        return "stdio"
    if normalized not in ("stdio", "http"):
        raise SystemExit(f"MCP_TRANSPORT must be 'stdio' or 'http', got {raw!r}")
    return normalized


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for both transports.

    Defaults to stdio so that ``uvx mcp-youtube`` (or any MCP client spawning
    this command) just works with nothing to install and no port to keep
    listening. The Docker image sets ``MCP_TRANSPORT=http`` to get the
    long-lived Streamable HTTP server instead.
    """
    args = _parse_args(argv)
    transport = _resolve_transport(args)

    # Under stdio the MCP client picks the working directory, and it is the
    # user's project, not this repo. Reading whatever .env happens to be there
    # would let an unrelated file crash us (LOG_LEVEL=debug is not a valid
    # level here) or silently turn us into an HTTP server the client can never
    # talk to. So stdio reads real environment variables only.
    # Overrides go through pydantic rather than being assigned onto the model,
    # because validate_assignment is off: a direct `settings.mcp_port = 70000`
    # would stick and then fail deep inside uvicorn instead of here.
    #
    # mcp_transport is passed the same way to canonicalise it. _resolve_transport
    # deliberately accepts near-misses the strict Literal on the field would
    # reject ("HTTP", " http ", ""), and an init kwarg outranks the environment,
    # so this keeps the process off a pydantic traceback below without writing
    # anything back into os.environ, which would leak into later main() calls,
    # the rest of a test session, and every child process.
    overrides: dict[str, object] = {"mcp_transport": transport}
    if args.host is not None:
        overrides["mcp_host"] = args.host
    if args.port is not None:
        overrides["mcp_port"] = args.port

    try:
        settings = load_settings(
            env_file=None if transport == "stdio" else ".env",
            **overrides,
        )
    except (ValidationError, SettingsError, OSError) as exc:
        raise SystemExit(f"invalid configuration:\n{exc}") from exc

    # Under stdio a human is usually watching the terminal, so plain text beats
    # JSON lines. An explicitly configured LOG_FORMAT still wins; model_fields_set
    # is what reports that, since a dotenv value never reaches os.environ.
    fmt = settings.log_format
    if transport == "stdio" and "log_format" not in settings.model_fields_set:
        fmt = "text"
    configure_logging(level=settings.log_level, fmt=fmt)

    if transport == "stdio":
        ignored = _recognised_dotenv_keys(Path(".env"))
        if ignored:
            logger.warning(
                "ignoring %s in %s: the stdio transport reads real environment "
                "variables only. Set them in your MCP client's env block.",
                ", ".join(ignored),
                Path.cwd(),
            )
        if args.host is not None or args.port is not None:
            logger.warning("--host and --port do nothing under the stdio transport")

    logger.info(
        "MCP YouTube starting",
        # log_format from safe_repr() is the configured value, which is not the
        # one in force when stdio defaults it to text.
        extra={"version": __version__, "config": {**settings.safe_repr(), "log_format": fmt}},
    )
    server = build_server(settings)

    if transport == "stdio":
        # No host/port: the client owns the process lifetime. The banner is
        # suppressed because FastMCP has written it to stdout in some versions,
        # and on this transport stdout is the wire.
        server.run(transport="stdio", show_banner=False)
        return

    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )


if __name__ == "__main__":
    main()
