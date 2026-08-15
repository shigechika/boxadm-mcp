# boxadm-mcp

MCP server that surfaces **external file flow** from a Box admin's point of
view. It reads Box's enterprise event log (`admin_logs`) to highlight "who
shares a lot with the outside" and "which files get accessed from outside" —
an early-warning signal for leakage, not a general-purpose file browser.

**Read-only**: it never revokes shares, deletes files, or otherwise mutates
anything — it only surfaces risk. This is a different tool from a
general-purpose Box file MCP (the official Box MCP, or the claude.ai Box
connector): those operate on a user's own files and cannot see enterprise
events, which is exactly what this server is for.

Named after the admin-console viewpoint (`boxadm` = Box admin), sibling of
[`gwsadm-mcp`](https://github.com/shigechika/gwsadm-mcp).

## Tools by area

| Area | Tool | Description |
|---|---|---|
| — | `health_check` | version + auth_mode + Box auth + `admin_logs` scope probe + configured domain allowlist |
| Diagnostic | `recent_admin_events` | Raw recent enterprise events, for checking event types/fields |
| Access (events, enterprise-wide) | `external_access_events` | Aggregates external DOWNLOAD/PREVIEW within a window; supports DLP tracing by accessor |
| Exposure (enumeration) | `external_collaborators` | Lists external collaborators |
| Exposure (enumeration) | `public_shared_links` | Lists items shared with an anyone-with-the-link share link |
| Exposure (enumeration) | `top_external_sharers` | Ranks internal owners by external exposure |
| One folder (`ls`) | `list_folder_items` | Names, upload time, size, uploader, and a direct link per item in one folder |
| Account state (lookup) | `get_user` | One account by its exact login: status, role, enterprise, quota, timestamps |
| Combined | `daily_brief` | Morning summary combining access (events) and exposure (enumeration) |

**No tool writes.** Every tool here only reads from Box's `admin_logs` and
directory APIs — there is nothing to gate behind a write permission.

## Design notes

**Two auth modes, one read surface.** `BOX_AUTH_MODE` selects between `ccg`
(Client Credentials Grant, server-to-server, the default) and `oauth`
(user auth via a one-time browser authorization). `admin_logs` is readable in
either mode, provided the authorizing/impersonated user is an admin and the
app has the **Manage enterprise properties** scope.

**A capped scan discloses that it is capped.** `BOX_SCAN_DEADLINE` bounds how
long an enumeration scan runs, and `BOX_SCAN_CONCURRENCY` bounds how many
per-folder lookups run at once. When a scan is cut short, the response sets
`capped: true` rather than silently returning what happened to be fetched. A
partial result that looks complete is worse than a slow one.

**Exact-match lookups stay exact.** `get_user` matches a login exactly and
case-insensitively; Box's underlying `filter_term` is a prefix search, so
the tool deliberately discards further prefix matches into a count-only
`other_prefix_hits` field rather than presenting a stranger's account as a
match.

## Next steps

- [Setup](setup.md) — auth setup, environment variables, MCP client registration
- [Reference](reference.md) — every tool, scope and limits, DLP tracing, `get_user`'s matching rules
