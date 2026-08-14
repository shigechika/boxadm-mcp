"""boxadm-mcp tools — Box admin-log analytics for external-sharing visibility.

Read-only. Tools:
- ``health_check`` (fleet standard) / ``recent_admin_events`` (raw diagnostic)
- ``external_access_events`` — enterprise-wide DOWNLOAD/PREVIEW analytics (events)
- ``external_collaborators`` / ``public_shared_links`` / ``top_external_sharers``
  — current-state enumeration over the co-admin's visible folders
- ``list_folder_items`` — one folder's contents (``ls``), with uploader attribution
- ``get_user`` — one account's current state, looked up by its exact login
- ``daily_brief`` — morning synthesis of access (events) + exposure (enumeration)
"""

import math
import os
import time
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from boxadm_mcp.client import (
    BoxClient,
    BoxError,
    BoxNotAuthenticatedError,
    BoxOAuthClient,
    BoxRequestError,
    _validate_resource_id,
    fetch_admin_events,
)
from boxadm_mcp.config import allowed_domains, is_external

mcp = FastMCP("boxadm-mcp")

# admin_logs event types that represent content access (read paths).
ACCESS_EVENT_TYPES = ["DOWNLOAD", "PREVIEW"]

DEFAULT_API_BASE = "https://api.box.com"


AUTH_MODE_CCG = "ccg"
AUTH_MODE_OAUTH = "oauth"


def _auth_mode() -> str:
    """Return the mode the server is actually running in, not the raw setting.

    ``_client()`` treats everything that is not ``oauth`` as CCG, so echoing the
    configured string let ``health_check`` report a mode the server was not in:
    ``BOX_AUTH_MODE=oauth2`` came back as ``oauth2`` while CCG was in use, and
    the reported value was outside the two the tool's description promises.

    An unrecognised value therefore reads as ``ccg`` — the same fallback
    ``_client()`` already applies, now visible in the report rather than hidden
    behind whatever was typed. An empty value is treated as unset.
    """
    configured = os.environ.get("BOX_AUTH_MODE", "").strip().lower()
    return AUTH_MODE_OAUTH if configured == AUTH_MODE_OAUTH else AUTH_MODE_CCG


# Cached client: a stdio server is long-lived and single-user, so we build and
# authenticate once, reusing the httpx pool and token across calls.
_CLIENT: BoxClient | BoxOAuthClient | None = None


def _client() -> BoxClient | BoxOAuthClient:
    global _CLIENT
    if _CLIENT is None:
        api_base = os.environ.get("BOX_API_BASE", DEFAULT_API_BASE)
        timeout = _http_timeout()
        # Same constant as _auth_mode(): the branch taken here and the mode
        # health_check reports must not be able to disagree.
        if _auth_mode() == AUTH_MODE_OAUTH:
            _CLIENT = BoxOAuthClient(
                os.environ["BOX_CLIENT_ID"],
                os.environ["BOX_CLIENT_SECRET"],
                token_cache=os.environ.get("BOX_TOKEN_CACHE") or None,
                api_base=api_base,
                timeout=timeout,
            )
        else:
            _CLIENT = BoxClient(
                os.environ["BOX_CLIENT_ID"],
                os.environ["BOX_CLIENT_SECRET"],
                os.environ["BOX_ENTERPRISE_ID"],
                api_base=api_base,
                timeout=timeout,
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
    ``service``, ``version``, ``auth_mode`` (ccg / oauth — the mode in effect,
    so an unrecognised ``BOX_AUTH_MODE`` reads as ``ccg``, which is what the
    server falls back to), ``box_api_base``,
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
_SCAN_CONCURRENCY_MIN = 1
_SCAN_CONCURRENCY_MAX = 32

# Soft wall-clock budget (seconds) for one _scan(). The per-folder retry budget in
# client.py bounds a single _get, but not the aggregate of a whole scan: under a
# sustained throttle many _gets over many BFS levels can sum past claude.ai's ~60s
# gateway timeout, which would kill the tool call and return nothing — losing the
# capped/fetch_errors "disclosed partial" contract. This deadline stops starting new
# BFS levels once elapsed >= budget and returns the partial with capped=True instead.
#
# The check sits between levels, so the *already-dispatched* batch always finishes:
# a scan is bounded to roughly deadline + one batch's duration. The default (45s) keeps
# the common case — an aggregate of many normal-latency calls — under a ~60s gateway.
# It does NOT by itself guarantee a hard sub-gateway bound when the final batch hits a
# *hung* endpoint (that batch can run a full _HTTP_TIMEOUT_DEFAULT, so ~45+30 > 60): a
# deployment that needs the hard bound lowers BOX_HTTP_TIMEOUT (and/or BOX_SCAN_DEADLINE)
# so deadline + one batch stays under its gateway.
_SCAN_DEADLINE_DEFAULT = 45.0

# Per-request httpx timeout (seconds). Kept at the historical default (a single Box
# folder listing can legitimately be slow, so lowering it globally risks false cut-offs);
# exposed via BOX_HTTP_TIMEOUT so a latency-sensitive deployment can lower it to keep a
# hung endpoint from stretching the final in-flight scan batch past its gateway timeout.
_HTTP_TIMEOUT_DEFAULT = 30


def _clamp_concurrency(n: int) -> int:
    return max(_SCAN_CONCURRENCY_MIN, min(_SCAN_CONCURRENCY_MAX, n))


def _scan_deadline(override: float | None = None) -> float:
    """Soft wall-clock budget in seconds for one ``_scan()``.

    Resolved from an explicit ``override`` (``_scan``'s ``deadline_seconds`` arg) when
    given, else ``BOX_SCAN_DEADLINE``, else ``_SCAN_DEADLINE_DEFAULT``. A value of 0 or
    negative (from either source) disables the deadline (returns ``inf``), for local use
    where there is no gateway timeout to beat. An unparseable or non-finite value
    (``nan``/``inf`` both parse as floats but are not a meaningful budget — from the env OR a
    computed override) falls back to the default rather than silently disabling. When the
    budget is hit the scan stops starting new BFS levels and returns the partial with
    ``capped=True`` — the same disclosure ``capped`` gives for the folder cap.
    """
    if override is not None:
        val = float(override)
    else:
        raw = os.environ.get("BOX_SCAN_DEADLINE")
        if not raw:
            return _SCAN_DEADLINE_DEFAULT
        try:
            val = float(raw)
        except ValueError:
            return _SCAN_DEADLINE_DEFAULT
    if not math.isfinite(val):  # "nan"/"inf" — not a usable budget; don't silently disable
        return _SCAN_DEADLINE_DEFAULT
    return val if val > 0 else float("inf")


def _http_timeout() -> float:
    """Per-request httpx timeout in seconds, from ``BOX_HTTP_TIMEOUT`` else the default.

    Parsed as a float so ``1.5`` works (mirroring ``BOX_SCAN_DEADLINE`` — the whole point of
    the knob is to *lower* the timeout, so a fractional value must not silently fall back to
    the larger default). A non-positive, non-finite, or unparseable value falls back to
    ``_HTTP_TIMEOUT_DEFAULT``. (httpx would take a literal ``0`` as a 0-second timeout — every
    request fails instantly — and ``None`` as "disabled"; this helper returns neither.)
    """
    raw = os.environ.get("BOX_HTTP_TIMEOUT")
    if not raw:
        return _HTTP_TIMEOUT_DEFAULT
    try:
        val = float(raw)
    except ValueError:
        return _HTTP_TIMEOUT_DEFAULT
    if not math.isfinite(val) or val <= 0:
        return _HTTP_TIMEOUT_DEFAULT
    return val


def _scan_concurrency(override: int | None = None) -> int:
    """Worker count for the per-folder collaboration/item fan-out in ``_scan()``.

    Box exposes no enterprise-wide "list every collaboration" API — the current
    state of collaborations is only readable per folder
    (``GET /folders/{id}/collaborations``), so a folder-by-folder fan-out is
    unavoidable. Running those lookups sequentially made the walk time out well
    before ``max_folders`` on wide enterprises; a small concurrent pool makes the
    wall-clock dominated by the slowest bucket instead of the sum of all calls.

    Resolved from an explicit ``override`` (``_scan``'s ``concurrency`` arg) when
    given, else ``BOX_SCAN_CONCURRENCY``, else the default. **Every** source is
    clamped to 1..32 — so ``ThreadPoolExecutor`` never gets a 0/negative
    ``max_workers`` (which raises) or an absurd thread count. Modest by default:
    the scan is I/O-bound, and Box's per-user rate limits are generous but finite,
    so a handful of concurrent requests captures most of the win without provoking
    429s. An unparseable env value falls back to the default.
    """
    if override is not None:
        return _clamp_concurrency(override)
    raw = os.environ.get("BOX_SCAN_CONCURRENCY")
    if not raw:
        return _SCAN_CONCURRENCY_DEFAULT
    try:
        return _clamp_concurrency(int(raw))
    except ValueError:
        return _SCAN_CONCURRENCY_DEFAULT


def _checked_root(root_folder_id: str) -> tuple[str, dict | None]:
    """Validate a CALLER-SUPPLIED root folder id up front: ``(id, None)`` or ``(id, error)``.

    The client refuses a malformed id too, but as a ``BoxError`` — which ``_scan``
    catches per folder and counts into ``fetch_errors``. Inside a walk that is the
    right behaviour; for the ROOT it is not, because the caller then gets a
    complete-looking result (``count: 0``, ``capped: false``) for a folder that was
    never queried, and reads "no findings" from a request that never left.

    So the root is checked here, where the answer can be an error shape instead of
    an empty one. Returning the STRIPPED id matters as well: ``_scan`` special-cases
    the literal ``"0"`` (the root has no collaborations to fetch) and ``_SCAN_CACHE``
    is keyed on it, so passing ``" 0 "`` through unnormalised would both defeat that
    skip and take a second cache entry for a tree already walked.
    """
    try:
        return _validate_resource_id(root_folder_id, kind="folder"), None
    except BoxRequestError as e:
        return "", {"error": str(e)}


def _cached_scan(client, root_folder_id: str, max_folders: int, max_depth: int, want_collabs: bool) -> dict:
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


def _scan(
    client,
    root_folder_id: str,
    max_folders: int,
    max_depth: int,
    *,
    want_collabs: bool = True,
    concurrency: int | None = None,
    deadline_seconds: float | None = None,
) -> dict:
    """BFS over folders the authenticating (co-admin) user can see, collecting
    public shared links and (when ``want_collabs``) external collaborations in one
    pass.

    Bounded by ``max_folders`` / ``max_depth`` and by a soft wall-clock deadline
    (``deadline_seconds``, else ``BOX_SCAN_DEADLINE``, else the default); sets
    ``capped`` when the folder cap is hit OR the deadline is reached before the
    walk finishes (so coverage is never silently partial). The deadline is checked
    only between BFS levels — the current concurrent batch always completes — so a
    scan is bounded to roughly the deadline plus one batch's duration, and a
    sustained throttle returns a disclosed partial instead of a gateway timeout
    with no result. A ``visited`` set avoids re-fetching a folder reached twice.
    ``want_collabs=False`` skips the per-folder collaborations call entirely (e.g.
    for public_shared_links, which doesn't need it) — a real API-call saving at
    scale.

    Each folder's per-folder work (its ``get_folder_collaborations`` call plus its
    ``get_folder_items`` paging) is independent, so a whole BFS frontier is
    fetched through a bounded ``ThreadPoolExecutor`` (``concurrency``, default
    ``_scan_concurrency()``): the walk drains one depth level at a time, up to the
    remaining folder budget, and processes that batch concurrently — so wall-clock
    is dominated by the slowest bucket rather than the sum of every call. Workers
    return only their own partial results and hold no shared state; results are
    merged (and subfolders enqueued) in input order, so the visited set,
    ``folders_scanned`` and output ordering are identical to a sequential BFS.

    Per-folder API errors are tolerated and skipped, but counted — once per folder,
    so ``fetch_errors`` never exceeds ``folders_scanned`` — and surfaced by the tools,
    so a folder whose lookup failed is disclosed rather than silently under-reported
    (the same contract ``capped`` gives for the budget/window caps). The client retries
    a 429 (honoring ``Retry-After``) or a transient 5xx with jittered backoff first, so
    a *passing* throttle recovers; only an error that outlasts those retries (e.g. a
    persistent 403 the co-admin can list but not read collaborations for, or a sustained
    throttle) lands in ``fetch_errors``.
    """
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    doms = allowed_domains()
    # Resolve + clamp in one place so an explicit `concurrency` (e.g. from a test)
    # is bounded to 1..32 exactly like BOX_SCAN_CONCURRENCY — a 0/negative value
    # would otherwise make ThreadPoolExecutor raise.
    workers = _scan_concurrency(concurrency)
    deadline = _scan_deadline(deadline_seconds)
    start = time.monotonic()

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
            # Soft wall-clock deadline (see _SCAN_DEADLINE_DEFAULT): checked between levels
            # so the in-flight batch always finishes, then break with capped set.
            if time.monotonic() - start >= deadline:
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
        root_folder_id: Folder to start from ("0" = the user's root). A Box
            folder id: decimal digits only, as shown at the end of a Box folder
            URL. Anything else is refused with ``{"error": ...}`` before any
            request is made, rather than being reported as an empty result.
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
    lookup hit an API error that outlasted the client's retries, e.g. a persistent
    403 or a sustained throttle — coverage is complete only when ``capped`` is false
    AND ``fetch_errors`` is 0), ``count``,
    ``external_collaborators`` (folder, owner, collaborator, role, status,
    expires_at), and ``skipped_externally_owned`` (folder_id, folder_name,
    owner). On failure returns ``{"error": ...}``.
    """
    root_folder_id, bad = _checked_root(root_folder_id)
    if bad:
        return bad
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
        root_folder_id: Folder to start from ("0" = the user's root). A Box
            folder id: decimal digits only, as shown at the end of a Box folder
            URL. Anything else is refused with ``{"error": ...}`` before any
            request is made, rather than being reported as an empty result.
        max_folders: Cap on folders visited (default 150); ``capped`` discloses truncation.
        max_depth: Folder recursion depth (default 1 = top-level only; raise to reach file links inside folders).

    Coverage note: limited to content the co-admin user can access and to the
    caps. Returns ``folders_scanned``, ``capped``, ``fetch_errors`` (count of
    folders whose lookup hit an API error; coverage is complete only when
    ``capped`` is false AND ``fetch_errors`` is 0), ``count``, and
    ``public_shared_links`` (item type/id/name, owner, access, can_download). On
    failure returns ``{"error": ...}``.
    """
    root_folder_id, bad = _checked_root(root_folder_id)
    if bad:
        return bad
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
    root_folder_id, bad = _checked_root(root_folder_id)
    if bad:
        return bad
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


# Fields asked for on the per-user lookup. Box answers ``GET /users`` with a minimal
# user object (id / type / name / login) unless asked for more, and everything that
# answers "why can this person not use Box" — status, role, quota — is in the "more".
# All of them ride on the same GET, so the explicit list costs no extra call.
USER_LOOKUP_FIELDS = [
    "id",
    "name",
    "login",
    "status",
    "role",
    "enterprise",
    "is_platform_access_only",
    "created_at",
    "modified_at",
    "space_used",
    "space_amount",
]

# One page is asked for and truncation is disclosed, rather than paging: a login
# lookup that needs paging has stopped being a lookup. A login is unique, and Box
# prefix-matches it, so the realistic hit count is a handful — but ``capped`` still
# says so when it is not, because "no exact match in a truncated page" is a weaker
# statement than "no such account", and only one of them is safe to act on.
_USER_SEARCH_LIMIT = 100


_NOT_FOUND_NOTE = (
    "No account carries this exact login. The login may not exist, may be spelled differently, or may be "
    "outside what this app's permissions can see. other_prefix_hits counts further matches on the same prefix; "
    "those are different accounts and are deliberately not identified."
)
_NOT_FOUND_CAPPED_NOTE = (
    "INCONCLUSIVE, not a negative answer: the search was truncated (capped), so an exact match may exist beyond "
    "the returned page. Do not report this as 'no such account'."
)
_FOUND_NOTE = "Exact, case-insensitive match on login."


def _login_is(entry: dict, wanted_lower: str) -> bool:
    """True when this search hit's login IS the requested one (exact, case-insensitive).

    The whole point of the tool: Box's prefix search returns accounts that merely start
    with the term, so membership in the result set is not identity. Case is folded
    because a login is an email address and Box echoes it as stored, which need not be
    the casing the caller typed.
    """
    return (entry.get("login") or "").strip().lower() == wanted_lower


def _is_email_shaped(term: str) -> bool:
    """True when ``term`` looks like a login, i.e. has a non-empty local part and domain.

    Not validation -- Box owns that. This only separates "a login" from "a fragment",
    because ``filter_term`` prefix-matches display name AND login with no minimum
    length, so a fragment is a directory listing rather than a lookup.
    """
    local, sep, domain = (term or "").strip().partition("@")
    return bool(local and sep and domain)


def _user_lookup_hint(message: str) -> str | None:
    """Actionable cause for a permission failure on the user lookup, else None.

    ``/2.0/users`` is not verified end-to-end in either auth mode (see ``get_user``),
    so a bare "HTTP 403" leaves an operator with nothing to act on. The two things
    worth checking are named instead — deliberately without asserting which scope is
    required, because that has not been confirmed here. Keyed off the message
    ``_get`` raises (``HTTP <status>: GET <path>``).
    """
    if "HTTP 403" in message or "HTTP 401" in message:
        return (
            "The app could not read the enterprise user directory. This is a permission result, not a statement about the account: "
            "in oauth mode the effective permission is the AUTHORISING user's, so check that user's Box role (an admin / co-admin "
            "with user visibility), and check the app's Application Scopes in the Box Developer Console. Which scope this endpoint "
            "requires has not been verified end-to-end for this server."
        )
    return None


@mcp.tool()
def get_user(login: str) -> dict:
    """Look up ONE Box account by its exact login (the account's email address).

    Answers "what is this account's state?" — the question behind a ticket that says
    "my Box account is disabled". Every other tool here reads the event stream or walks
    folders, so an account with no recent events is invisible to them; this is one
    request against the user directory and the only tool that answers about an account
    directly. Use it when a specific account is named. It cannot list, search or
    enumerate accounts: it takes one login and answers about that login only.

    Args:
        login: The account's full Box login, i.e. its email address
            (``someone@example.com``) — not a display name, not a user id. Matched
            EXACTLY, case-insensitively. A partial login does not match.

    This server is downstream of an identity provider, not the master. Read what comes
    back as "what Box currently believes", and compare it against the IdP's own record
    (which is authoritative for who the account is). A disagreement is the finding, and
    is usually drift on the Box side rather than a mistyped address:

    - ``enterprise`` absent/null — the account is no longer in the enterprise (it has
      become a free personal account), so enterprise SSO no longer applies to it even
      though the IdP still authenticates the person. Box classes such an account as
      *external* and returns it only on a COMPLETE login match, which is exactly what
      this tool asks for — so it is reachable here, and a partial login would silently
      lose it.
    - ``status`` other than ``active`` — the IdP authenticates, Box refuses.
    - ``is_platform_access_only`` true — an App User, which cannot sign in interactively
      at all.

    One drift this tool cannot find for you: the same person under a second login at
    another domain (an alias, or a duplicate left by a migration). ``filter_term``
    prefix-matches the WHOLE term, so a search for ``alice@old.example`` can never return
    ``alice@new.example``. Finding that would take a search on the local part alone,
    which is a prefix search over the directory and is refused here by design. Ask the
    identity provider which login it asserts, and look that one up.

    Returns two shapes, distinguished by whether the lookup completed.

    On a completed lookup:
    - ``requested_login`` — what was asked for, echoed back.
    - ``found`` — bool. **The only field that says whether the account was found.**
    - ``user`` — the account when ``found`` is true, else ``null``: id, name, login,
      ``status`` (``active`` / ``inactive`` / …, the usual answer to "why can't I sign
      in"), ``role``, ``enterprise``, ``space_used`` / ``space_amount`` (quota
      exhaustion is another recurring cause), ``created_at``, ``modified_at``.
    - ``other_prefix_hits`` — how many further accounts the prefix search matched. A
      COUNT ONLY: those are different accounts and are deliberately not identified, so
      this can never be used to browse the directory.
    - ``search_hits`` — how many entries the search returned.
    - ``capped`` — true when the search result was truncated, so ``found: false`` is
      inconclusive rather than negative (``note`` says so).
    - ``note`` — plain-language reading of the above.

    Why the filtering matters: Box's ``filter_term`` is a **prefix search over display
    name AND login**, not a lookup, so it happily returns somebody else — a colleague
    whose display name starts with the same letters. ``user`` is therefore only ever an
    exact login match, no other hit is ever identified, and a term that is not
    email-shaped is refused before the request is made (a one-character term would
    otherwise return a page of real accounts).

    On failure the other shape is returned: ``{"error": ...}`` (missing env / ``needs-login`` for an expired
    OAuth session / a Box API error), plus ``likely_cause`` when the failure was a
    permission one. **``found`` is absent from that shape on purpose** — a failed lookup
    is not a negative answer, and must never be read as "no such account". Auth caveat,
    stated honestly: this server supports two auth modes,
    and under ``oauth`` the effective permission is the authorising user's. Whether
    ``/2.0/users`` is reachable **has not been verified end-to-end** in either mode, and
    no scope requirement is claimed here that was not confirmed — a permission failure
    therefore points at the authorising user's role and the app's Application Scopes
    rather than asserting which one is at fault.
    """
    needle = login.strip()
    if not needle:
        # An empty filter_term is not an empty search: Box would answer with a page of
        # the enterprise directory, which is the enumeration this tool exists to avoid.
        # Refused before any API call.
        return {"error": "login is required: pass the account's exact Box login (an email address)"}

    if not _is_email_shaped(needle):
        # filter_term is a PREFIX search over display name AND login, so a short or
        # name-shaped term matches strangers: "a" returns a page of real accounts. A
        # login is an email address, and requiring that shape is what keeps this a
        # lookup. Refused before any API call -- the request itself is the leak.
        return {
            "error": (
                "login must be a full Box login (an email address). A display name or a "
                "partial string is a prefix search over the whole directory, not a lookup"
            )
        }

    client, err = _connect()
    if err:
        return err
    try:
        resp = client.get_users(filter_term=needle, fields=USER_LOOKUP_FIELDS, limit=_USER_SEARCH_LIMIT)
    except BoxNotAuthenticatedError as e:
        reset_client()
        return {"error": f"needs-login: {e}"}
    except BoxError as e:
        reset_client()
        result = {"error": str(e)}
        hint = _user_lookup_hint(str(e))
        if hint:
            result["likely_cause"] = hint
        return result

    entries = resp.get("entries") or []
    wanted = needle.lower()
    exact = [e for e in entries if _login_is(e, wanted)]
    # Non-exact hits are counted, never identified: they are different accounts, and
    # returning their id/name/login turned this lookup into a page of the enterprise
    # directory.
    other_prefix_hits = sum(1 for e in entries if not _login_is(e, wanted))

    # Truncation disclosure, same contract as the scan tools' `capped`: a "not found"
    # computed over a partial result is not a negative answer. total_count is Box's own
    # figure; if it is absent, a full page is assumed to be truncated.
    total = resp.get("total_count")
    capped = total > len(entries) if isinstance(total, int) else len(entries) >= _USER_SEARCH_LIMIT

    if exact:
        note = _FOUND_NOTE
    else:
        note = _NOT_FOUND_CAPPED_NOTE if capped else _NOT_FOUND_NOTE
    return {
        "requested_login": needle,
        "found": bool(exact),
        "user": exact[0] if exact else None,
        # Count only, never identities: these are other accounts that merely share the
        # prefix. Reported so a caller knows the search was not empty.
        "other_prefix_hits": other_prefix_hits,
        "search_hits": len(entries),
        "capped": capped,
        "note": note,
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


# ---------------------------------------------------------------------------
# One folder's contents ("ls"), with submitter attribution.
# ---------------------------------------------------------------------------

#: Requested for every listed item. ``uploader_display_name`` is the load-bearing
#: one: for a File Request upload Box records NO user, so ``created_by`` and
#: ``modified_by`` both read "Anonymous User" and ``owned_by`` is the application's
#: own service account -- identical on every row and useless for attribution.
#: Verified against a live folder: 101 of 101 files carried
#: ``uploader_display_name`` while ``created_by`` was anonymous on all of them.
ITEM_LIST_FIELDS = [
    "type",
    "id",
    "name",
    "uploader_display_name",
    "created_by",
    "created_at",
    "modified_at",
    "size",
    "shared_link",
]

#: Item types that have a Box web path. Closed on purpose: an unrecognised type
#: gets ``item_url: null`` rather than a guessed URL that 404s.
_LINKABLE_TYPES = ("file", "folder", "web_link")

#: One page is fetched regardless of the caller's ``limit`` (Box's own maximum),
#: because filtering happens after the fetch: bounding the FETCH by ``limit``
#: would make ``uploaded_by`` search only the newest N items and miss the match
#: it was asked for.
_ITEM_PAGE = 1000


def _parse_ts(value: str | None):
    """Parse a Box/ISO-8601 timestamp to an aware datetime, or None.

    Comparing these as STRINGS is wrong and quietly so. Box stamps items in its
    own zone (``-07:00`` on the folder this was built for) while a caller asks in
    theirs, so ``"2026-08-13T22:33:26-07:00" < "2026-08-14T10:00:00+09:00"`` is
    True as text and False as time -- the item is four and a half hours NEWER.
    A lexicographic filter therefore misclassifies everything near a date
    boundary, and says nothing about having done so.

    ``Z`` is rewritten to ``+00:00`` because ``fromisoformat`` rejects it before
    Python 3.11, and this package supports 3.10.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _bound(value: str, *, name: str):
    """Parse a caller's ``since``/``until`` bound: ``(dt|None, error|None)``.

    A NAIVE timestamp is refused rather than assumed. "2026-08-14" means a
    different instant in every zone, and this server has no basis for picking
    one -- guessing would reproduce, as a default, the same off-by-a-timezone
    error the string comparison would have made.
    """
    if not value.strip():
        return None, None
    parsed = _parse_ts(value)
    if parsed is None:
        return None, {"error": f"{name} is not an ISO 8601 timestamp: {value!r}"}
    if parsed.tzinfo is None:
        return None, {
            "error": (
                f"{name} must carry a UTC offset, e.g. 2026-08-14T00:00:00+09:00 "
                f"(got {value!r}); a bare date names a different instant in every timezone"
            )
        }
    return parsed, None


def _item_row(it: dict) -> dict:
    """Project one Box item to the fields a triage answer needs.

    Built key by key rather than by deleting from Box's dict, so a field Box adds
    later cannot arrive in an answer nobody reviewed.
    """
    itype = it.get("type")
    sl = it.get("shared_link") or {}
    return {
        "item_type": itype,
        "item_id": it.get("id"),
        "name": it.get("name"),
        # See ITEM_LIST_FIELDS: uploader_display_name is the only field carrying
        # the submitter. created_by is kept as the fallback for a file uploaded
        # by a signed-in user, where the reverse holds.
        "uploaded_by": it.get("uploader_display_name") or (it.get("created_by") or {}).get("login"),
        "created_at": it.get("created_at"),
        "modified_at": it.get("modified_at"),
        # A folder's `size` is a rolled-up total, a different unit of meaning
        # from a file's byte count -- reporting both under one key invites a sum.
        "size_bytes": it.get("size") if itype == "file" else None,
        "shared_link_access": sl.get("access"),
        # Box's own web path happens to be the item type verbatim, for all three
        # types a folder can hold. Verified with GET (HEAD answers 405 for the
        # file form, which made an earlier check look like a failure):
        # /file/{id}, /folder/{id} and /web_link/{id} all 302 to login carrying
        # the right redirect_url, while /weblink/{id} is a 404 -- so the
        # underscore matters and the set is closed rather than derived from
        # whatever `type` Box sends.
        "item_url": f"https://app.box.com/{itype}/{it.get('id')}" if itype in _LINKABLE_TYPES else None,
    }


@mcp.tool()
def list_folder_items(folder_id: str, uploaded_by: str = "", since: str = "", until: str = "", limit: int = 100) -> dict:
    """List ONE Box folder's contents, newest first, with who uploaded each item.

    An ``ls``, not a ``cat``: names, timestamps, sizes, uploader and a direct link
    per item. File CONTENT is not read and no shared link is ever created.

    Written for a help desk answering a submitted enquiry whose attachments land
    in a Box folder. Instead of a human going to find that folder, the answer can
    name the attachments and link straight to them.

    Args:
        folder_id: The folder's Box id — decimal digits, the number at the end of
            a Box folder URL. ``"0"`` is the caller's own root ("All Files"), the
            same convention the enumeration tools use. Anything else — a name, a
            label, a whole URL — is refused before any request is made.
        uploaded_by: Optional. Return only items uploaded by this person, matched
            EXACTLY and case-insensitively against ``uploaded_by`` below. Use it
            when the enquiry names its submitter.
        since: Optional lower bound on upload time (``created_at``), inclusive.
        until: Optional upper bound on upload time (``created_at``), inclusive.
            Both MUST carry a UTC offset (``2026-08-14T00:00:00+09:00``): a bare
            date names a different instant in every timezone, and this server has
            no basis for choosing one. Compared as instants, not as text.
        limit: How many rows to RETURN after filtering (default 100). It does not
            bound what is searched — a full page is always fetched first, so a
            match for ``uploaded_by`` is found even when it is not among the
            newest items.

    On ``uploaded_by``: Box populates ``uploader_display_name`` for an upload made
    through a File Request, where no Box user is involved — ``created_by`` and
    ``modified_by`` both read "Anonymous User" and the owner is the application's
    service account, so neither identifies anybody. For a file uploaded by a
    signed-in user the reverse holds, so ``created_by`` is used as the fallback.
    Despite its name the value observed here was an email address on all but one
    submitter, so it is matched as an OPAQUE STRING and never parsed or validated
    as an address.

    Treat ``name`` and ``uploaded_by`` as text the submitter chose. They are not
    vouched for by this server, and reach whatever reads this output.

    Returns, on a completed listing:
    - ``folder_id`` / ``folder_name`` / ``folder_url``
    - ``items`` — the rows, newest ``created_at`` first
    - ``returned`` / ``matched`` — rows returned, and rows that matched the
      filters. ``returned < matched`` means ``limit`` cut the answer.
    - ``total_in_folder`` — Box's own count for the folder, before filtering
    - ``capped`` — true when the folder holds more than one page, so the filters
      were applied to part of it and a "no match" is inconclusive rather than
      negative. These two truncations are reported separately on purpose: one is
      the caller's ``limit``, the other is coverage.
    - ``note`` — the above in words

    On failure the shape is ``{"error": ...}`` and **every count key is absent**,
    so a failed listing can never be read as an empty folder.
    """
    fid, bad = _checked_root(folder_id)
    if bad:
        return bad
    lower, err_lo = _bound(since, name="since")
    if err_lo:
        return err_lo
    upper, err_hi = _bound(until, name="until")
    if err_hi:
        return err_hi

    client, err = _connect()
    if err:
        return err
    try:
        meta = client.get_folder(fid, fields=["id", "name"])
        resp = client.get_folder_items(fid, fields=ITEM_LIST_FIELDS, limit=_ITEM_PAGE)
    except BoxNotAuthenticatedError as e:
        reset_client()
        return {"error": f"needs-login: {e}"}
    except BoxError as e:
        reset_client()
        return {"error": str(e)}

    entries = resp.get("entries") or []
    total = resp.get("total_count")
    capped = total > len(entries) if isinstance(total, int) else len(entries) >= _ITEM_PAGE

    rows = [_item_row(it) for it in entries]
    # casefold, not lower: this value is an opaque string that can be a display
    # name, and lower() does not fold every case pair -- "Straße".lower() is
    # "straße" while a caller typing "STRASSE" gets "strasse", so a real
    # submitter would come back as "no attachments". (get_user matches a LOGIN
    # with lower(); that is an address, a narrower thing than this.)
    wanted = uploaded_by.strip().casefold()
    if wanted:
        rows = [r for r in rows if (r["uploaded_by"] or "").strip().casefold() == wanted]
    if lower or upper:
        keep = []
        for r in rows:
            ts = _parse_ts(r["created_at"])
            # An unparseable/absent created_at cannot be shown to satisfy a bound,
            # so it is dropped rather than passed through as "probably fine".
            if ts is None or (lower and ts < lower) or (upper and ts > upper):
                continue
            keep.append(r)
        rows = keep

    # Sorted here, not by Box. `sort=date` orders by TYPE first (a subfolder
    # precedes every file regardless of date) and, measured against this folder,
    # matched neither created_at nor modified_at order -- so it cannot honestly be
    # presented as "newest". Sorting on the parsed instant also survives Box
    # returning a different UTC offset.
    rows.sort(key=lambda r: _parse_ts(r["created_at"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    matched = len(rows)
    rows = rows[: max(0, limit)]

    if capped:
        note = (
            f"{matched} item(s) matched among the {len(entries)} fetched, but the folder holds "
            f"{total} — filters were applied to part of it, so an absent item is inconclusive."
        )
    elif matched == 0:
        note = "No item in this folder matched. The folder was read in full, so this is a negative answer, not a partial one."
    else:
        note = f"{matched} of {len(entries)} item(s) in this folder matched."
    if len(rows) < matched:
        note += f" Showing the {len(rows)} newest; raise limit for the rest."

    return {
        "folder_id": meta.get("id") or fid,
        "folder_name": meta.get("name"),
        "folder_url": f"https://app.box.com/folder/{meta.get('id') or fid}",
        "items": rows,
        "returned": len(rows),
        "matched": matched,
        "total_in_folder": total,
        "capped": capped,
        "note": note,
    }
