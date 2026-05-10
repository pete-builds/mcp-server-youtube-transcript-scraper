# mcp-server-youtube-transcript-scraper

A Model Context Protocol (MCP) server that fetches YouTube transcripts and
shapes them as research-ready Markdown.

> Repo: [`pete-builds/mcp-server-youtube-transcript-scraper`](https://github.com/pete-builds/mcp-server-youtube-transcript-scraper)
> Package / module: `mcp_youtube` (the local working name is kept short for
> the deployed container and Python package).

Built on [FastMCP](https://github.com/jlowin/fastmcp) with the
[youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api)
library underneath. Self-throttles aggressively so YouTube doesn't ban your IP.

## What it does

Two tools, single-user, stateless:

| Tool | What it does |
|------|--------------|
| `fetch_transcript(url_or_id, language?)` | Pulls captions for a YouTube video. Accepts URLs or bare 11-char IDs. Returns transcript text with `[MM:SS]` timestamps plus metadata (language, generated/manual, snippet count, duration). |
| `format_transcript_as_research(transcript, video_id, title?, channel?, url?, language?, is_generated?)` | Wraps a fetched transcript in a frontmatter block (title, date, source, summary, type, status) and returns the rendered Markdown plus a slug and suggested path. The MCP server does not write to disk — the calling agent persists the file in its own workspace. |

Both tools return JSON strings using a uniform contract:

- Success: `{"data": ...}`
- Failure: `{"error": "...", "code": "INVALID_INPUT" | "NOT_FOUND" | "RATE_LIMITED" | "UPSTREAM_DOWN" | "INTERNAL", "details": {...}}`

## Anti-ban hardening

YouTube doesn't expose a free transcript API; this server scrapes the same
internal endpoints a browser uses. From a single residential IP that means
the server can get IP-banned if it hammers YouTube. Defaults:

- **Self-throttle:** every `fetch_transcript` call sleeps a random
  `RATE_LIMIT_MIN_SECONDS`–`RATE_LIMIT_MAX_SECONDS` (default 5–10 s) since
  the previous call. Configurable per-deployment.
- **No retry on IP block:** if YouTube returns `RequestBlocked` or
  `IpBlocked`, the tool surfaces `RATE_LIMITED` and stops. Retrying would
  deepen the ban.
- **Webshare proxy slot reserved:** `WEBSHARE_PROXY_USERNAME` and
  `WEBSHARE_PROXY_PASSWORD` env vars are read at startup and logged. They
  are NOT wired into the fetch path in v0.1 — that lands in v0.2 if needed.

## Configuration

Copy `.env.example` to `.env` and edit. All settings are optional with
sensible defaults; see `src/mcp_youtube/config.py` for the full list.

| Env var | Default | Purpose |
|---------|---------|---------|
| `RATE_LIMIT_MIN_SECONDS` | `5` | Lower bound of random sleep between calls |
| `RATE_LIMIT_MAX_SECONDS` | `10` | Upper bound (with jitter) |
| `DEFAULT_LANGUAGE` | `en` | Primary preferred caption language |
| `FALLBACK_LANGUAGES` | `en-US,en-GB` | Comma-separated fallbacks |
| `WEBSHARE_PROXY_USERNAME` | _(unset)_ | Reserved (v0.2) |
| `WEBSHARE_PROXY_PASSWORD` | _(unset)_ | Reserved (v0.2) |
| `MCP_HOST` | `0.0.0.0` | Bind host inside the container |
| `MCP_PORT` | `3716` | TCP port |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `json` | `json` (production) or `text` (dev) |

## Run

### Clone

```bash
git clone https://github.com/pete-builds/mcp-server-youtube-transcript-scraper.git
cd mcp-server-youtube-transcript-scraper
```

### Docker (recommended)

```bash
cp .env.example .env
docker compose up -d --build
```

The server listens on `${MCP_PORT:-3716}` over Streamable HTTP at `/mcp`.

### Direct

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m mcp_youtube.server
```

## Register as an MCP client

```bash
# Claude Code (Streamable HTTP transport)
claude mcp add youtube --transport http --scope user --url http://localhost:3716/mcp
```

Then in any Claude session:

```
fetch_transcript("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
```

## Testing

```bash
pytest
```

The unit tests cover the formatters (slug, timestamp, video-ID parsing,
frontmatter rendering) without hitting YouTube.

## License

MIT. See `LICENSE`.
