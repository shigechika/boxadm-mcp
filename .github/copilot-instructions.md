# Repository overview

`boxadm-mcp` is an MCP (Model Context Protocol) server exposing Box
admin-log analytics (external file access, external sharing exposure) to AI
assistants over **stdio transport**. Built on the official `mcp` Python
SDK's `FastMCP` (`boxadm_mcp/server.py`), with `BoxClient`/`BoxOAuthClient`
(`boxadm_mcp/client.py`) wrapping the Box Enterprise API. Read-only: no tool
ever revokes a share, deletes a file, or otherwise mutates anything.

See `CLAUDE.md` for the authoritative command list and architecture notes —
read it before reviewing changes to `client.py`, `oauth.py`, or
`server.py`. In particular, read `CLAUDE.md`'s section on the Box
refresh-token rotation invariant before reviewing **any** change to
`client.py`'s `cache_lock()`, `_ensure_token()`, `_refresh()`, or
`_force_refresh()` — it explains why the locking is structured the way it
is, not just what it does.

# Build & validate

```bash
uv sync --dev
uv run pytest -v                    # all tests
uv run ruff check .                 # lint
uv run ruff format --check .        # format check
```

This mirrors `.github/workflows/ci.yml`: a `lint` job (`ruff check` +
`ruff format --check`) and a `test` job (`pytest -v`) on Python
3.10/3.12/3.13, **Linux only** — there is no Windows job in this repo
(unlike some sibling MCP repos in this family), because `client.py` imports
`fcntl` at module load and would fail on import before any test ran. Don't
suggest adding a Windows CI job without also addressing the `fcntl`
dependency.

# What to focus review on in this repo

## 1. Box's refresh token is single-use — a locking bug here is a real outage, not a style issue

Presenting an already-rotated Box refresh token revokes the **entire**
token chain (Box's reuse-detection treats it as compromise), forcing a
manual browser re-login via `boxadm-mcp auth`. `cache_lock()` in
`client.py` exists specifically to serialize the refresh path across
concurrent processes/sessions. When reviewing any change touching
`cache_lock()`, `_ensure_token()`, `_refresh()`, or `_force_refresh()`,
check specifically:
- Does every code path that calls `_refresh()` (persists a new
  refresh token) do so **while holding** `cache_lock()`?
- After acquiring the lock, does the code **re-read the on-disk cache**
  before deciding to refresh (another process may have already rotated
  while this one was waiting for the lock)? Skipping this re-check is the
  specific bug shape that would present a stale, already-rotated token.
- Is the lock acquired on the `<cache>.lock` sidecar path, not the cache
  file itself? (`write_token_cache` replaces the cache's inode via
  `os.replace` — a lock on that file would stop excluding anyone after the
  first write.)
A test change in this area needs to demonstrate the concurrent-refresh
scenario, not just the single-process happy path — see
`tests/test_oauth_client.py`'s existing lock/rotation tests as the bar.

## 2. This is a stdio MCP server — stdout is a JSON-RPC channel, not a log

Any `print()` or logging that writes to stdout (instead of stderr) corrupts
the protocol stream for the connected client. `oauth.py`'s `login()`
function does print to stdout, but only from the interactive `boxadm-mcp
auth` CLI path (`__main__.py`'s `_auth()`), never while `mcp.run()` is
active. Flag any new code path that could print to stdout while the stdio
server is running.

## 3. FastMCP already wraps tool returns — don't ask for manual envelope code

`server.py`'s `@mcp.tool()`-decorated functions return plain `dict` values;
FastMCP handles the MCP content-envelope wrapping itself. Do **not** suggest
a tool handler manually construct `{"content": [...], "isError": ...}`.

## 4. `capped` must be set whenever a scan or window is cut short

`_scan()` sets `capped=True` when `max_folders` is hit;
`external_access_events`/`daily_brief`'s event fetch reports `capped` when
`max_events` is hit. Every tool that surfaces enumeration or event-window
results propagates this flag so partial coverage is never presented as "no
findings". A new probe or tool that adds a page/count cap without wiring a
`capped` (or equivalent) flag through to the tool's return value is a
correctness bug, not a style nit.

## 5. `BOX_ALLOWED_DOMAINS` has no built-in default — verify new code doesn't reintroduce one

`config.py`'s `allowed_domains()` returns an empty list when
`BOX_ALLOWED_DOMAINS` is unset, so `is_external()` treats every address as
external until an operator configures it — deliberately fail-safe for a
leakage-detection tool (nothing is silently trusted as internal). Flag any
change that hardcodes a fallback domain, or that makes an ambiguous/missing
address classify as internal instead of external (see `is_external()`'s own
"blank/malformed = external" contract, and the code path in `_scan()` that
treats a collaboration with no `login` as *not* external — group
collaborations vs. missing individual data are not the same thing; verify a
change touching that distinction preserves it).

### Externally-owned folders are out of audit scope, but never silently dropped

`_scan()` skips folders whose `owned_by` login is a **known** external address
(the same `owner and "@" in owner and is_external(owner, doms)` guard used for
collaborators — an unknown/blank owner stays in scope, cautious toward
auditing). The rationale: this org is only a *guest* on an externally-owned
folder, cannot govern its collaborations, and the "external collaborators" on
it are just the owner's own org accounts — noise, not a leak of *our* content.
Skipped folders are reported under `skipped_externally_owned` (and counted in
`daily_brief`), so this is scoping, not silent truncation. Flag any change that
(a) skips on an *unknown* owner instead of a known-external one, (b) drops
skipped folders from the output, or (c) lets an externally-owned folder's
collaborations count as an external-sharing finding.

## 6. Secrets and adversarial tool inputs

- `BOX_CLIENT_ID` / `BOX_CLIENT_SECRET` are read from the environment
  (`server.py`'s `_client()`); the OAuth token cache holds a live access +
  refresh token pair. Flag any diff that logs or returns these, an
  `Authorization` header, or a raw token value in a tool response or error
  string — including at a hypothetical debug log level (this codebase has
  no logging module currently; don't introduce one that could leak these).
- Tool inputs (`root_folder_id`, `event_types`, `created_by_logins`, window
  sizes) come from an LLM acting on a user's behalf — treat them as
  adversarial. `external_access_events`' `created_by_logins` parsing
  (split on `,`, strip, drop empties) is the existing pattern for a
  delimited free-text input; a new similar parameter should handle
  empty/malformed input the same defensive way rather than passing it
  straight into an API call.
- A new `@mcp.tool()`'s name and docstring are what the calling model uses
  to decide whether/how to invoke it — flag a vague name or a docstring
  that omits a parameter format an LLM would otherwise have to guess (e.g.
  `root_folder_id`'s `"0"` = the user's root convention).

## 7. Test conventions

- HTTP-level mocking goes through `respx` (`tests/conftest.py`'s
  `make_router()`, `TOKEN_URL`, `EVENTS_URL`) — a new test that hand-mocks
  `httpx` instead is inconsistent with the existing suite.
- Tests call tools through the `_call()` helper (`getattr(tool, "fn",
  tool)`), not the decorated function directly — see `CLAUDE.md`.
- A new tool or probe needs a test covering both a normal response and a
  `capped=True` / `BoxError` / `BoxNotAuthenticatedError` path (the latter
  should surface as `needs-login` in the tool's error, matching the
  existing convention in `_access_or_error()`/`_connect()`) — a gap here is
  a real coverage gap for this codebase, not a nice-to-have.
- Any test touching `cache_lock`/`_refresh`/`_force_refresh` needs to
  exercise the concurrent/re-check behavior described in focus item #1, not
  just a single-process refresh.

# Out of scope for review comments

- `release-please.yml`'s use of `secrets.RELEASE_PLEASE_TOKEN` instead of
  `GITHUB_TOKEN` is intentional (a `GITHUB_TOKEN`-authored release doesn't
  trigger the downstream `release` workflow); it falls back to
  `GITHUB_TOKEN` when the secret is unset so PR CI still passes on forks —
  don't suggest reverting it.
- The wider-than-usual `line-length = 150` in `pyproject.toml`'s
  `[tool.ruff]` is intentional (see `CLAUDE.md`) — don't flag long lines
  that are within that limit.
- `gwsadm-mcp` (the sibling Google-Workspace-equivalent of this server) is a
  separate repository and out of scope here.
