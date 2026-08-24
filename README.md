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

## Install

Two ways to run this, and the first one is almost certainly the one you want.

### On your own machine (no Docker, nothing left running)

The server speaks [stdio](https://modelcontextprotocol.io/docs/concepts/transports)
by default, which means your MCP client starts it on demand and stops it when
it's done. There is no port to pick, no daemon to babysit, and no container.

With [uv](https://docs.astral.sh/uv/) installed, this is the whole install:

```bash
claude mcp add youtube -- \
  uvx --from git+https://github.com/pete-builds/mcp-server-youtube-transcript-scraper \
  mcp-youtube
```

That's it. `uvx` fetches the code, builds it in a throwaway environment, and
downloads a matching Python for you if you don't have one, so the
`requires-python = ">=3.13"` pin is not something you have to satisfy yourself.

For any other MCP client, the same thing as config:

```json
{
  "mcpServers": {
    "youtube": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/pete-builds/mcp-server-youtube-transcript-scraper",
        "mcp-youtube"
      ]
    }
  }
}
```

On Claude Desktop that file is `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`).

Prefer pip? Same result, one more step:

```bash
pip install git+https://github.com/pete-builds/mcp-server-youtube-transcript-scraper
claude mcp add youtube -- mcp-youtube
```

No `.env` is needed for any of this. The defaults are the local-friendly ones:
stdio transport, loopback bind, plain-text logs on stderr.

### As an always-on server (Docker)

Use this when you want one shared instance on a homelab box rather than a copy
per laptop. It serves Streamable HTTP at `/mcp` instead of stdio.

```bash
git clone https://github.com/pete-builds/mcp-server-youtube-transcript-scraper.git
cd mcp-server-youtube-transcript-scraper
cp .env.example .env
docker compose up -d --build
```

```bash
claude mcp add youtube --transport http --url http://localhost:3716/mcp
```

You can also get the HTTP server without Docker, if you want it under your own
process manager:

```bash
mcp-youtube --transport http        # or: MCP_TRANSPORT=http mcp-youtube
```

### Check it worked

```
> fetch_transcript("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
```

The first call takes 5-10 seconds on purpose. See
[Anti-ban hardening](#anti-ban-hardening).

## Transports

| | stdio | http |
|---|---|---|
| Who starts the process | your MCP client, on demand | you, and it stays up |
| Needs a port | no | yes (`MCP_PORT`, default 3716) |
| Good for | one person, one machine | a shared/homelab instance |
| Select with | default | `--transport http` or `MCP_TRANSPORT=http` |

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
  are NOT wired into the fetch path yet. Setting them changes nothing today.

## Configuration

Everything here is optional and the defaults are sane, so a local stdio
install needs no configuration at all. For the Docker deployment, copy
`.env.example` to `.env` and edit. Full list in `src/mcp_youtube/config.py`.

| Env var | Default | Purpose |
|---------|---------|---------|
| `RATE_LIMIT_MIN_SECONDS` | `5` | Lower bound of random sleep between calls |
| `RATE_LIMIT_MAX_SECONDS` | `10` | Upper bound (with jitter) |
| `DEFAULT_LANGUAGE` | `en` | Primary preferred caption language |
| `FALLBACK_LANGUAGES` | `en-US,en-GB` | Comma-separated fallbacks |
| `WEBSHARE_PROXY_USERNAME` | _(unset)_ | Reserved, not yet wired |
| `WEBSHARE_PROXY_PASSWORD` | _(unset)_ | Reserved, not yet wired |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http`. The Docker image sets `http` |
| `MCP_HOST` | `127.0.0.1` | Bind host, `http` only. The Docker image sets `0.0.0.0` |
| `MCP_PORT` | `3716` | TCP port, `http` only |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `text` under stdio, else `json` | `json` (production) or `text` (dev). Always written to stderr |

## Development

```bash
git clone https://github.com/pete-builds/mcp-server-youtube-transcript-scraper.git
cd mcp-server-youtube-transcript-scraper
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
mcp-youtube --help
```

## Testing

```bash
pytest
```

The unit tests cover the formatters (slug, timestamp, video-ID parsing,
frontmatter rendering) without hitting YouTube.

## License

MIT. See `LICENSE`.
