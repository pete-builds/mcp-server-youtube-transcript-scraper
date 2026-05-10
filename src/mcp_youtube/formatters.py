"""Pure-formatting helpers: video-id parsing, slug generation, [MM:SS] timestamps,
research-report frontmatter rendering.

No I/O here — these are easy to unit test and don't need rate limiting.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_id(url_or_id: str) -> str | None:
    """Extract an 11-character YouTube video ID from a URL or bare ID.

    Accepts:
        - bare 11-char IDs (``dQw4w9WgXcQ``)
        - https://www.youtube.com/watch?v=<id>
        - https://youtu.be/<id>
        - https://www.youtube.com/embed/<id>
        - https://www.youtube.com/shorts/<id>
        - https://m.youtube.com/watch?v=<id>

    Returns the ID or ``None`` if nothing matched.
    """
    s = (url_or_id or "").strip()
    if not s:
        return None
    if _VIDEO_ID_RE.match(s):
        return s

    try:
        parsed = urlparse(s)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        # https://youtu.be/<id>
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    if "youtube.com" not in host and "youtube-nocookie.com" not in host:
        return None

    # /watch?v=<id>
    qs = parse_qs(parsed.query)
    if qs.get("v"):
        candidate = qs["v"][0]
        if _VIDEO_ID_RE.match(candidate):
            return candidate

    # /embed/<id>, /shorts/<id>, /v/<id>, /live/<id>
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in {"embed", "shorts", "v", "live"}:
        candidate = parts[1]
        if _VIDEO_ID_RE.match(candidate):
            return candidate

    return None


def format_timestamp(seconds: float) -> str:
    """Format a seconds-offset as ``[MM:SS]`` (or ``[HH:MM:SS]`` past one hour)."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"[{hours:d}:{minutes:02d}:{secs:02d}]"
    return f"[{minutes:d}:{secs:02d}]"


def render_transcript_text(
    snippets: Iterable[dict],
    *,
    with_timestamps: bool = True,
    timestamp_every_n_snippets: int = 1,
) -> str:
    """Concatenate transcript snippets into a readable string.

    Args:
        snippets: iterable of dicts with at least ``text`` and ``start`` keys.
        with_timestamps: prepend ``[MM:SS]`` markers when True.
        timestamp_every_n_snippets: if > 1, only emit a timestamp every Nth
            snippet (the first snippet always gets one).

    Returns:
        The transcript as a single string. Snippet texts are separated by a
        single newline; YouTube's snippets are typically one short phrase each.
    """
    out: list[str] = []
    for idx, snip in enumerate(snippets):
        text = (snip.get("text") or "").strip()
        if not text:
            continue
        if with_timestamps and idx % max(1, timestamp_every_n_snippets) == 0:
            ts = format_timestamp(snip.get("start") or 0)
            out.append(f"{ts} {text}")
        else:
            out.append(text)
    return "\n".join(out)


def slugify(text: str, *, max_length: int = 80) -> str:
    """Return a lowercased, ASCII-only, dash-separated slug from text.

    Falls back to ``"untitled"`` if the input contains no usable characters.
    """
    if not text:
        return "untitled"
    # Strip accents and normalise to ASCII.
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii")
    # Lowercase, collapse non-alphanum to dashes.
    lowered = ascii_text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        return "untitled"
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug


def render_research_markdown(
    *,
    transcript: str,
    video_id: str,
    title: str = "",
    channel: str = "",
    url: str = "",
    language: str = "",
    is_generated: bool = False,
    today: str | None = None,
) -> dict:
    """Build a frontmatter-shaped markdown block for research-reports/.

    Args:
        transcript: the transcript body text (already formatted, with or
            without timestamps).
        video_id: the 11-char YouTube ID.
        title: video title; defaults to ``"YouTube transcript: <video_id>"``.
        channel: channel name (free-form).
        url: canonical YouTube URL; defaults to the watch URL for ``video_id``.
        language: human-readable language label (e.g. ``"English"``).
        is_generated: True if this is YouTube's auto-generated caption track.
        today: override the date stamp (YYYY-MM-DD). Defaults to today (UTC).

    Returns:
        A dict with three keys: ``slug``, ``suggested_path``,
        ``frontmatter_markdown``. The MCP server returns this directly to
        Claude, which writes the markdown into the workspace itself.
    """
    if not url:
        url = f"https://www.youtube.com/watch?v={video_id}"
    if not title:
        title = f"YouTube transcript: {video_id}"
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    slug = slugify(title)

    source_bits: list[str] = []
    if channel:
        source_bits.append(channel)
    source_bits.append(url)
    source_line = " — ".join(source_bits)

    caption_kind = "auto-generated captions" if is_generated else "captions"
    summary_lang = f" ({language})" if language else ""

    frontmatter = (
        "---\n"
        f'title: "{title}"\n'
        f"date: {today}\n"
        f'source: "{source_line}"\n'
        f"summary: \"Full transcript of '{title}' from YouTube{summary_lang}, "
        f'fetched via mcp-youtube ({caption_kind})."\n'
        "type: primary-source\n"
        "status: permanent — do not overwrite\n"
        "---\n\n"
        f"# {title}\n\n"
        f"*Source: {source_line}. Captions: {caption_kind}"
        f"{summary_lang}. Timestamps preserved from original audio.*\n\n"
        "---\n\n"
        f"{transcript}\n"
    )

    return {
        "slug": slug,
        "suggested_path": f"research-reports/{slug}.md",
        "frontmatter_markdown": frontmatter,
    }
