# Review rules for this repository

Review rules on top of the reviewer's default focus. Three things:
which findings are blocking here, which classes to report that the
default focus would otherwise skip, and which are noise. The reasoning
behind the rules lives in `.github/copilot-instructions.md` (its
numbered focus items are cited below) and `CLAUDE.md`, which the
reviewer also receives.

## Always blocking

- **A refresh-token rotation path that can present an already-rotated
  token (§1).** Box's reuse detection revokes the *entire* chain, so
  this is an outage requiring a manual browser re-login, not a
  robustness nit. Three shapes, any one of them: a `_refresh()` /
  `_force_refresh()` call reachable without holding `cache_lock()`; a
  path that decides to refresh without re-reading the on-disk cache
  *after* acquiring the lock; or a lock taken on the cache file itself
  rather than the `<cache>.lock` sidecar, which excludes nobody once
  `write_token_cache` replaces the inode via `os.replace`.
- **Flattening `_ensure_token()`'s local snapshot of `self._token` and
  its deadline (§4).** A concurrent `_on_401()` on another scan worker
  can null the field between the guard and the return, so re-reading it
  after the check sends `Bearer None`.
- **Presenting partial coverage as complete (§4).** A new cap, page
  limit or deadline that does not wire `capped` through to the tool's
  return value, a per-folder failure swallowed without counting it in
  `fetch_errors`, or either flag dropped from a tool result. Coverage
  is complete only when `capped` is false **and** `fetch_errors` is 0.
- **Breaking the level-synchronous BFS contract in `_scan()` (§4).**
  Concurrency must not change results: `folders_scanned`, the visited
  set and output ordering match a sequential walk. A change that
  reorders or races them is a regression even when the counts happen to
  agree.
- **Weakening the fail-safe external classification (§5).** A
  hardcoded fallback for `BOX_ALLOWED_DOMAINS`, an ambiguous or missing
  address classified as internal rather than external, or a
  collaboration with no `login` treated as external (a group
  collaboration and missing individual data are different things).
- **Regressing the externally-owned skip (§5).** Reintroducing an
  owner-email-domain heuristic in place of Box's `is_externally_owned`
  flag, dropping the `want_collabs` gate, skipping on a missing or
  false flag, dropping `skipped_externally_owned` from the output, or
  letting an externally-owned folder's collaborations count as an
  external-sharing finding. The domain heuristic was tried and cut a
  real production audit from ~190 folders to 9.
- **A secret reaching a log line, a tool response or an error string
  (§6).** `BOX_CLIENT_ID`, `BOX_CLIENT_SECRET`, an `Authorization`
  header, or a raw access or refresh token.
- **Anything reaching stdout from code that can run while the stdio
  server is serving (§2).** Stdout is the JSON-RPC channel there. This
  does **not** cover `oauth.py`'s `login()`, which prints to stdout by
  design and only from the interactive `boxadm-mcp auth` CLI path in
  `__main__.py`, never while `mcp.run()` is active.

## Report even though the default focus would not

- **A new `@mcp.tool()`'s name and docstring (§6).** The calling model
  decides whether and how to invoke a tool by reading them, so a vague
  name, or a docstring omitting a parameter format the model would
  otherwise guess (`root_folder_id`'s `"0"` = the user's root), is a
  functional defect here — report it even though docstring accuracy is
  normally out of scope when reviewing code.
- **A delimited free-text tool input parsed without the existing
  defensive shape (§6)**, as advisory. Inputs arrive from an LLM acting
  on a user's behalf; `external_access_events`' `created_by_logins`
  (split on `,`, strip, drop empties) is the pattern. Passing such a
  value straight into an API call is the finding.
- **A diff that changes `cache_lock` / `_refresh` / `_force_refresh`
  and also touches `tests/` without exercising the concurrent
  re-check** (§7), as advisory. Judge this from the diff only: a pull
  request that leaves `tests/` alone may well be covered by tests you
  were not given, and you receive changed files only.

## Never report

- A finding that does nothing but restate one of the two gates CI
  already enforces: `ruff check .` and `ruff format --check .` both gate
  this repository, and
  `tests/test_smoke_probes.py` already fails the build for a
  registered tool with no probe spec. This covers those two and
  nothing further. It never applies to a rule listed under **Always
  blocking** above, even when the same diff happens to fail a test as
  well, and it does not cover that same file's enterprise-specific-literal
  assertion — a leak reaching a public repository is worth
  catching twice.
- A long line that fits within `line-length = 150`. The wider limit in
  `pyproject.toml` is deliberate.
- Suggestions to hand-build an MCP content envelope
  (`{"content": [...], "isError": ...}`) inside a tool handler. FastMCP
  wraps returned values already.
- Suggestions to *replace* `release-please.yml`'s
  `secrets.RELEASE_PLEASE_TOKEN` with `GITHUB_TOKEN`. Preferring the
  dedicated token is deliberate, because a `GITHUB_TOKEN`-authored
  release does not trigger the downstream `release` workflow. (The line
  falls back to `GITHUB_TOKEN` when the secret is unset, so a finding
  about the fallback arm itself is still fair game.)
- Anything about the sibling Google-Workspace server. It is a separate
  repository and nothing here should be judged against it.
