"""Tests for fetch_admin_events pagination and the external_access_events tool."""

import httpx
import respx

from boxadm_mcp import server
from boxadm_mcp.client import BoxClient, fetch_admin_events
from tests.conftest import EVENTS_URL, TOKEN_URL


def _call(tool):
    return getattr(tool, "fn", tool)


def _ev(event_type, login, item_id, item_name, *, size=0, shared_link=False):
    ad = {"size": size}
    if shared_link:
        ad["shared_link_id"] = "lnk"
    return {
        "event_type": event_type,
        "created_by": {"type": "user", "login": login} if login else {"type": "user", "login": None},
        "source": {"item_id": item_id, "item_name": item_name, "owned_by": {"login": "svc@boxdevedition.com"}},
        "additional_details": ad,
    }


def test_fetch_admin_events_paginates_until_empty():
    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(200, json={"entries": [{"event_type": "DOWNLOAD"}], "next_stream_position": "p1"}),
                httpx.Response(200, json={"entries": [{"event_type": "PREVIEW"}], "next_stream_position": "p2"}),
                httpx.Response(200, json={"entries": [], "next_stream_position": "p2"}),
            ]
        )
        c = BoxClient("cid", "secret", "ent", api_base="https://api.box.com")
        events, capped = fetch_admin_events(c, created_after="2026-06-01T00:00:00Z", event_types=["DOWNLOAD", "PREVIEW"])
    assert [e["event_type"] for e in events] == ["DOWNLOAD", "PREVIEW"]
    assert capped is False


def test_fetch_admin_events_caps_and_flags():
    state = {"n": 0}

    def handler(request):  # unique stream position each page, like the real API
        state["n"] += 1
        return httpx.Response(200, json={"entries": [{"event_type": "DOWNLOAD"}], "next_stream_position": f"p{state['n']}"})

    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        r.get(EVENTS_URL).mock(side_effect=handler)
        c = BoxClient("cid", "secret", "ent", api_base="https://api.box.com")
        events, capped = fetch_admin_events(c, created_after="2026-06-01T00:00:00Z", max_events=3, page_size=1)
    assert len(events) == 3
    assert capped is True  # more existed; not silently truncated


def test_external_access_events_aggregates(monkeypatch):
    monkeypatch.setenv("BOX_ALLOWED_DOMAINS", "example.com")
    page = [
        _ev("DOWNLOAD", "x@gmail.com", "A", "fileA", size=100),  # external
        _ev("DOWNLOAD", "u@example.com", "A", "fileA"),  # internal (ignored)
        _ev("PREVIEW", None, "B", "fileB", size=50, shared_link=True),  # anonymous open-link = external
    ]
    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(200, json={"entries": page, "next_stream_position": "p1"}),
                httpx.Response(200, json={"entries": [], "next_stream_position": "p1"}),
            ]
        )
        out = _call(server.external_access_events)(since_hours=24)
    assert out["events_scanned"] == 3
    assert out["external_access_count"] == 2  # gmail + anonymous
    assert out["via_shared_link"] == 1
    accessors = {a["accessor"] for a in out["top_external_accessors"]}
    assert "x@gmail.com" in accessors
    assert any("anonymous" in a for a in accessors)
    files = {f["item_id"]: f for f in out["top_externally_accessed_files"]}
    assert files["A"]["external_count"] == 1 and files["B"]["external_count"] == 1


def test_fetch_admin_events_actor_filter_spans_pages():
    """A login on page 2 is found, and max_events bounds *scanned*, not kept."""
    page1 = [_ev("DOWNLOAD", "u@example.com", "A", "fileA") for _ in range(2)]
    page2 = [_ev("DOWNLOAD", "target@gmail.com", "B", "fileB", size=1_180_000_000)]
    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(200, json={"entries": page1, "next_stream_position": "p1"}),
                httpx.Response(200, json={"entries": page2, "next_stream_position": "p2"}),
                httpx.Response(200, json={"entries": [], "next_stream_position": "p2"}),
            ]
        )
        c = BoxClient("cid", "secret", "ent", api_base="https://api.box.com")
        events, capped = fetch_admin_events(c, created_after="2026-06-01T00:00:00Z", created_by_logins=["target@gmail.com"])
    # Only the page-2 match is kept, even though it sits behind a full page of others.
    assert [e["source"]["item_id"] for e in events] == ["B"]
    assert capped is False


def test_external_access_events_dlp_tracing(monkeypatch):
    """created_by_logins → matched_events with per-file detail, scoped aggregate."""
    monkeypatch.delenv("BOX_ALLOWED_DOMAINS", raising=False)
    page1 = [
        _ev("DOWNLOAD", "u@example.com", "A", "fileA"),  # internal noise
        _ev("DOWNLOAD", "other@gmail.com", "C", "fileC", size=10),  # different external actor
    ]
    page2 = [
        _ev("DOWNLOAD", "target@gmail.com", "B", "video.mp4", size=1_180_000_000, shared_link=True),
    ]
    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(200, json={"entries": page1, "next_stream_position": "p1"}),
                httpx.Response(200, json={"entries": page2, "next_stream_position": "p2"}),
                httpx.Response(200, json={"entries": [], "next_stream_position": "p2"}),
            ]
        )
        out = _call(server.external_access_events)(since_hours=24, created_by_logins="target@gmail.com")
    assert out["filtered_logins"] == ["target@gmail.com"]
    assert out["events_matched"] == 1  # only the matched accessor's events
    assert "events_scanned" not in out  # DLP mode reports matched, not scanned
    assert out["capped"] is False  # full window scanned (not truncated)
    assert len(out["matched_events"]) == 1
    m = out["matched_events"][0]
    assert m["name"] == "video.mp4"
    assert m["item_id"] == "B"
    assert m["size_bytes"] == 1_180_000_000
    assert m["size_gb"] == 1.18
    assert m["via_shared_link"] is True
    assert m["accessor"] == "target@gmail.com"
    # Aggregate is scoped to the filtered accessor only.
    assert out["external_access_count"] == 1
    assert {a["accessor"] for a in out["top_external_accessors"]} == {"target@gmail.com"}


def test_external_access_events_missing_env(monkeypatch):
    monkeypatch.delenv("BOX_CLIENT_ID", raising=False)
    out = _call(server.external_access_events)()
    assert "error" in out and "BOX_CLIENT_ID" in out["error"]
