# Reference

## `health_check()`

Reports the running server version, `auth_mode` in effect, whether the
configured Box credentials can authenticate, a probe of the `admin_logs`
scope, and the configured `BOX_ALLOWED_DOMAINS` allowlist. In `oauth` mode,
before the first `boxadm-mcp auth` run it reports `needs-login` instead of
failing outright.

Safe to call at session start or after a tool-call timeout — it does not walk
folders or page the event stream.

## Tool index

| Tool | Category | Description |
|---|---|---|
| `health_check` | — | version + auth_mode + Box auth + `admin_logs` scope probe + configured domain allowlist. Reports `needs-login` when not yet authenticated (OAuth mode) |
| `recent_admin_events` | Diagnostic | Raw recent enterprise events (for checking event types/fields). Supports manual pagination via `stream_position` |
| `external_access_events` | Access (events, enterprise-wide) | Aggregates external DOWNLOAD/PREVIEW within a window: top external accessors, top externally-accessed files, share-link count. Pass `created_by_logins` for **DLP tracing** of a specific account |
| `external_collaborators` | Exposure (enumeration) | Lists external collaborators (outside-org login or external invite email) |
| `public_shared_links` | Exposure (enumeration) | Lists items shared with an `open` (anyone-with-the-link) share link |
| `top_external_sharers` | Exposure (enumeration) | Ranks internal owners by external exposure (external collabs + public links) |
| `list_folder_items` | One folder (`ls`) | Names, upload time, size, **who uploaded**, and a direct link per item. Filter by uploader or upload-time window. Reads no file content |
| `get_user` | Account state (lookup) | One account by its **exact login**: `status`, `role`, `enterprise`, quota, timestamps |
| `daily_brief` | Combined | Morning summary combining access (events) and exposure (enumeration) |

**No tool writes.** Every tool reads from Box's `admin_logs` or directory
APIs; nothing here revokes shares, deletes files, or otherwise mutates
anything.

## Scope and limits

- **Access tools** (`external_access_events`, and the access half of
  `daily_brief`) read the **enterprise-wide** events stream. Hitting
  `max_events` sets `capped: true` (oldest-first scan).
- **Exposure (enumeration) tools** only see folders visible to the co-admin
  account (not a guaranteed 100% of the enterprise), plus
  `max_folders`/`max_depth` limits (surfaced via `capped`). Requires the
  **Read all files and folders** scope.
- The scan fans its per-folder lookups out concurrently
  (`BOX_SCAN_CONCURRENCY`), since Box has no enterprise-wide collaboration
  listing. The read path retries `429` (honoring `Retry-After`) and
  transient `5xx` with jittered backoff; a folder dropped by a per-folder API
  error that outlasts those retries (e.g. a persistent `403`) is counted in
  `fetch_errors`. Coverage is complete only when `capped` is false **and**
  `fetch_errors` is 0.
- Enumeration tools share a short-TTL scan memo across calls;
  `public_shared_links` skips collaboration calls entirely (optimization).
- **`get_user`** reads the enterprise **user directory** instead — one
  request, no paging, and structurally not an enumerator. Its `capped` flag
  discloses a truncated search, so a `found: false` from a truncated result
  reads as inconclusive rather than negative.

## DLP tracing (reverse-lookup by accessor)

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
  `events_scanned`; use `capped` to judge coverage. `capped: true` means the
  window wasn't fully scanned — raise `max_events`.
- Box's `admin_logs` API has no `created_by` query parameter, so this is a
  client-side filter.

## `list_folder_items`

An `ls`, not a `cat`. File content is never read, and no shared link is
ever created — an existing one is reported because it is an exposure
finding, not a convenience.

**Who uploaded an item is not where you would look for it.** For an upload
made through a File Request, Box records no user at all: `created_by` and
`modified_by` both read *"Anonymous User"*, and `owned_by` is the
application's own service account. The only field carrying the submitter is
`uploader_display_name`, which in practice holds an email address — it is
matched as an **opaque string** (exact, case-insensitive), never parsed as an
address. For a file uploaded by a signed-in user the reverse holds, so
`created_by` is the fallback.

Ordering and time bounds are computed by the server rather than trusted to
Box: `sort=date` does not reliably match `created_at`/`modified_at` order in
practice, and `since`/`until` are compared as instants (both bounds must
carry a UTC offset; a bare date is refused rather than guessed).

`limit` bounds what is **returned**, not what is searched — a full page is
fetched first. `returned` vs `matched` reflects the caller's own `limit`;
`capped` means the folder holds more than one page, so a miss is
inconclusive rather than negative.

## `get_user`

Answers "is this account disabled, and is its quota full?" in one request:

```
get_user(login="someone@example.com")
```

`login` is matched **exactly and case-insensitively**. Box's underlying
`filter_term` is a prefix search over display name and login, so only an
exact match lands in `user`; everything else is counted in
`other_prefix_hits` and never identified. A term that is not email-shaped is
refused before the request is made.

| Field | Meaning |
|---|---|
| `found` | The only field that says whether the account exists. `false` is a normal answer, not an error |
| `user` | The account when `found`, else `null`: `status`, `role`, `enterprise`, `space_used` / `space_amount`, `created_at`, `modified_at` |
| `other_prefix_hits` | Count of further prefix matches. A count only: those are different accounts and are deliberately not identified |
| `capped` | The search was truncated, so `found: false` is inconclusive rather than negative |
| `search_hits`, `note` | How many entries came back, and a plain-language reading |

One drift it cannot find: the same person under a second login at another
domain. `filter_term` prefix-matches the whole term, so `alice@old.example`
can never return `alice@new.example`.

## CLI

```bash
boxadm-mcp auth       # OAuth first-time login (opens a browser)
boxadm-mcp --version  # Print version and exit
boxadm-mcp            # Start MCP server (STDIO, default)
```

No-argument mode is the normal one — that is how MCP clients launch it.
