"""Tests for the Box CCG client (token flow, caching, events params)."""

import httpx
import respx

from boxadm_mcp.client import BoxAuthError, BoxClient
from tests.conftest import EVENTS_URL, TOKEN_URL, make_router


def _client():
    return BoxClient("cid", "secret", "12345", api_base="https://api.box.com")


def test_authenticate_requests_token_with_enterprise_subject():
    r = make_router()
    with r:
        c = _client()
        assert c.authenticate() is True
        body = r.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "box_subject_type=enterprise" in body
    assert "box_subject_id=12345" in body


def test_token_is_cached_across_calls():
    """A cached, unexpired token must not trigger a second token request."""
    with respx.mock(assert_all_called=False) as r:
        token_route = r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        events_route = r.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"entries": []}))
        c = _client()
        c.get_admin_events(limit=1)
        c.get_admin_events(limit=1)
    assert token_route.call_count == 1  # token fetched once, reused
    assert events_route.call_count == 2


def test_events_sends_bearer_and_admin_logs_params():
    r = make_router()
    with r:
        c = _client()
        c.get_admin_events(event_types=["DOWNLOAD", "PREVIEW"], created_after="2026-06-01T00:00:00+09:00")
        get = next(call.request for call in r.calls if call.request.method == "GET")
        assert get.headers["Authorization"] == "Bearer tok-abc"
        assert get.url.params["stream_type"] == "admin_logs"
        assert get.url.params["event_type"] == "DOWNLOAD,PREVIEW"
        assert get.url.params["created_after"] == "2026-06-01T00:00:00+09:00"


def test_bad_token_raises_auth_error():
    r = respx.mock(assert_all_called=False)
    r.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    with r:
        c = _client()
        try:
            c.authenticate()
            raised = False
        except BoxAuthError:
            raised = True
    assert raised


def test_token_200_without_access_token_raises_auth_error():
    """A 200 with an unexpected body must surface as BoxAuthError, not KeyError."""
    r = respx.mock(assert_all_called=False)
    r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
    with r:
        c = _client()
        try:
            c.authenticate()
            raised = False
        except BoxAuthError:
            raised = True
    assert raised


def test_get_retries_once_on_401_with_fresh_token():
    """A 401 (token revoked early) triggers one re-auth + retry, then succeeds."""
    with respx.mock(assert_all_called=False) as r:
        token_route = r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        events_route = r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(401, json={"code": "unauthorized"}),
                httpx.Response(200, json={"entries": [{"event_id": "x"}]}),
            ]
        )
        c = _client()
        out = c.get_admin_events(limit=1)
    assert out["entries"][0]["event_id"] == "x"
    assert events_route.call_count == 2  # first 401, then retried
    assert token_route.call_count == 2  # token refreshed after the 401
