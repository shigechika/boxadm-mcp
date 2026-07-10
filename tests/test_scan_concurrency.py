"""Tests for the parallelized ``_scan()`` fan-out (issue #7).

These pin the guarantees the parallelization must not break: per-folder
collaboration/item lookups actually run concurrently, but the ``capped`` budget
semantics, the BFS-order output, and the ``folders_scanned`` count stay identical
to a sequential walk, and folders dropped by a per-folder API error are disclosed
via ``fetch_errors`` rather than silently under-reported.

respx intercepts httpx at the transport layer and is safe to drive from several
worker threads, so the scan's ThreadPoolExecutor can be exercised end-to-end
against mocked Box responses. Concurrency is proven with a ``threading.Barrier``
(deterministic — N parties must arrive before any proceeds) rather than timing,
which would be flaky under CI load.
"""

import threading
from collections import Counter

import httpx
import pytest
import respx

from boxadm_mcp import server


def _call(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture(autouse=True)
def _org_domain(monkeypatch):
    monkeypatch.setenv("BOX_ALLOWED_DOMAINS", "example.com")


def _folder_entry(fid, *, ext_owned=False, link=None):
    return {
        "type": "folder",
        "id": fid,
        "name": fid,
        "owned_by": {"login": f"owner-{fid}@example.com"},
        "is_externally_owned": ext_owned,
        "shared_link": link,
    }


def _root_items(fids):
    entries = [_folder_entry(f) for f in fids]
    return {"total_count": len(entries), "entries": entries}


EXT_COLLAB = {"entries": [{"accessible_by": {"type": "user", "login": "ext@gmail.com"}, "role": "editor", "status": "accepted"}]}


def _collab_body(fid):
    """One external collaborator, uniquely named per folder (for ordering assertions)."""
    return {"entries": [{"accessible_by": {"login": f"ext-{fid}@gmail.com"}, "role": "viewer", "status": "accepted"}]}


def _fid_of(path):
    return path.split("/")[3]  # /2.0/folders/<fid>/(items|collaborations)


def _tree_router(items_map):
    """respx router serving a folder tree: ``items_map`` maps folder id -> child ids,
    and every folder answers /collaborations with its own unique external collaborator.
    Records the folder ids whose collaborations / items were fetched."""
    collab_ids: list[str] = []
    item_ids: list[str] = []
    lock = threading.Lock()

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path.endswith("/items"):
            fid = _fid_of(path)
            with lock:
                item_ids.append(fid)
            return httpx.Response(200, json=_root_items(items_map.get(fid, [])))
        if path.endswith("/collaborations"):
            fid = _fid_of(path)
            with lock:
                collab_ids.append(fid)
            return httpx.Response(200, json=_collab_body(fid))
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    return r, collab_ids, item_ids


# --------------------------------------------------------------------------
# Multi-level (max_depth>=2) walk: the core of the parallelization
# --------------------------------------------------------------------------
def test_scan_max_depth_2_bfs_order_and_count():
    """root->[A,B], A->[A1,A2], B->[B1,B2] at max_depth=2. All levels are walked,
    non-root folders' items ARE listed (concurrently), and collaborations come back
    in cross-parent BFS order (all of A's children before B's) — pinning the
    level-synchronous drain + order-preserving merge a sequential walk would give."""
    tree = {"0": ["A", "B"], "A": ["A1", "A2"], "B": ["B1", "B2"]}
    r, collab_ids, item_ids = _tree_router(tree)
    with r:
        client, err = server._connect()
        assert err is None
        scan = server._scan(client, "0", max_folders=100, max_depth=2, want_collabs=True, concurrency=4)

    assert scan["folders_scanned"] == 7  # root + A,B + A1,A2,B1,B2
    assert scan["capped"] is False
    assert scan["fetch_errors"] == 0
    # Non-root folders A and B were item-listed (depth 1 < 2), proving multi-level
    # concurrent item fetches actually happen — not just the root.
    assert set(item_ids) == {"0", "A", "B"}
    # Cross-parent BFS order: level 1 (A,B) before level 2, and A's children before B's.
    assert [c["folder_id"] for c in scan["external_collaborations"]] == ["A", "B", "A1", "A2", "B1", "B2"]


def test_scan_max_depth_2_capped_prefix():
    """Budget cutting off partway through the depth-2 frontier visits the exact BFS
    prefix and sets capped — multi-level cap equivalence with a sequential walk."""
    tree = {"0": ["A", "B"], "A": ["A1", "A2"], "B": ["B1", "B2"]}
    r, collab_ids, _ = _tree_router(tree)
    with r:
        client, err = server._connect()
        # budget 4 → root(1) + A,B(2) + A1(1) = 4; A2,B1,B2 unreached.
        scan = server._scan(client, "0", max_folders=4, max_depth=2, want_collabs=True, concurrency=4)

    assert scan["folders_scanned"] == 4
    assert scan["capped"] is True
    # Only the BFS prefix's collaborations were queried (root has none).
    assert set(collab_ids) == {"A", "B", "A1"}


def test_scan_deduplicates_folder_reachable_from_two_parents():
    """A subfolder id listed under two parents (root->[A,B], A->[C], B->[C]) is
    visited once: its collaborations are fetched once, it appears once, and it
    consumes the budget once (folders_scanned counts 4, not 5)."""
    tree = {"0": ["A", "B"], "A": ["C"], "B": ["C"]}
    r, collab_ids, _ = _tree_router(tree)
    with r:
        client, err = server._connect()
        scan = server._scan(client, "0", max_folders=100, max_depth=2, want_collabs=True, concurrency=4)

    assert scan["folders_scanned"] == 4  # root + A + B + C (C not double-counted)
    assert collab_ids.count("C") == 1  # dup fetched only once
    c_rows = [c for c in scan["external_collaborations"] if c["folder_id"] == "C"]
    assert len(c_rows) == 1  # dup surfaces only once


def test_fetch_errors_capped_at_one_per_folder():
    """An intermediate folder whose collaborations AND items both fail counts once,
    so fetch_errors never exceeds folders_scanned."""

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=_root_items(["A"]))
        if path == "/2.0/folders/A/collaborations":
            return httpx.Response(500, json={"message": "err"})  # fails
        if path == "/2.0/folders/A/items":
            return httpx.Response(503, json={"message": "err"})  # also fails
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        client, err = server._connect()
        scan = server._scan(client, "0", max_folders=100, max_depth=2, want_collabs=True, concurrency=4)

    assert scan["fetch_errors"] == 1  # folder A failed both calls → counted once, not twice
    assert scan["folders_scanned"] == 2  # root + A (A's failed items yields no children)


def test_public_shared_links_surfaces_fetch_errors():
    """public_shared_links (want_collabs=False) also discloses a failed item listing
    via fetch_errors — a dropped surfacing line here would otherwise be silent."""

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=_root_items(["A"]))
        if path == "/2.0/folders/A/items":
            return httpx.Response(500, json={"message": "err"})  # A's listing fails
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        out = _call(server.public_shared_links)(max_folders=100, max_depth=2)

    assert out["fetch_errors"] == 1
    assert out["folders_scanned"] == 2  # root + A


# --------------------------------------------------------------------------
# Concurrent token handling: N workers each hit a 401, refresh, and retry without
# a spurious 'Bearer None' / fetch_error (the CCG check-then-return snapshot).
# --------------------------------------------------------------------------
def test_concurrent_401_refresh_no_spurious_errors():
    n = 6
    fids = [f"F{i}" for i in range(n)]
    seen_401: Counter = Counter()
    lock = threading.Lock()

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=_root_items(fids))
        if path.endswith("/collaborations"):
            fid = _fid_of(path)
            with lock:
                first = seen_401[fid] == 0
                seen_401[fid] += 1
            if first:
                # First hit 401s → _get resets the token and retries; under
                # concurrency several workers reset/refetch at once.
                return httpx.Response(401, json={"message": "expired"})
            return httpx.Response(200, json=_collab_body(fid))
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        client, err = server._connect()
        scan = server._scan(client, "0", max_folders=100, max_depth=1, want_collabs=True, concurrency=n)

    assert scan["fetch_errors"] == 0  # every 401 was transparently retried, none dropped
    assert scan["folders_scanned"] == n + 1
    assert len(scan["external_collaborations"]) == n  # all folders' collaborators surfaced
    assert all(v == 2 for v in seen_401.values())  # each folder: one 401 then one 200


# --------------------------------------------------------------------------
# Concurrency proof
# --------------------------------------------------------------------------
def test_scan_fetches_collaborations_concurrently():
    """With ``concurrency=N`` and N sibling folders, all N collaboration calls are
    in-flight at once — proven by a Barrier that only releases once N parties wait."""
    n = 5
    fids = [f"F{i}" for i in range(n)]
    barrier = threading.Barrier(n, timeout=15)
    lock = threading.Lock()
    passed_barrier: list[str] = []

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=_root_items(fids))
        if path.endswith("/collaborations"):
            # Blocks until all N collaboration workers arrive; a sequential (or
            # under-provisioned) pool would never reach N parties and time out.
            barrier.wait()
            with lock:
                passed_barrier.append(path)
            return httpx.Response(200, json=EXT_COLLAB)
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        client, err = server._connect()
        assert err is None
        scan = server._scan(client, "0", max_folders=100, max_depth=1, want_collabs=True, concurrency=n)

    assert len(passed_barrier) == n  # every collaboration call cleared the barrier → truly concurrent
    assert set(passed_barrier) == {f"/2.0/folders/{f}/collaborations" for f in fids}
    assert scan["folders_scanned"] == n + 1  # root + N leaves
    assert scan["capped"] is False
    assert scan["fetch_errors"] == 0
    assert len(scan["external_collaborations"]) == n  # one external collaborator per folder


# --------------------------------------------------------------------------
# Budget / capped semantics unchanged under parallelism
# --------------------------------------------------------------------------
def test_scan_capped_visits_exact_bfs_prefix():
    """max_folders below the reachable count still visits the BFS prefix (root +
    the first budget-1 folders), sets capped, and never touches folders past it."""
    fids = [f"F{i}" for i in range(10)]
    queried: list[str] = []
    lock = threading.Lock()

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=_root_items(fids))
        if path.endswith("/collaborations"):
            with lock:
                queried.append(path.split("/")[3])  # folder id
            return httpx.Response(200, json={"entries": []})
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        client, err = server._connect()
        # budget 5 → root (1) + F0..F3 (4) = 5 folders; F4..F9 unreached.
        scan = server._scan(client, "0", max_folders=5, max_depth=1, want_collabs=True, concurrency=4)

    assert scan["folders_scanned"] == 5
    assert scan["capped"] is True
    # Exactly the BFS prefix's collaborations were queried — nothing past the cap.
    assert set(queried) == {"F0", "F1", "F2", "F3"}


def test_scan_not_capped_when_everything_fits():
    fids = [f"F{i}" for i in range(3)]

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=_root_items(fids))
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        client, err = server._connect()
        scan = server._scan(client, "0", max_folders=150, max_depth=1, want_collabs=True, concurrency=4)

    assert scan["folders_scanned"] == 4  # root + 3
    assert scan["capped"] is False


# --------------------------------------------------------------------------
# Output ordering is deterministic (BFS order), independent of completion order
# --------------------------------------------------------------------------
def test_scan_output_order_matches_bfs_not_completion_order():
    """Workers are forced to *complete* in reverse (F2, then F1, then F0), yet the
    merged collaborations come back in listing order — proving executor.map's
    order-preserving merge, so results are deterministic under concurrency."""
    fids = ["F0", "F1", "F2"]
    ready = {f: threading.Event() for f in fids}
    start = threading.Barrier(len(fids), timeout=15)

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=_root_items(fids))
        if path.endswith("/collaborations"):
            fid = path.split("/")[3]
            start.wait()  # all three enter together
            # Release in reverse: F2 returns first and unblocks F1, which unblocks F0.
            if fid == "F2":
                ready["F1"].set()
            elif fid == "F1":
                ready["F1"].wait()
                ready["F0"].set()
            else:  # F0 completes last
                ready["F0"].wait()
            collab = {"accessible_by": {"login": f"ext-{fid}@gmail.com"}, "role": "viewer", "status": "accepted"}
            return httpx.Response(200, json={"entries": [collab]})
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        client, err = server._connect()
        scan = server._scan(client, "0", max_folders=100, max_depth=1, want_collabs=True, concurrency=3)

    # Output follows folder-listing (BFS) order F0, F1, F2 — not completion order.
    assert [c["folder_id"] for c in scan["external_collaborations"]] == ["F0", "F1", "F2"]
    assert [c["collaborator"] for c in scan["external_collaborations"]] == ["ext-F0@gmail.com", "ext-F1@gmail.com", "ext-F2@gmail.com"]


# --------------------------------------------------------------------------
# fetch_errors: per-folder failures are counted and surfaced, not silently dropped
# --------------------------------------------------------------------------
def test_scan_counts_and_surfaces_fetch_errors():
    """A folder whose collaborations call fails is dropped from findings but
    disclosed via fetch_errors — the parallel path keeps the 'no silent partial
    coverage' contract that capped gives for the budget cap."""

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=_root_items(["FOK", "FERR"]))
        if path == "/2.0/folders/FOK/collaborations":
            return httpx.Response(200, json=EXT_COLLAB)
        if path == "/2.0/folders/FERR/collaborations":
            return httpx.Response(500, json={"message": "server error"})  # transient failure
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        out = _call(server.external_collaborators)(max_folders=100, max_depth=1)

    assert out["fetch_errors"] == 1  # FERR's failed lookup is disclosed
    assert out["folders_scanned"] == 3  # both leaves consumed budget (root + FOK + FERR)
    # Only the reachable folder's external collaborator surfaces; FERR contributes none.
    assert {c["collaborator"] for c in out["external_collaborators"]} == {"ext@gmail.com"}
    assert all(c["folder_id"] == "FOK" for c in out["external_collaborators"])


def test_item_page_failure_counts_as_fetch_error():
    """A failed get_folder_items page (the folder-listing side) is also counted."""

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        client, err = server._connect()
        scan = server._scan(client, "0", max_folders=100, max_depth=1, want_collabs=True, concurrency=4)

    assert scan["fetch_errors"] == 1  # root's items page failed
    assert scan["folders_scanned"] == 1  # only root was reachable
    assert scan["external_collaborations"] == []


# --------------------------------------------------------------------------
# Concurrency is configurable via env, clamped to a safe range
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (None, server._SCAN_CONCURRENCY_DEFAULT),
        ("", server._SCAN_CONCURRENCY_DEFAULT),
        ("1", 1),
        ("16", 16),
        ("0", 1),  # clamped up
        ("-3", 1),  # clamped up
        ("999", 32),  # clamped down
        ("nope", server._SCAN_CONCURRENCY_DEFAULT),  # unparseable → default
    ],
)
def test_scan_concurrency_env_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("BOX_SCAN_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("BOX_SCAN_CONCURRENCY", value)
    assert server._scan_concurrency() == expected


@pytest.mark.parametrize("override,expected", [(0, 1), (-5, 1), (1, 1), (8, 8), (32, 32), (999, 32)])
def test_scan_concurrency_explicit_override_is_clamped(override, expected):
    """An explicit concurrency (e.g. _scan's arg) is clamped to 1..32 just like the
    env var, so ThreadPoolExecutor never gets a 0/negative or absurd worker count."""
    assert server._scan_concurrency(override) == expected


def test_scan_with_zero_concurrency_clamps_and_runs():
    """_scan(concurrency=0) is clamped to 1 rather than crashing ThreadPoolExecutor."""
    r, collab_ids, _ = _tree_router({"0": ["F0", "F1"]})
    with r:
        client, err = server._connect()
        assert err is None
        scan = server._scan(client, "0", max_folders=100, max_depth=1, want_collabs=True, concurrency=0)
    assert scan["folders_scanned"] == 3  # root + F0 + F1
    assert set(collab_ids) == {"F0", "F1"}


def test_scan_default_concurrency_still_correct():
    """Tools that don't pass an explicit concurrency (use the env default) return
    the same results — smoke test that the default-path wiring works."""
    counts: Counter = Counter()
    lock = threading.Lock()

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=_root_items(["F0", "F1", "F2"]))
        if path.endswith("/collaborations"):
            with lock:
                counts[path.split("/")[3]] += 1
            return httpx.Response(200, json=EXT_COLLAB)
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    with r:
        out = _call(server.external_collaborators)(max_folders=100, max_depth=1)

    assert out["count"] == 3
    assert out["fetch_errors"] == 0
    assert set(counts) == {"F0", "F1", "F2"}  # each folder queried exactly once
    assert all(v == 1 for v in counts.values())
