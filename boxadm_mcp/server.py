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


def _cached_scan(client, root_folder_id: str, max_folders: int, max_depth: int, want_collabs: bool) -> dict:
    import time

    key = (root_folder_id, max_folders, max_depth, want_collabs)
    hit = _SCAN_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _SCAN_TTL:
        return hit[1]
    result = _scan(client, root_folder_id, max_folders, max_depth, want_collabs=want_collabs)
    _SCAN_CACHE[key] = (time.time(), result)
    return result


def _scan(client, root_folder_id: str, max_folders: int, max_depth: int, *, want_collabs: bool = True) -> dict:
    """BFS over folders the authenticating (co-admin) user can see, collecting
    public shared links and (when ``want_collabs``) external collaborations in one
    pass.

    Bounded by ``max_folders`` / ``max_depth``; sets ``capped`` when the folder
    cap is hit (so coverage is never silently partial). A ``visited`` set avoids
    re-fetching a folder reached twice. Per-folder API errors (e.g. 403/404) are
    tolerated and skipped. ``want_collabs=False`` skips the per-folder
    collaborations call entirely (e.g. for public_shared_links, which doesn't need
    it) — a real API-call saving at scale.
    """
    from collections import deque

    doms = allowed_domains()
    ext_collabs: list[dict] = []
    public_links: list[dict] = []
    visited: set[str] = set()
    seen = 0
    capped = False
    # queue items: (folder_id, name, owner_login, depth). Root "0" is synthetic.
    queue = deque([(root_folder_id, None, None, 0)])
    while queue:
        if seen >= max_folders:
            capped = True
            break
        fid, fname, fowner, depth = queue.popleft()
        if fid in visited:
            continue
        visited.add(fid)
        seen += 1

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
                        ext_collabs.append(
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
                pass

        # Items: capture public shared links (files AND subfolders, via this
        # listing) and enqueue subfolders for the next depth.
        if depth < max_depth:
            offset = 0
            while True:
                try:
                    resp = client.get_folder_items(fid, fields=["type", "id", "name", "owned_by", "shared_link"], limit=1000, offset=offset)
                except BoxError:
                    break
                entries = resp.get("entries", [])
                for it in entries:
                    owner = (it.get("owned_by") or {}).get("login")
                    sl = it.get("shared_link")
                    if sl and sl.get("access") in PUBLIC_ACCESS:
                        public_links.append(
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
                        queue.append((it.get("id"), it.get("name"), owner, depth + 1))
                total = resp.get("total_count")
                offset += len(entries)
                if not entries or (total is not None and offset >= total):
                    break

    return {"folders_scanned": seen, "capped": capped, "external_collaborations": ext_collabs, "public_shared_links": public_links}


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

    Coverage note: limited to content the co-admin user can access (not provably
    100% of the enterprise) and to the depth/folders caps. Returns
    ``folders_scanned``, ``capped``, ``count``, and ``external_collaborators``
    (folder, owner, collaborator, role, status, expires_at). On failure returns
    ``{"error": ...}``.
    """
    client, err = _connect()
    if err:
        return err
    scan = _cached_scan(client, root_folder_id, max_folders, max_depth, want_collabs=True)
    return {
        "folders_scanned": scan["folders_scanned"],
        "capped": scan["capped"],
        "count": len(scan["external_collaborations"]),
        "external_collaborators": scan["external_collaborations"],
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
    caps. Returns ``folders_scanned``, ``capped``, ``count``, and
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
    Returns ``folders_scanned``, ``capped``, and ``top_external_sharers`` (owner,
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
            "external_collaborations_count": len(scan["external_collaborations"]),
            "public_shared_links_count": len(scan["public_shared_links"]),
            "external_collaborations_sample": scan["external_collaborations"][:top],
            "public_shared_links_sample": scan["public_shared_links"][:top],
            "top_external_sharers": _rank_external_sharers(scan)[:top],
        },
    }
