# CLAUDE.md

## Overview

MCP (Model Context Protocol) server surfacing external file flow from a Box
admin's point of view — reads the Box enterprise event log (`admin_logs`)
to highlight who shares a lot with the outside and which files get accessed
from outside. Built on the official `mcp` Python SDK's `FastMCP`
(`boxadm_mcp/server.py`), over **stdio transport**. Read-only: no tool ever
revokes a share, deletes a file, or otherwise mutates anything.

## Commands

```bash
uv sync --dev
uv run pytest -v                    # run all tests
uv run ruff check .                 # lint
uv run ruff format --check .        # format check
```

This mirrors `.github/workflows/ci.yml`: a `lint` job (`ruff check` +
`ruff format --check`) and a `test` job (`pytest -v`) on Python
3.10/3.12/3.13, **Linux only** — no Windows job, because `client.py` imports
`fcntl` at module load (POSIX-only; see below), which would fail before any
test runs.

## Architecture

- `boxadm_mcp/server.py` — FastMCP server with 7 tools: `health_check`,
  `recent_admin_events` (raw diagnostic), `external_access_events`
  (enterprise-wide DOWNLOAD/PREVIEW analytics, plus a `created_by_logins`
  DLP-tracing mode), `external_collaborators` / `public_shared_links` /
  `top_external_sharers` (enumeration over the co-admin's visible folders,
  BFS via `_scan()`), and `daily_brief` (synthesis of both). `_SCAN_CACHE`
  memoizes `_scan()` results for 60s (`_SCAN_TTL`) keyed on
  `(root_folder_id, max_folders, max_depth, want_collabs)`, so
  `external_collaborators`/`top_external_sharers` (same `want_collabs=True`
  key) share one traversal instead of re-walking; cleared on
  `reset_client()`. `_scan()` fans the per-folder
  `get_folder_collaborations` + `get_folder_items` calls out across a bounded
  `ThreadPoolExecutor` (`BOX_SCAN_CONCURRENCY`, default 8, clamped 1..32),
  draining one BFS level at a time up to the folder budget — Box has no
  enterprise-wide "list all collaborations" API, so the per-folder fan-out is
  unavoidable and concurrency is the only lever. Concurrency is deliberately
  **not** in the cache key (it changes speed, never results), and
  `executor.map`'s order-preserving merge keeps the visited set,
  `folders_scanned`, and output ordering identical to a sequential walk.
  Per-folder API failures are tolerated but counted in `fetch_errors` and
  surfaced by every collab/exposure tool, so a folder dropped by an error is
  disclosed the same way `capped` discloses a budget cut — coverage is
  complete only when `capped` is false AND `fetch_errors` is 0. A 429
  (honoring `Retry-After`) or transient 5xx is retried with jittered backoff
  in `client.py`'s `_get` first (bounded by an attempt cap and a per-call
  wall-clock budget), so only a failure that outlasts those retries (e.g. a
  persistent 403, or a sustained throttle) lands in `fetch_errors`.
- `boxadm_mcp/client.py` — two read-only client classes sharing
  `_FolderReadMixin`: `BoxClient` (Client Credentials Grant, server-to-server)
  and `BoxOAuthClient` (OAuth 2.0 user auth with an auto-refreshed,
  cross-process-locked token cache — see below). Exception hierarchy:
  `BoxError` (base) → `BoxAuthError` → `BoxNotAuthenticatedError` (no usable
  cache; run `boxadm-mcp auth`). `server.py` callers only special-case
  `BoxNotAuthenticatedError` (surfaced as `needs-login`); a bare
  `BoxAuthError` falls through to the same `except BoxError` handling as
  any other Box API failure — it is not given its own `except BoxAuthError`
  clause anywhere in `server.py` today.
- `boxadm_mcp/config.py` — `allowed_domains()` reads `BOX_ALLOWED_DOMAINS`
  (comma-separated); **no organization-specific default** — an unset/empty
  value yields no domains, so `is_external()` treats every address as
  external until configured (fail-safe for a leakage-detection tool).
- `boxadm_mcp/oauth.py` — `login()`: the one-time interactive OAuth flow run
  via `boxadm-mcp auth`. Spins up a local `http.server` to catch the
  redirect, then writes the token cache through the same
  `cache_lock`/`write_token_cache` path `BoxOAuthClient` uses for refreshes.
- `boxadm_mcp/__main__.py` — CLI entry point (`--version`/`auth`) and the
  `mcp.run()` stdio server start.

### Box refresh-token rotation — the highest-stakes invariant in this codebase

Box's OAuth refresh tokens are **single-use**: presenting an already-rotated
token is treated as compromise and revokes the *entire* chain, forcing a
manual browser re-login (`boxadm-mcp auth`). `client.py`'s `cache_lock()`
(an `fcntl.flock` on a `<cache>.lock` sidecar — not the cache file itself,
because `write_token_cache` replaces the cache's inode via `os.replace`, and
a lock held on a replaced inode stops excluding anyone) makes
concurrent-refresh-safe by serializing the refresh path across processes,
with an unlocked fast-path read for the common case where another process
already refreshed. `_ensure_token()` and `_force_refresh()` both re-read the
cache *after* acquiring the lock before deciding to refresh, specifically to
avoid presenting a token another process already rotated. See the inline
comments on `_refresh()` / `_force_refresh()` for the residual failure mode
this locking cannot close (a lost HTTP response after Box already committed
a rotation).

## Conventions

- Python 3.10+, `requires-python = ">=3.10"`; `classifiers = ["Operating
  System :: POSIX"]` — this package does not run on Windows (`fcntl`).
- `ruff` lint rules: `E, F, I, W, UP`, line length **150** (wider than some
  sibling MCP repos in this family — this codebase predates a lint-enforced
  line-length convention and has long, information-dense lines,
  particularly in `server.py`'s tool docstrings and `_scan()`).
- Every enumeration/scan result carries a `capped` boolean (folder cap hit,
  or an events-window cap hit) so partial coverage is never mistaken for
  "nothing found" — see the coverage notes in each tool's docstring.
- Tests use `respx` for HTTP-level mocking (`tests/conftest.py`'s
  `make_router()`/`TOKEN_URL`/`EVENTS_URL`) and call tools through a
  `_call()` helper (`getattr(tool, "fn", tool)`) rather than calling the
  `@mcp.tool()`-decorated function directly, so the suite keeps working
  regardless of whether the installed `mcp` version's tool decorator
  returns the plain function or a wrapper exposing it via `.fn`.
