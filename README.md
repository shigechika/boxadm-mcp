<!-- mcp-name: io.github.shigechika/boxadm-mcp -->

# boxadm-mcp

English | [日本語](README.ja.md)

MCP (Model Context Protocol) server that surfaces **external file flow** from
a Box admin's point of view. It reads Box's enterprise event log
(`admin_logs`) to highlight "who shares a lot with the outside" and "which
files get accessed from outside" — an early-warning signal for leakage, not a
general-purpose file browser.

**Read-only**: it never revokes shares, deletes files, or otherwise mutates
anything — it only surfaces risk. This is a different tool from a
general-purpose Box file MCP (the official Box MCP, or the claude.ai Box
connector): those operate on a user's own files and cannot see enterprise
events, which is exactly what this server is for.

Named after the admin-console viewpoint (`boxadm` = Box admin), sibling of
[`gwsadm-mcp`](https://github.com/shigechika/gwsadm-mcp).

## Features

| Tool | Category | Description |
|------|------|-------------|
| `health_check` | — | version + auth_mode + Box auth + `admin_logs` scope probe + configured domain allowlist. Reports `needs-login` when not yet authenticated (OAuth mode) |
| `recent_admin_events` | Diagnostic | Raw recent enterprise events (for checking event types/fields). Supports manual pagination via `stream_position` |
| `external_access_events` | Access (events, enterprise-wide) | Aggregates external DOWNLOAD/PREVIEW within a window: top external accessors, top externally-accessed files, share-link count. Pass `created_by_logins` for **DLP tracing** of a specific account |
| `external_collaborators` | Exposure (enumeration) | Lists external collaborators (outside-org login or external invite email) |
| `public_shared_links` | Exposure (enumeration) | Lists items shared with an `open` (anyone-with-the-link) share link |
| `top_external_sharers` | Exposure (enumeration) | Ranks internal owners by external exposure (external collabs + public links) |
| `daily_brief` | Combined | Morning summary combining access (events) and exposure (enumeration) |

## Auth model

Two modes, selected via `BOX_AUTH_MODE`:

- `oauth` — OAuth 2.0 (user auth). An admin authorizes once in a browser; the
  refresh token keeps it running unattended after that.
- `ccg` — Client Credentials Grant (server-to-server). Simpler to run
  unattended if your Box tenant has an available server-authentication app
  slot.

`admin_logs` (enterprise events) is readable in **either mode**, provided the
authorizing/impersonated user is an admin and the app has the **Manage
enterprise properties** scope.

### OAuth setup (one-time, by a Box admin)

1. Developer Console → Create Platform App → **Custom App → User
   Authentication (OAuth 2.0)**
2. **Redirect URI**: `http://localhost:8787/callback`
3. **Application Scopes**: check **Manage enterprise properties** (required
   for `admin_logs`). Add **Read all files and folders** too if you also want
   collaboration/share-link enumeration (requires re-consent)
4. Enable the app in the Admin Console (unpublished apps are disabled by
   default under most tenant policies)
5. Note the **Client ID / Client Secret**
6. First login: set `BOX_AUTH_MODE=oauth` etc., then run **`boxadm-mcp
   auth`** → authorize in the browser → a token cache is written to
   `~/.config/boxadm-mcp/token.json` (chmod 600)

## Setup

```bash
# uv
uv pip install boxadm-mcp

# pip
pip install boxadm-mcp
```

Or from source:

```bash
git clone https://github.com/shigechika/boxadm-mcp.git
cd boxadm-mcp

# uv
uv sync

# pip
pip install -e .
```

## Configuration

| Variable | Required | Description |
|---|---|---|
| `BOX_AUTH_MODE` | | `oauth` / `ccg` (default `ccg`) |
| `BOX_CLIENT_ID` | ✓ | App Client ID |
| `BOX_CLIENT_SECRET` | ✓ | App Client Secret |
| `BOX_ENTERPRISE_ID` | ccg mode | Enterprise ID (CCG subject; not needed for oauth) |
| `BOX_OAUTH_REDIRECT_URI` | | oauth redirect. Default `http://localhost:8787/callback` |
| `BOX_TOKEN_CACHE` | | oauth token cache path. Default `~/.config/boxadm-mcp/token.json` |
| `BOX_API_BASE` | | Default `https://api.box.com` |
| `BOX_SCAN_CONCURRENCY` | | Parallel per-folder lookups in the enumeration scan. Default `8`, clamped `1`–`32` |
| `BOX_ALLOWED_DOMAINS` | ✓ | Internal email domains (comma-separated). No default — every address counts as external until you set this |

Keep secrets out of `.mcp.json` (e.g. in a local env file sourced before
launch); `.mcp.json` itself can reference `${BOX_CLIENT_ID}`-style variables
and be safely committed.

### Scope and limits

- **Access tools** (`external_access_events`, and the access half of
  `daily_brief`) read the **enterprise-wide** events stream. Hitting
  `max_events` sets `capped: true` (oldest-first scan).
- **Exposure (enumeration) tools** only see folders visible to the
  co-admin account (not a guaranteed 100% of the enterprise), plus
  `max_folders`/`max_depth` limits (surfaced via `capped`). Requires the
  **Read all files and folders** scope.
- The scan fans its per-folder lookups out concurrently
  (`BOX_SCAN_CONCURRENCY`), since Box has no enterprise-wide collaboration
  listing — this widens how many folders finish inside a tool-call timeout,
  but coverage is still bounded by the caps. The read path retries `429`
  (honoring `Retry-After`) and transient `5xx` with jittered backoff, so a
  passing throttle recovers instead of degrading coverage; a folder dropped by
  a per-folder API error that outlasts those retries (e.g. a persistent `403`)
  is counted in `fetch_errors`: coverage is complete only when `capped` is
  false **and** `fetch_errors` is 0.
- Enumeration tools share a short-TTL scan memo across calls;
  `public_shared_links` skips collaboration calls entirely (optimization).

### DLP tracing (reverse-lookup by accessor)

To answer "what did this external account download": pass
`created_by_logins` (comma-separated logins) to `external_access_events`. It
keeps only that accessor's events and returns per-file detail
(`matched_events`: item id/name, owner, size in bytes+GB, timestamp,
event_type, whether it was via a share link).

```
external_access_events(since_hours=26, created_by_logins="someone@example.com")
```

- Since the accessor could appear anywhere in the window, a filtered call
  auto-extends the scan cap to **up to 50,000 events** (oldest-first) — but
  only matching events are kept, so memory stays bounded.
- In this mode the response carries `events_matched` (match count) instead of
  `events_scanned` (no running total is kept; use `capped` to judge coverage).
  `capped: true` means the window wasn't fully scanned — raise `max_events`.
- Box's `admin_logs` API has no `created_by` query parameter, so this is a
  client-side filter (`fetch_admin_events(created_by_logins=...)`).

## Usage

### Claude Code

Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "boxadm-mcp": {
      "type": "stdio",
      "command": "boxadm-mcp",
      "env": {
        "BOX_AUTH_MODE": "oauth",
        "BOX_CLIENT_ID": "${BOX_CLIENT_ID}",
        "BOX_CLIENT_SECRET": "${BOX_CLIENT_SECRET}",
        "BOX_ALLOWED_DOMAINS": "example.com"
      }
    }
  }
}
```

### CLI Options

```bash
boxadm-mcp auth       # OAuth first-time login (opens a browser)
boxadm-mcp --version  # Print version and exit
boxadm-mcp            # Start MCP server (STDIO, default)
```

## Development

```bash
git clone https://github.com/shigechika/boxadm-mcp.git
cd boxadm-mcp

# uv
uv sync --dev
uv run pytest -v
uv run ruff check .

# pip
python3 -m venv .venv
.venv/bin/pip install -e . && .venv/bin/pip install pytest respx ruff
.venv/bin/pytest -v
.venv/bin/ruff check .
```

Tests never touch Box — `respx` mocks the CCG/OAuth token endpoint and the
`admin_logs`/enumeration APIs.

## Releasing

Releases are automated with [release-please](https://github.com/googleapis/release-please).
Merging [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, …)
to `main` keeps a release PR open with the next version and changelog. Merging
that PR tags `vX.Y.Z` and publishes a GitHub Release, whose `release: published`
event triggers the `release` workflow to build and publish to PyPI and the MCP
Registry. release-please owns the version in `boxadm_mcp/__init__.py` and
`server.json` (do not bump them by hand).

> [!IMPORTANT]
> The release-please workflow should be given a repository secret
> `RELEASE_PLEASE_TOKEN` (a PAT with `contents: write` + `pull-requests: write`).
> The default `GITHUB_TOKEN` cannot create the Release that triggers the
> downstream `release` workflow (GitHub blocks workflow runs triggered by
> `GITHUB_TOKEN`), so without the PAT nothing gets published. The workflow falls
> back to `GITHUB_TOKEN` when the secret is unset so PR CI keeps working on forks.

## Governance

Because this surfaces what users share, run it as **authorized information-security
monitoring** with a clear purpose, a defined set of viewers, and a retention
policy. Most external sharing is legitimate (collaborators, vendors), so treat
findings as a **risk ranking**, not an alert queue — build an allowlist of
known-OK sharers over time.

## License

MIT
