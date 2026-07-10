"""boxadm-mcp tools — Box admin-log analytics for external-sharing visibility.

Read-only. Tools:
- ``health_check`` (fleet standard) / ``recent_admin_events`` (raw diagnostic)
- ``external_access_events`` — enterprise-wide DOWNLOAD/PREVIEW analytics (events)
- ``external_collaborators`` / ``public_shared_links`` / ``top_external_sharers``
  — current-state enumeration over the co-admin's visible folders
- ``daily_brief`` — morning synthesis of access (events) + exposure (enumeration)
"""

import os
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from boxadm_mcp.client import BoxClient, BoxError, BoxNotAuthenticatedError, BoxOAuthClient, fetch_admin_events
from boxadm_mcp.config import allowed_domains, is_external

mcp = FastMCP("boxadm-mcp")

# admin_logs event types that represent content access (read paths).
ACCESS_EVENT_TYPES = ["DOWNLOAD", "PREVIEW"]

DEFAULT_API_BASE = "https://api.box.com"


def _auth_mode() -> str:
    return os.environ.get("BOX_AUTH_MODE", "ccg").lower()


# Cached client: a stdio server is long-lived and single-user, so we build and
# authenticate once, reusing the httpx pool and token across calls.
_CLIENT: BoxClient | BoxOAuthClient | None = None


def _client() -> BoxClient | BoxOAuthClient:
    global _CLIENT
    if _CLIENT is None:
        api_base = os.environ.get("BOX_API_BASE", DEFAULT_API_BASE)
        if _auth_mode() == "oauth":
            _CLIENT = BoxOAuthClient(
                os.environ["BOX_CLIENT_ID"],
                os.environ["BOX_CLIENT_SECRET"],
                token_cache=os.environ.get("BOX_TOKEN_CACHE") or None,
                api_base=api_base,
            )
        else:
            _CLIENT = BoxClient(
                os.environ["BOX_CLIENT_ID"],
                os.environ["BOX_CLIENT_SECRET"],
                os.environ["BOX_ENTERPRISE_ID"],
                api_base=api_base,
            )
    return _CLIENT


def reset_client() -> None:
    """Drop the cached client so the next call re-authenticates (token refresh)."""
    global _CLIENT
    _SCAN_CACHE.clear()
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        except Exception:
            pass
        _CLIENT = None


def _rfc3339_hours_ago(hours: int) -> str:
    """RFC3339 timestamp ``hours`` ago in UTC, as Box's created_after wants.

    Uses the explicit ``Z`` zone designator (unambiguous RFC3339) rather than a
    numeric offset, which some APIs render without the colon and reject.
    """
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


@mcp.tool()
def health_check() -> dict:
    """Report server version, Box connectivity/auth, and configuration.

    Call this at session start (or after a tool-call timeout) to confirm the MCP
    is up, see which version is running, verify the Box enterprise token can be
    obtained (CCG) and that the ``admin_logs`` event scope is actually granted,
    and view the org domain allowlist used for external detection. Lightweight:
    one token request plus a single-row events probe — it does not scan history.

    Always returns the same keys: ``status`` (healthy / degraded / error),
    ``service``, ``version``, ``auth_mode`` (ccg / oauth), ``box_api_base``,
    ``enterprise_id``, ``auth`` (ok / error / missing-env / needs-login),
    ``events_accessible`` (bool), and ``allowed_domains``. On a degraded or error
    result, ``detail`` carries the reason.
    """
    from boxadm_mcp import __version__

    result: dict = {
        "status": "healthy",
        "service": "boxadm-mcp",
        "version": __version__,
        "auth_mode": _auth_mode(),
        "box_api_base": os.environ.get("BOX_API_BASE", DEFAULT_API_BASE),
        "enterprise_id": os.environ.get("BOX_ENTERPRISE_ID", ""),
        "auth": "unknown",
        "events_accessible": False,
        "allowed_domains": allowed_domains(),
    }

    # Step 1: obtain a token (CCG: client creds + enterprise auth; OAuth: refresh
    # from the cached token written by `boxadm-mcp auth`).
    try:
        client = _client()
        client.authenticate()
        result["auth"] = "ok"
    except KeyError as e:
        result["status"] = "error"
        result["auth"] = "missing-env"
        result["detail"] = f"Missing environment variable: {e}"
        return result
    except BoxNotAuthenticatedError as e:
        reset_client()
        result["status"] = "degraded"
        result["auth"] = "needs-login"
        result["detail"] = str(e)
        return result
    except BoxError as e:
        reset_client()
        result["status"] = "degraded"
        result["auth"] = "error"
        result["detail"] = str(e)
        return result

    # Step 2: a 1-row admin_logs fetch confirms the enterprise events scope is
    # actually granted (a valid token alone does not prove the scope).
    try:
        client.get_admin_events(limit=1)
        result["events_accessible"] = True
    except BoxError as e:
        result["status"] = "degraded"
        result["detail"] = f"events not accessible (scope?): {e}"

    return result


@mcp.tool()
def recent_admin_events(event_types: str = "", since_hours: int = 24, limit: int = 100, stream_position: str = "") -> dict:
    """Fetch recent enterprise ``admin_logs`` events (raw passthrough).

    Diagnostic/starter tool: returns Box events verbatim so the real event types
    and field shapes can be confirmed before analytics tools are layered on. For
    external-sharing work the event types of interest are typically
    COLLABORATION_INVITE / COLLAB_ADD_COLLABORATOR, SHARED_LINK_CREATED /
    ITEM_SHARED_CREATE, and DOWNLOAD / PREVIEW.

    Args:
        event_types: Comma-separated Box event_type filter (empty = all types).
        since_hours: Look-back window in hours (default 24).
        limit: Max events to return in this page (default 100).
        stream_position: Continue a previous page by passing back the
            ``next_stream_position`` from the prior call (empty = first page).
            Box caps a single page at 500, so manual paging is needed to walk a
            busy window — or use ``external_access_events`` which pages for you.
    """
    try:
        client = _client()
    except KeyError as e:
        return {"error": f"Missing environment variable: {e}"}

    types = [t.strip() for t in event_types.split(",") if t.strip()] or None
    try:
        resp = client.get_admin_events(
            created_after=_rfc3339_hours_ago(since_hours),
            event_types=types,
            stream_position=stream_position or None,
            limit=limit,
        )
    except BoxError as e:
        reset_client()
        return {"error": str(e)}

    entries = resp.get("entries", [])
    return {
        "count": len(entries),
        "next_stream_position": resp.get("next_stream_position"),
        "events": entries,
    }


def _login_domain(login: str | None) -> str:
    if not login:
        return "anonymous"
    return login.rsplit("@", 1)[1].lower() if "@" in login else login


def _aggregate_access(events: list[dict], top: int) -> dict:
    """Aggregate DOWNLOAD/PREVIEW events into external-access metrics.

    Shared by external_access_events and daily_brief. Returns
    ``external_access_count``, ``via_shared_link`` (all link accesses),
    ``top_external_accessors`` and ``top_externally_accessed_files``.
    """
    doms = allowed_domains()
    via_link = 0
    external_count = 0
    accessors: dict[str, dict] = {}
    files: dict[str, dict] = {}
    for e in events:
        cb = e.get("created_by") or {}
        login = cb.get("login")
        ad = e.get("additional_details") or {}
        src = e.get("source") or {}
        ext = is_external(login, doms)
        if ad.get("shared_link_id"):
            via_link += 1
        iid = src.get("item_id") or "?"
        frec = files.setdefault(
            iid,
            {"item_id": iid, "name": src.get("item_name"), "owner": (src.get("owned_by") or {}).get("login"), "count": 0, "external_count": 0},
        )
        frec["count"] += 1
        if ext:
            external_count += 1
            frec["external_count"] += 1
            key = login or "anonymous (open link)"
            arec = accessors.setdefault(key, {"accessor": key, "domain": _login_domain(login), "count": 0, "bytes": 0})
            arec["count"] += 1
            arec["bytes"] += int(ad.get("size") or 0)
    return {
        "external_access_count": external_count,
        "via_shared_link": via_link,
        "top_external_accessors": sorted(accessors.values(), key=lambda x: -x["count"])[:top],
        "top_externally_accessed_files": sorted((f for f in files.values() if f["external_count"] > 0), key=lambda x: -x["external_count"])[:top],
    }


def _event_detail(e: dict) -> dict:
    """Per-event file detail for an actor-filtered access lookup.

    Turns one raw DOWNLOAD/PREVIEW event into the "who pulled which file, how big,
    when, via what" record that the aggregate (count/bytes only) can't express.
    """
    src = e.get("source") or {}
    ad = e.get("additional_details") or {}
    size = int(ad.get("size") or 0)
    return {
        "item_id": src.get("item_id"),
        "name": src.get("item_name"),
        "owner": (src.get("owned_by") or {}).get("login"),
        "size_bytes": size,
        "size_gb": round(size / 1e9, 2),
        "created_at": e.get("created_at"),
        "event_type": e.get("event_type"),
        "accessor": (e.get("created_by") or {}).get("login"),
        "via_shared_link": bool(ad.get("shared_link_id")),
    }


def _access_or_error(since_hours: int, max_events: int, created_by_logins: list[str] | None = None):
    """Connect + fetch DOWNLOAD/PREVIEW events for the window.

    Shared by external_access_events and daily_brief. Returns
    ``(client, events, capped, None)`` on success or ``(None, None, None, error)``
    where ``error`` is the tool's ``{"error": ...}`` dict (missing-env /
    needs-login / Box error). ``created_by_logins`` (when set) keeps only events
    from those accessors — see ``fetch_admin_events``.
    """
    client, err = _connect()
    if err:
        return None, None, None, err
    try:
        events, capped = fetch_admin_events(
            client,
            created_after=_rfc3339_hours_ago(since_hours),
            event_types=ACCESS_EVENT_TYPES,
            max_events=max_events,
            created_by_logins=created_by_logins,
        )
    except BoxNotAuthenticatedError as e:
        reset_client()
        return None, None, None, {"error": f"needs-login: {e}"}
    except BoxError as e:
        reset_client()
        return None, None, None, {"error": str(e)}
    return client, events, capped, None


@mcp.tool()
def external_access_events(since_hours: int = 24, max_events: int = 5000, top: int = 20, created_by_logins: str = "") -> dict:
    """Surface external file access (DOWNLOAD / PREVIEW) from enterprise admin_logs.

    Enterprise-wide (events stream): over the window, flags each access whose
    actor (``created_by.login``) is outside the org domain allowlist — an
    external party, or an anonymous open-link visitor (no login) — and whether it
    came via a shared link. Aggregates to the top externally-accessed files and
    the top external accessors, so an admin can spot unusual outbound data pulls.

    Args:
        since_hours: Look-back window in hours (default 24).
        max_events: Cap on DOWNLOAD/PREVIEW events scanned (default 5000); the
            result's ``capped`` flag is true when more existed (never silently
            truncated).
        top: How many top files / accessors to return (default 20).
        created_by_logins: Comma-separated accessor logins to trace (empty = all).
            When set, switches to DLP-tracing mode (see below).

    Returns ``window_hours``, ``events_scanned``, ``capped``,
    ``external_access_count``, ``via_shared_link``, ``top_external_accessors``
    (login + count + bytes), and ``top_externally_accessed_files`` (item id/name/
    owner + external-access count). On failure returns ``{"error": ...}`` (incl.
    ``needs-login`` for an expired OAuth session).

    Notes:
    - ``via_shared_link`` counts ALL scanned accesses that went through a shared
      link (internal and external), not just external ones.
    - Events are scanned oldest-first from the window start. When ``capped`` is
      true the aggregates reflect only the scanned (earliest) slice, NOT the full
      window — raise ``max_events`` for a complete picture.
    - **DLP tracing** (``created_by_logins`` set): scans up to the wider of
      ``max_events`` and 50000 events (the accessor may sit anywhere in the
      window) but keeps only that accessor's events, so the answer to "which
      files did this account pull" is exact and bounded. The result reports
      ``events_matched`` (not ``events_scanned`` — this mode doesn't track the
      scanned total; judge coverage by ``capped``), ``filtered_logins`` and
      ``matched_events`` (per access: item id/name, owner, size bytes+GB,
      created_at, event_type, accessor, via_shared_link); the aggregate is scoped
      to the filtered accessor(s). ``capped`` true means the window was not fully
      scanned (raise ``max_events``).
    """
    logins = [s.strip() for s in created_by_logins.split(",") if s.strip()] or None
    # An actor lookup must cover the whole window (the accessor may sit anywhere
    # in it), so widen the scan cap when filtering; only matches are kept, so
    # memory stays bounded regardless of how many events are scanned.
    scan_cap = max(max_events, 50000) if logins else max_events
    _, events, capped, err = _access_or_error(since_hours, scan_cap, created_by_logins=logins)
    if err:
        return err
    agg = _aggregate_access(events, top)
    if logins:
        # DLP-tracing mode: `events` is only the matched accessor's events, so
        # report a matched count under its own key (this mode doesn't track the
        # scanned total) — reusing `events_scanned` here would mislead a reader
        # judging window coverage. Use `capped` for that.
        return {
            "window_hours": since_hours,
            "events_matched": len(events),
            "capped": capped,
            "filtered_logins": logins,
            **agg,
            "matched_events": [_event_detail(e) for e in events],
        }
    return {"window_hours": since_hours, "events_scanned": len(events), "capped": capped, **agg}


# Shared-link access levels that expose content beyond explicit collaborators.
# "open" = anyone with the link (public); "company" = anyone in the enterprise.
PUBLIC_ACCESS = {"open"}


def _connect():
    """Build + authenticate the client, returning (client, None) or (None, error_dict)."""
    try:
        client = _client()
        client.authenticate()
        return client, None
    except KeyError as e:
        return None, {"error": f"Missing environment variable: {e}"}
    except BoxNotAuthenticatedError as e:
        reset_client()
        return None, {"error": f"needs-login: {e}"}
    except BoxError as e:
        reset_client()
        return None, {"error": str(e)}


# Short-lived memo of _scan results so the collab-based tools (external_collaborators
# / top_external_sharers, same want_collabs key) and back-to-back calls reuse a single
# folder traversal instead of re-walking. Cleared on reset_client().
_SCAN_CACHE: dict = {}
_SCAN_TTL = 60  # seconds

# Bounded worker count for _scan()'s per-folder collaboration/item fan-out.
_SCAN_CONCURRENCY_DEFAULT = 8


def _scan_concurrency() -> int:
    """Worker count for the per-folder collaboration/item fan-out in ``_scan()``.

    Box exposes no enterprise-wide "list every collaboration" API — the current
    state of collaborations is only readable per folder
    (``GET /folders/{id}/collaborations``), so a folder-by-folder fan-out is
    unavoidable. Running those lookups sequentially made the walk time out well
    before ``max_folders`` on wide enterprises; a small concurrent pool makes the
    wall-clock dominated by the slowest bucket instead of the sum of all calls.

    Overridable via ``BOX_SCAN_CONCURRENCY`` (clamped to 1..32). Modest by
    default: the scan is I/O-bound, and Box's per-user rate limits are generous
    but finite, so a handful of concurrent requests captures most of the win
    without provoking 429s. An unparseable value falls back to the default.
    """
    raw = os.environ.get("BOX_SCAN_CONCURRENCY")
    if not raw:
        return _SCAN_CONCURRENCY_DEFAULT
    try:
        return max(1, min(32, int(raw)))
    except ValueError:
        return _SCAN_CONCURRENCY_DEFAULT


def _cached_scan(client, root_folder_id: str, max_folders: int, max_depth: int, want_collabs: bool) -> dict:
    import time

    # Concurrency is deliberately NOT part of the key: it changes how fast the
    # traversal runs, never what it returns, so two callers with different pool
    # sizes still share one memoized result correctly.
    key = (root_folder_id, max_folders, max_depth, want_collabs)
    hit = _SCAN_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _SCAN_TTL:
        return hit[1]
    result = _scan(client, root_folder_id, max_folders, max_depth, want_collabs=want_collabs)
    _SCAN_CACHE[key] = (time.time(), result)
    return result


def _scan(client, root_folder_id: str, max_folders: int, max_depth: int, *, want_collabs: bool = True, concurrency: int | None = None) -> dict:
    """BFS over folders the authenticating (co-admin) user can see, collecting
    public shared links and (when ``want_collabs``) external collaborations in one
    pass.

    Bounded by ``max_folders`` / ``max_depth``; sets ``capped`` when the folder
    cap is hit (so coverage is never silently partial). A ``visited`` set avoids
    re-fetching a folder reached twice. ``want_collabs=False`` skips the
    per-folder collaborations call entirely (e.g. for public_shared_links, which
    doesn't need it) — a real API-call saving at scale.

    Each folder's per-folder work (its ``get_folder_collaborations`` call plus its
    ``get_folder_items`` paging) is independent, so a whole BFS frontier is
    fetched through a bounded ``ThreadPoolExecutor`` (``concurrency``, default
    ``_scan_concurrency()``): the walk drains one depth level at a time, up to the
    remaining folder budget, and processes that batch concurrently — so wall-clock
    is dominated by the slowest bucket rather than the sum of every call. Workers
    return only their own partial results and hold no shared state; results are
    merged (and subfolders enqueued) in input order, so the visited set,
    ``folders_scanned`` and output ordering are identical to a sequential BFS.

    Per-folder API errors (e.g. 403 on a folder the co-admin can list but not read
    collaborations for, or a transient 429) are tolerated and skipped, but counted
    — once per folder, so ``fetch_errors`` never exceeds ``folders_scanned`` — and
    surfaced by the tools, so a folder whose lookup failed is disclosed rather than
    silently under-reported (the same contract ``capped`` gives for the budget/
    window caps).
    """
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    doms = allowed_domains()
    workers = concurrency if concurrency is not None else _scan_concurrency()

    def _visit(entry: tuple) -> dict:
        """Fetch one folder's external collabs + public links + subfolders.

        Runs in a worker thread and returns only this folder's partial results
        (no shared mutable state), so the caller can merge them in deterministic
        BFS order. Mirrors the sequential body's two error boundaries: a failed
        collaborations call and a failed items page are each tolerated and counted.
        """
        fid, fname, fowner, depth = entry
        ext_c: list[dict] = []
        pub: list[dict] = []
        skip: list[dict] = []
        subs: list[tuple] = []
        # Per-FOLDER failure flag, not a call counter: a folder whose collaborations
        # AND items calls both fail still counts once, so the total never exceeds
        # folders_scanned (fetch_errors = "folders with a failed lookup").
        errors = 0

        # External collaborations on this folder (root "0" has none).
        if want_collabs and fid != "0":
            try:
                for c in client.get_folder_collaborations(fid).get("entries", []):
                    ab = c.get("accessible_by") or {}
                    who = ab.get("login") or c.get("invite_email")
                    # Only a real external email counts. A missing login means a
                    # group (or login-less entry) — NOT an external person, so skip
                    # (unlike anonymous *access*, where no login = external).
                    if who and "@" in who and is_external(who, doms):
                        ext_c.append(
                            {
                                "folder_id": fid,
                                "folder_name": fname,
                                "owner": fowner,
                                "collaborator": who,
                                "collaborator_type": ab.get("type") or ("invite" if c.get("invite_email") else None),
                                "role": c.get("role"),
                                "status": c.get("status"),
                                "expires_at": c.get("expires_at"),
                            }
                        )
            except BoxError:
                errors = 1

        # Items: capture public shared links (files AND subfolders, via this
        # listing) and collect subfolders for the next depth.
        if depth < max_depth:
            offset = 0
            while True:
                try:
                    resp = client.get_folder_items(
                        fid,
                        fields=["type", "id", "name", "owned_by", "shared_link", "is_externally_owned"],
                        limit=1000,
                        offset=offset,
                    )
                except BoxError:
                    errors = 1
                    break
                entries = resp.get("entries", [])
                for it in entries:
                    owner = (it.get("owned_by") or {}).get("login")
                    sl = it.get("shared_link")
                    if sl and sl.get("access") in PUBLIC_ACCESS:
                        pub.append(
                            {
                                "item_type": it.get("type"),
                                "item_id": it.get("id"),
                                "name": it.get("name"),
                                "owner": owner,
                                "access": sl.get("access"),
                                "can_download": (sl.get("permissions") or {}).get("can_download"),
                            }
                        )
                    if it.get("type") == "folder":
                        # Skip folders owned by a DIFFERENT Box enterprise (a
                        # vendor's folder this org is only a guest on): we don't own
                        # the content, can't govern its collaborations, and the
                        # "external collaborators" on them are just the owner's own
                        # org accounts — noise, not a leak of our data.
                        #
                        # `is_externally_owned` is Box's AUTHORITATIVE signal — true
                        # only when the owner is outside our enterprise, regardless
                        # of the owner's login domain. An earlier version used an
                        # owner-email-domain heuristic (is_external(owner)); that was
                        # wrong because this org's OWN folders are largely owned by
                        # Box Platform service accounts on `boxdevedition.com`, which
                        # the heuristic misread as external and over-skipped. The
                        # flag has no such false positive (those service accounts are
                        # in-enterprise → is_externally_owned=false). A missing/false
                        # flag stays in scope (cautious toward auditing).
                        #
                        # Scoped to the collaborator audit; public_shared_links
                        # (want_collabs=False) keeps its prior full traversal.
                        if want_collabs and it.get("is_externally_owned"):
                            skip.append({"folder_id": it.get("id"), "folder_name": it.get("name"), "owner": owner})
                            continue
                        subs.append((it.get("id"), it.get("name"), owner, depth + 1))
                total = resp.get("total_count")
                offset += len(entries)
                if not entries or (total is not None and offset >= total):
                    break

        return {"ext": ext_c, "pub": pub, "skip": skip, "subs": subs, "errors": errors}

    ext_collabs: list[dict] = []
    public_links: list[dict] = []
    skipped_external: list[dict] = []
    visited: set[str] = set()
    seen = 0
    capped = False
    fetch_errors = 0
    # queue items: (folder_id, name, owner_login, depth). Root "0" is synthetic.
    # BFS enqueues one depth level at a time, so the queue's contents at the top of
    # each iteration are exactly the current frontier — draining it (up to budget)
    # and fetching that batch concurrently is a level-synchronous parallel BFS.
    queue = deque([(root_folder_id, None, None, 0)])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while queue:
            if seen >= max_folders:
                capped = True
                break
            # Drain the current frontier into a batch, deduping via `visited` and
            # bounded by the remaining folder budget. A duplicate is popped without
            # consuming a batch slot or budget — matching the sequential body's
            # pop -> "if visited: continue" -> count.
            batch: list[tuple] = []
            while queue and seen + len(batch) < max_folders:
                entry = queue.popleft()
                if entry[0] in visited:
                    continue
                visited.add(entry[0])
                batch.append(entry)
            seen += len(batch)
            if not batch:
                break
            # ThreadPoolExecutor.map preserves input order, so merging results and
            # enqueuing discovered subfolders below reproduces a sequential BFS's
            # ordering exactly — only the I/O runs concurrently.
            for res in pool.map(_visit, batch):
                ext_collabs.extend(res["ext"])
                public_links.extend(res["pub"])
                skipped_external.extend(res["skip"])
                fetch_errors += res["errors"]
                for sub in res["subs"]:
                    queue.append(sub)

    return {
        "folders_scanned": seen,
        "capped": capped,
        "fetch_errors": fetch_errors,
        "external_collaborations": ext_collabs,
        "public_shared_links": public_links,
        "skipped_externally_owned": skipped_external,
    }


@mcp.tool()
def external_collaborators(root_folder_id: str = "0", max_folders: int = 150, max_depth: int = 1) -> dict:
    """List external collaborators on Box folders (current state, enumeration).

    Walks folders the authenticating co-admin user can see (default from the root
    "All Files") and reports collaborations whose collaborator is outside the org
    domain allowlist — accepted external users or pending external invites. Useful
    to review who outside the organization has standing access.

    Args:
        root_folder_id: Folder to start from ("0" = the user's root).
        max_folders: Cap on folders visited (default 150); ``capped`` discloses
            when coverage was cut short.
        max_depth: Folder recursion depth (default 1 = top-level folders only).

    Externally-owned folders (this org is only a guest, not the owner) are out
    of scope and skipped — we cannot govern their collaborations, and their
    "external collaborators" are just the owner's own org accounts. They are
    reported separately under ``skipped_externally_owned`` (never silently
    dropped) and do not consume the ``max_folders`` budget.

    Coverage note: limited to content the co-admin user can access (not provably
    100% of the enterprise) and to the depth/folders caps. Returns
    ``folders_scanned``, ``capped``, ``fetch_errors`` (count of folders whose
    lookup hit an API error, e.g. 403/429 — coverage is complete only when
    ``capped`` is false AND ``fetch_errors`` is 0), ``count``,
    ``external_collaborators`` (folder, owner, collaborator, role, status,
    expires_at), and ``skipped_externally_owned`` (folder_id, folder_name,
    owner). On failure returns ``{"error": ...}``.
    """
    client, err = _connect()
    if err:
        return err
    scan = _cached_scan(client, root_folder_id, max_folders, max_depth, want_collabs=True)
    return {
        "folders_scanned": scan["folders_scanned"],
        "capped": scan["capped"],
        "fetch_errors": scan["fetch_errors"],
        "count": len(scan["external_collaborations"]),
        "external_collaborators": scan["external_collaborations"],
        "skipped_externally_owned": scan["skipped_externally_owned"],
    }


@mcp.tool()
def public_shared_links(root_folder_id: str = "0", max_folders: int = 150, max_depth: int = 1) -> dict:
    """List items with an open ("anyone with the link") shared link (enumeration).

    Walks folders the authenticating co-admin user can see and reports files and
    folders whose shared link access is ``open`` — reachable by anyone with the
    URL, the highest-exposure sharing mode.

    Args:
        root_folder_id: Folder to start from ("0" = the user's root).
        max_folders: Cap on folders visited (default 150); ``capped`` discloses truncation.
        max_depth: Folder recursion depth (default 1 = top-level only; raise to reach file links inside folders).

    Coverage note: limited to content the co-admin user can access and to the
    caps. Returns ``folders_scanned``, ``capped``, ``fetch_errors`` (count of
    folders whose lookup hit an API error; coverage is complete only when
    ``capped`` is false AND ``fetch_errors`` is 0), ``count``, and
    ``public_shared_links`` (item type/id/name, owner, access, can_download). On
    failure returns ``{"error": ...}``.
    """
    client, err = _connect()
    if err:
        return err
    # public_shared_links doesn't need collaborations → skip those API calls.
    scan = _cached_scan(client, root_folder_id, max_folders, max_depth, want_collabs=False)
    return {
        "folders_scanned": scan["folders_scanned"],
        "capped": scan["capped"],
        "fetch_errors": scan["fetch_errors"],
        "count": len(scan["public_shared_links"]),
        "public_shared_links": scan["public_shared_links"],
    }


def _rank_external_sharers(scan: dict) -> list[dict]:
    """Rank internal owners by external exposure (external collabs + open links)."""
    owners: dict[str, dict] = {}
    for c in scan["external_collaborations"]:
        o = c.get("owner") or "unknown"
        owners.setdefault(o, {"owner": o, "external_collaborations": 0, "public_links": 0})["external_collaborations"] += 1
    for p in scan["public_shared_links"]:
        o = p.get("owner") or "unknown"
        owners.setdefault(o, {"owner": o, "external_collaborations": 0, "public_links": 0})["public_links"] += 1
    ranked = sorted(owners.values(), key=lambda x: -(x["external_collaborations"] + x["public_links"]))
    for r in ranked:
        r["total"] = r["external_collaborations"] + r["public_links"]
    return ranked


@mcp.tool()
def top_external_sharers(root_folder_id: str = "0", max_folders: int = 150, max_depth: int = 1, top: int = 20) -> dict:
    """Rank internal owners by their external exposure (enumeration).

    One traversal (same as external_collaborators / public_shared_links), then
    ranks internal file/folder owners by how much external exposure they hold:
    external collaborations + open shared links on content they own. Surfaces the
    people whose content is most exposed outside the organization.

    Args:
        root_folder_id / max_folders / max_depth: traversal bounds (see external_collaborators).
        top: How many owners to return (default 20).

    Coverage note: limited to the co-admin user's visible content and the caps.
    Returns ``folders_scanned``, ``capped``, ``fetch_errors`` (count of folders
    whose lookup hit an API error; coverage is complete only when ``capped`` is
    false AND ``fetch_errors`` is 0), and ``top_external_sharers`` (owner,
    external_collaborations, public_links, total). On failure ``{"error": ...}``.
    """
    client, err = _connect()
    if err:
        return err
    # Same want_collabs=True key as external_collaborators → shared traversal.
    scan = _cached_scan(client, root_folder_id, max_folders, max_depth, want_collabs=True)
    return {
        "folders_scanned": scan["folders_scanned"],
        "capped": scan["capped"],
        "fetch_errors": scan["fetch_errors"],
        "top_external_sharers": _rank_external_sharers(scan)[:top],
    }


@mcp.tool()
def daily_brief(since_hours: int = 24, max_events: int = 5000, max_folders: int = 150, max_depth: int = 1, top: int = 5) -> dict:
    """Morning DLP brief: external access (events) + external-sharing state (enumeration).

    One call that combines:
    - **access** (enterprise-wide, events): external DOWNLOAD/PREVIEW in the last
      ``since_hours``, with top external accessors and top externally-accessed files.
    - **exposure** (co-admin visible folders, enumeration): current external
      collaborations, open ("anyone with the link") shared links, and the owners
      most externally exposed.

    Reuses the cached folder scan, so calling this alongside the other enumeration
    tools doesn't re-walk. Args mirror the underlying tools; ``top`` defaults to 5
    for a compact summary. Coverage/caps caveats are the same (``capped`` flags +
    enumeration limited to the co-admin's visible content). On failure returns
    ``{"error": ...}``.
    """
    client, events, ev_capped, err = _access_or_error(since_hours, max_events)
    if err:
        return err
    access = {"events_scanned": len(events), "capped": ev_capped, **_aggregate_access(events, top)}
    scan = _cached_scan(client, "0", max_folders, max_depth, want_collabs=True)
    return {
        "window_hours": since_hours,
        "access": access,
        "exposure": {
            "folders_scanned": scan["folders_scanned"],
            "capped": scan["capped"],
            "fetch_errors": scan["fetch_errors"],
            "external_collaborations_count": len(scan["external_collaborations"]),
            "public_shared_links_count": len(scan["public_shared_links"]),
            "external_collaborations_sample": scan["external_collaborations"][:top],
            "public_shared_links_sample": scan["public_shared_links"][:top],
            "top_external_sharers": _rank_external_sharers(scan)[:top],
            "skipped_externally_owned_count": len(scan["skipped_externally_owned"]),
        },
    }
