"""Tests for boxadm-mcp tools (health_check, recent_admin_events)."""

import json
import time

import httpx
import respx

from boxadm_mcp import server
from tests.conftest import EVENTS_URL, SAMPLE_EVENT, TOKEN_URL, make_router


def _call(tool):
    """FastMCP wraps functions; call the underlying fn."""
    return getattr(tool, "fn", tool)


# ---- health_check ---------------------------------------------------------


def test_health_check_reports_version_and_backend(monkeypatch):
    from boxadm_mcp import __version__

    monkeypatch.delenv("BOX_ALLOWED_DOMAINS", raising=False)
    with make_router():
        out = _call(server.health_check)()
    assert out["status"] == "healthy"
    assert out["service"] == "boxadm-mcp"
    assert out["version"] == __version__
    assert out["auth"] == "ok"
    assert out["events_accessible"] is True
    assert out["allowed_domains"] == []


def test_health_check_events_denied_is_degraded(monkeypatch):
    """Token OK but the admin_logs scope not granted -> degraded, events_accessible False."""
    monkeypatch.delenv("BOX_ALLOWED_DOMAINS", raising=False)
    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        r.get(EVENTS_URL).mock(return_value=httpx.Response(403, json={"code": "access_denied_insufficient_permissions"}))
        out = _call(server.health_check)()
    assert out["status"] == "degraded"
    assert out["auth"] == "ok"  # token obtained fine
    assert out["events_accessible"] is False
    assert "events not accessible" in out["detail"]


def test_health_check_missing_env_is_error(monkeypatch):
    """A missing connection env var yields status=error, not a crash."""
    monkeypatch.delenv("BOX_CLIENT_ID", raising=False)
    out = _call(server.health_check)()  # no router: must not reach the network
    assert out["status"] == "error"
    assert out["auth"] == "missing-env"
    assert "BOX_CLIENT_ID" in out["detail"]
    assert out["version"]  # version is still reported even when the backend is down
    assert out["events_accessible"] is False


def test_health_check_oauth_mode_healthy(monkeypatch, tmp_path):
    monkeypatch.setenv("BOX_AUTH_MODE", "oauth")
    monkeypatch.delenv("BOX_ALLOWED_DOMAINS", raising=False)
    cache = tmp_path / "token.json"
    cache.write_text(json.dumps({"access_token": "good", "refresh_token": "r", "access_expires_at": int(time.time()) + 3600}))
    monkeypatch.setenv("BOX_TOKEN_CACHE", str(cache))
    with respx.mock(assert_all_called=False) as r:
        r.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"entries": []}))
        out = _call(server.health_check)()
    assert out["auth_mode"] == "oauth"
    assert out["status"] == "healthy"
    assert out["auth"] == "ok"
    assert out["events_accessible"] is True


def test_health_check_oauth_needs_login(monkeypatch, tmp_path):
    """OAuth mode with no token cache reports needs-login (degraded), not a crash."""
    monkeypatch.setenv("BOX_AUTH_MODE", "oauth")
    monkeypatch.setenv("BOX_TOKEN_CACHE", str(tmp_path / "absent.json"))
    out = _call(server.health_check)()
    assert out["auth_mode"] == "oauth"
    assert out["status"] == "degraded"
    assert out["auth"] == "needs-login"
    assert "boxadm-mcp auth" in out["detail"]


def test_recent_admin_events_returns_entries():
    body = {"chunk_size": 1, "next_stream_position": "42", "entries": [SAMPLE_EVENT]}
    with make_router(events=body):
        out = _call(server.recent_admin_events)(event_types="COLLABORATION_INVITE", since_hours=24)
    assert out["count"] == 1
    assert out["next_stream_position"] == "42"
    assert out["events"][0]["event_type"] == "COLLABORATION_INVITE"


def test_recent_admin_events_forwards_stream_position():
    """A passed stream_position continues the page (forwarded to Box query)."""
    body = {"chunk_size": 0, "next_stream_position": "99", "entries": []}
    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        route = r.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=body))
        out = _call(server.recent_admin_events)(since_hours=24, stream_position="42")
    assert out["next_stream_position"] == "99"
    assert route.calls.last.request.url.params["stream_position"] == "42"


def test_recent_admin_events_missing_env_returns_error(monkeypatch):
    monkeypatch.delenv("BOX_CLIENT_ID", raising=False)
    out = _call(server.recent_admin_events)()
    assert "error" in out
    assert "BOX_CLIENT_ID" in out["error"]
