"""Tests for the daily_brief synthesis tool (access events + exposure enumeration)."""

import httpx
import respx

from boxadm_mcp import server

ROOT_ITEMS = {
    "total_count": 2,
    "entries": [
        {
            "type": "folder",
            "id": "F1",
            "name": "alpha",
            "owned_by": {"login": "ownerA@internal.example"},
            "shared_link": {"access": "open", "permissions": {"can_download": True}},
        },
        {"type": "file", "id": "X", "name": "x.pdf", "owned_by": {"login": "ownerA@internal.example"}, "shared_link": None},
    ],
}
F1_COLLABS = {
    "entries": [
        {"accessible_by": {"type": "user", "login": "ext@gmail.com"}, "role": "editor", "status": "accepted"},
    ]
}
ACCESS_EVENTS = [
    {
        "event_type": "DOWNLOAD",
        "created_by": {"login": "vendor@example.com"},
        "source": {"item_id": "X", "item_name": "x.pdf", "owned_by": {"login": "ownerA@internal.example"}},
        "additional_details": {"size": 10, "shared_link_id": "lnk"},
    },
    {
        "event_type": "PREVIEW",
        "created_by": {"login": "u@internal.example"},
        "source": {"item_id": "X", "item_name": "x.pdf", "owned_by": {"login": "ownerA@internal.example"}},
        "additional_details": {"size": 5},
    },
]


def _call(tool):
    return getattr(tool, "fn", tool)


def _router():
    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/2.0/events":
            # one page of access events then empty
            if request.url.params.get("stream_position"):
                return httpx.Response(200, json={"entries": [], "next_stream_position": "p1"})
            return httpx.Response(200, json={"entries": ACCESS_EVENTS, "next_stream_position": "p1"})
        if path == "/2.0/folders/0/items":
            return httpx.Response(200, json=ROOT_ITEMS)
        if path == "/2.0/folders/F1/collaborations":
            return httpx.Response(200, json=F1_COLLABS)
        return httpx.Response(200, json={"entries": []})

    r = respx.mock(assert_all_called=False)
    r.route(host="api.box.com").mock(side_effect=handler)
    return r


def test_daily_brief_combines_access_and_exposure(monkeypatch):
    monkeypatch.setenv("BOX_ALLOWED_DOMAINS", "internal.example")
    with _router():
        out = _call(server.daily_brief)(since_hours=24, max_depth=1, top=5)
    assert out["window_hours"] == 24
    # access: vendor external DL counted, internal preview not
    acc = out["access"]
    assert acc["external_access_count"] == 1
    assert acc["via_shared_link"] == 1
    assert acc["top_external_accessors"][0]["accessor"] == "vendor@example.com"
    # exposure: F1 has an external collaborator + an open link
    exp = out["exposure"]
    assert exp["external_collaborations_count"] == 1
    assert exp["public_shared_links_count"] == 1  # F1 open link
    assert exp["top_external_sharers"][0]["owner"] == "ownerA@internal.example"
    assert exp["fetch_errors"] == 0  # clean run: every folder fetched


def test_daily_brief_missing_env(monkeypatch):
    monkeypatch.delenv("BOX_CLIENT_ID", raising=False)
    out = _call(server.daily_brief)()
    assert "error" in out and "BOX_CLIENT_ID" in out["error"]
