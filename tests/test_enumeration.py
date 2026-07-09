"""Tests for the enumeration tools (external_collaborators / public_shared_links / top_external_sharers)."""

from collections import Counter

import httpx
import pytest
import respx

from boxadm_mcp import server


def _call(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture(autouse=True)
def _org_domain(monkeypatch):
    """Fixtures below use example.com as "us"; gmail.com/partner.example as external."""
    monkeypatch.setenv("BOX_ALLOWED_DOMAINS", "example.com")


ROOT_ITEMS = {
    "total_count": 3,
    "entries": [
        {
            "type": "folder",
            "id": "F1",
            "name": "alpha",
            "owned_by": {"login": "ownerA@example.com"},
            "shared_link": {"access": "open", "permissions": {"can_download": True}},
        },
        {"type": "folder", "id": "F2", "name": "beta", "owned_by": {"login": "ownerB@example.com"}, "shared_link": None},
        {"type": "file", "id": "X", "name": "x.pdf", "owned_by": {"login": "ownerA@example.com"}, "shared_link": {"access": "open"}},
    ],
}
F1_COLLABS = {
    "entries": [
        {"accessible_by": {"type": "user", "login": "ext@gmail.com"}, "role": "editor", "status": "accepted", "expires_at": None},
        {"accessible_by": {"type": "user", "login": "u@example.com"}, "role": "viewer", "status": "accepted"},
        # group collaboration (no login) — must NOT be flagged external
        {"accessible_by": {"type": "group", "name": "affiliation-grp"}, "role": "viewer", "status": "accepted"},
        # pending external invite by email (no accessible_by login) — MUST be flagged
        {"accessible_by": {}, "invite_email": "pending@partner.example", "role": "viewer", "status": "pending"},
    ]
}


def _router():
    counts: Counter = Counter()

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            counts["token"] += 1
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            counts["items"] += 1
            return httpx.Response(200, json=ROOT_ITEMS)
        if path.endswith("/collaborations"):
            counts["collab"] += 1
            return httpx.Response(200, json=F1_COLLABS if path == "/2.0/folders/F1/collaborations" else {"entries": []})
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    return r, counts


def test_external_collaborators_external_only_and_invite():
    r, _ = _router()
    with r:
        out = _call(server.external_collaborators)(max_depth=1)
    by = {c["collaborator"]: c for c in out["external_collaborators"]}
    assert set(by) == {"ext@gmail.com", "pending@partner.example"}  # group + internal excluded
    assert out["count"] == 2
    assert by["pending@partner.example"]["collaborator_type"] == "invite"
    assert by["ext@gmail.com"]["folder_id"] == "F1"


def test_public_shared_links_lists_open_and_skips_collab_calls():
    r, counts = _router()
    with r:
        out = _call(server.public_shared_links)(max_depth=1)
    ids = {p["item_id"] for p in out["public_shared_links"]}
    assert ids == {"F1", "X"}  # F2 has no link
    assert counts["collab"] == 0  # optimization: no collaboration calls when not needed


def test_top_external_sharers_ranks_owner():
    r, _ = _router()
    with r:
        out = _call(server.top_external_sharers)(max_depth=1)
    top = out["top_external_sharers"][0]
    assert top["owner"] == "ownerA@example.com"
    # ownerA: 2 external collabs (gmail + invite, both on F1) + 2 public links (F1, X) = 4
    assert top["external_collaborations"] == 2 and top["public_links"] == 2 and top["total"] == 4


def test_scan_memo_shared_between_collab_tools():
    r, counts = _router()
    with r:
        _call(server.external_collaborators)(max_depth=1)
        after_first = dict(counts)
        _call(server.top_external_sharers)(max_depth=1)  # same want_collabs key → reuse memo
    assert counts["items"] == after_first["items"]  # no re-traversal
    assert counts["collab"] == after_first["collab"]


EXT_OWNED_ROOT = {
    "total_count": 3,
    "entries": [
        # internal-owned folder with a real external collaborator — in scope
        {"type": "folder", "id": "F1", "name": "alpha", "owned_by": {"login": "ownerA@example.com"}, "shared_link": None},
        # externally-owned folder (we're only a guest) — out of scope, must be skipped
        {"type": "folder", "id": "FEXT", "name": "EXT_vendor", "owned_by": {"login": "svc@partner.example"}, "shared_link": None},
        # folder with UNKNOWN ownership (no owned_by) — stays in scope (cautious)
        {"type": "folder", "id": "FUNK", "name": "mystery", "shared_link": None},
    ],
}


def _router_ext_owned():
    counts: Counter = Counter()
    collab_paths: list[str] = []

    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=EXT_OWNED_ROOT)
        if path.endswith("/collaborations"):
            collab_paths.append(path)
            # F1 has an external collaborator; FUNK has none
            body = F1_COLLABS if path == "/2.0/folders/F1/collaborations" else {"entries": []}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    return r, counts, collab_paths


def test_external_collaborators_skips_externally_owned_folders():
    r, _, collab_paths = _router_ext_owned()
    with r:
        out = _call(server.external_collaborators)(max_depth=1)

    # The externally-owned folder is reported separately, never as a leak of our data.
    skipped = {s["folder_id"]: s for s in out["skipped_externally_owned"]}
    assert set(skipped) == {"FEXT"}
    assert skipped["FEXT"]["owner"] == "svc@partner.example"

    # Its collaborations are never even queried (out of scope, budget-free)...
    assert "/2.0/folders/FEXT/collaborations" not in collab_paths
    # ...and the unknown-owner folder IS still walked (cautious toward auditing).
    assert "/2.0/folders/FUNK/collaborations" in collab_paths

    # Only the internal folder's external collaborator surfaces; nothing from FEXT.
    by = {c["collaborator"] for c in out["external_collaborators"]}
    assert by == {"ext@gmail.com", "pending@partner.example"}
    assert all(c["folder_id"] == "F1" for c in out["external_collaborators"])

    # Skipped folder does not consume the folders_scanned budget.
    assert out["folders_scanned"] == 3  # root + F1 + FUNK (FEXT excluded)


def test_enumeration_missing_env(monkeypatch):
    monkeypatch.delenv("BOX_CLIENT_ID", raising=False)
    out = _call(server.external_collaborators)()
    assert "error" in out and "BOX_CLIENT_ID" in out["error"]
