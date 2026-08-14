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

- `boxadm_mcp/server.py` — FastMCP server with 8 tools: `health_check`,
  `recent_admin_events` (raw diagnostic), `external_access_events`
  (enterprise-wide DOWNLOAD/PREVIEW analytics, plus a `created_by_logins`
  DLP-tracing mode), `external_collaborators` / `public_shared_links` /
  `top_external_sharers` (enumeration over the co-admin's visible folders,
  BFS via `_scan()`), `get_user` (one account's current state by exact login —
  see below), and `daily_brief` (synthesis of both). `_SCAN_CACHE`
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
  persistent 403, or a sustained throttle) lands in `fetch_errors`. That
  per-`_get` budget does not bound a whole scan, so `_scan()` also carries a
  soft wall-clock deadline (`BOX_SCAN_DEADLINE`, default 45s; `0` disables),
  checked only between BFS levels (the in-flight batch always finishes) — when
  hit it sets `capped` and returns the disclosed partial rather than letting
  the tool call run to a gateway timeout that returns nothing. The per-request
  HTTP timeout is `BOX_HTTP_TIMEOUT` (default 30s). Neither is in the cache key.
  When `want_collabs=True`, folders Box flags with `is_externally_owned` are
  skipped (an outside party owns them, so their collaborations aren't the
  enterprise's to audit) and reported separately under
  `skipped_externally_owned`, never silently dropped — this is Box's
  authoritative signal, and an earlier owner-domain heuristic
  (`is_external(owner)`) was tried and reverted as a production regression (it
  collapsed ~190 audited folders to 9).
- `boxadm_mcp/client.py` — two read-only client classes sharing
  `_FolderReadMixin` (the shared authenticated GET plus the folder/collaboration
  getters and `get_users()`, so one implementation serves both auth modes):
  `BoxClient` (Client Credentials Grant, server-to-server)
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

### `get_user` — a search endpoint used as a lookup

`get_user(login)` is the only tool that answers about one named account (every
other one reads the event stream or walks folders, so an account with no recent
activity is invisible to them). It is backed by `GET /2.0/users?filter_term=`,
and the gap between what that endpoint does and what the tool promises is the
whole design:

- **`filter_term` is a prefix search over display name AND login**, so it
  returns zero, one or several accounts and a display-name hit is somebody
  else. The tool filters to an exact, case-insensitive `login` match; anything
  else is only counted, in `other_prefix_hits`. A term that is not email-shaped
  is refused before the request — identifying every prefix hit had made a
  one-character term return a page of the directory. Note the endpoint cannot
  find an alias at another domain: it prefix-matches the whole term. There is a `note` saying
  those are not the requested account. `found: false` is a first-class answer,
  never an `{"error": ...}` — a confident wrong account is worse than a clean
  not-found, and the smoke probe asserts exactly that path.
- **`capped`** follows the repo-wide rule: one page is requested
  (`_USER_SEARCH_LIMIT`) and truncation is disclosed, because a `found: false`
  computed over a truncated result set is inconclusive, not negative.
- **An empty login is refused before the request**, since Box reads an empty
  `filter_term` as "no filter" and would answer with a page of the directory.
- **`/2.0/users` reachability is not verified end-to-end** in either auth mode
  (under `oauth` the effective permission is the authorising user's), so no
  scope requirement is asserted anywhere; a 401/403 returns `likely_cause`
  naming the authorising user's role and the app's Application Scopes as the
  two things to check. Confirm it against a live tenant before writing a scope
  claim into the README.

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
  `make_router()`/`TOKEN_URL`/`EVENTS_URL`/`USERS_URL`) and call tools through a
  `_call()` helper (`getattr(tool, "fn", tool)`) rather than calling the
  `@mcp.tool()`-decorated function directly, so the suite keeps working
  regardless of whether the installed `mcp` version's tool decorator
  returns the plain function or a wrapper exposing it via `.fn`.
- `scripts/` holds the live smoke test: `smoke_test.py` (CLI), its per-tool
  specs in `smoke_probes.py`, and `smoke_harness.py` — the server-agnostic
  engine, kept identical across the servers that share it, so fix engine bugs
  once and sync the file rather than patching this copy (it is excluded from
  `ruff format` for that reason, and keeps the shared copies' own style;
  `ruff check` still applies to it). It runs every registered tool against a
  real enterprise (see README); `tests/test_smoke_probes.py` is the offline
  half and needs only the tool registry. Adding a tool without a probe spec
  fails CI: decide when you add the tool how anyone would know it works.
  Probes stay read-only, name no enterprise-specific value, and pass an
  explicit small value for every bounding parameter a tool offers.
