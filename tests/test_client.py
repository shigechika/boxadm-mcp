"""Tests for the Box CCG client (token flow, caching, events params)."""

import httpx
import respx

import boxadm_mcp.client as client_mod
from boxadm_mcp.client import BoxAuthError, BoxClient, BoxError
from tests.conftest import EVENTS_URL, TOKEN_URL, make_router


def _client():
    return BoxClient("cid", "secret", "12345", api_base="https://api.box.com")


def _stub_sleep(monkeypatch) -> list:
    """Record retry backoff durations without actually waiting (keeps retry tests instant).

    Also freezes ``time.monotonic`` so the per-call retry-time budget depends only on each
    delay's magnitude, never on real elapsed time — a scheduler/GC stall can't fail-fast a
    count-based test one attempt early.
    """
    slept: list = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: 1000.0)
    return slept


def _token_ok(r):
    return r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))


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


# --- read-path retry/backoff on 429 / transient errors (issue #11) ---


def test_get_retries_429_then_succeeds(monkeypatch):
    slept = _stub_sleep(monkeypatch)
    with respx.mock(assert_all_called=False) as r:
        _token_ok(r)
        route = r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(429, json={"code": "rate_limited"}),
                httpx.Response(200, json={"entries": [{"event_id": "ok"}]}),
            ]
        )
        out = _client().get_admin_events(limit=1)
    assert out["entries"][0]["event_id"] == "ok"
    assert route.call_count == 2  # 429, then a successful retry
    assert len(slept) == 1  # one backoff between the two attempts


def test_get_honors_retry_after_header(monkeypatch):
    slept = _stub_sleep(monkeypatch)
    # Pin the de-sync jitter to a fixed 0.5s so the assertion proves the jitter term is ADDED
    # (a no-jitter revert would sleep exactly 3.0, failing the == 3.5 check below).
    monkeypatch.setattr(client_mod.random, "uniform", lambda a, b: 0.5)
    with respx.mock(assert_all_called=False) as r:
        _token_ok(r)
        r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "3"}, json={}),
                httpx.Response(200, json={"entries": []}),
            ]
        )
        _client().get_admin_events(limit=1)
    # Server's Retry-After (3s) honored, plus the small de-sync jitter (0.5s here).
    assert slept == [3.5]


def test_get_retries_transient_5xx_then_succeeds(monkeypatch):
    slept = _stub_sleep(monkeypatch)
    with respx.mock(assert_all_called=False) as r:
        _token_ok(r)
        route = r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(503, json={}),
                httpx.Response(500, json={}),
                httpx.Response(200, json={"entries": []}),
            ]
        )
        _client().get_admin_events(limit=1)
    assert route.call_count == 3
    assert len(slept) == 2


def test_get_exhausts_retries_then_raises(monkeypatch):
    slept = _stub_sleep(monkeypatch)
    with respx.mock(assert_all_called=False) as r:
        _token_ok(r)
        route = r.get(EVENTS_URL).mock(return_value=httpx.Response(429, json={}))
        raised = False
        try:
            _client().get_admin_events(limit=1)
        except BoxError:
            raised = True
    assert raised
    assert route.call_count == 5  # _MAX_ATTEMPTS total attempts
    assert len(slept) == 4  # backoff between attempts, none after the final one


def test_get_403_fails_fast_without_retry(monkeypatch):
    slept = _stub_sleep(monkeypatch)
    with respx.mock(assert_all_called=False) as r:
        _token_ok(r)
        route = r.get(EVENTS_URL).mock(return_value=httpx.Response(403, json={"code": "forbidden"}))
        raised = False
        try:
            _client().get_admin_events(limit=1)
        except BoxError:
            raised = True
    assert raised
    assert route.call_count == 1  # a permission 403 is not retried
    assert slept == []


def test_get_401_reauth_does_not_consume_retry_budget(monkeypatch):
    """A 401 re-auth is free: it must not eat into the 429 backoff budget (Copilot review on #12).

    Under the old for-range(_MAX_ATTEMPTS) loop the leading 401 consumed one iteration, so the
    fourth 429 exhausted the budget and this raised. The 401 retry is now free, so all five
    rate-limit attempts remain and the final 200 succeeds.
    """
    slept = _stub_sleep(monkeypatch)
    with respx.mock(assert_all_called=False) as r:
        _token_ok(r)  # initial token + the _on_401 refresh both succeed
        route = r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(401, json={}),  # triggers a free re-auth, no backoff, no budget spent
                httpx.Response(429, json={}),
                httpx.Response(429, json={}),
                httpx.Response(429, json={}),
                httpx.Response(429, json={}),
                httpx.Response(200, json={"entries": []}),  # 6th request, still within budget
            ]
        )
        _client().get_admin_events(limit=1)
    assert route.call_count == 6  # 401 + four 429 + the success — the 401 cost no 429 slot
    assert len(slept) == 4  # only the four 429s backed off


def test_get_fails_fast_on_oversized_retry_after(monkeypatch):
    """A Retry-After longer than the per-call time budget fails fast instead of under-waiting.

    Box asking for a 3600s wait must not be capped-and-hammered: the delay exceeds
    _MAX_RETRY_ELAPSED, so _get raises immediately without sleeping or retrying (issue #12
    review). The folder then surfaces in fetch_errors rather than the client re-hitting a
    still-throttled endpoint five times.
    """
    slept = _stub_sleep(monkeypatch)
    with respx.mock(assert_all_called=False) as r:
        _token_ok(r)
        route = r.get(EVENTS_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}, json={}))
        raised = False
        try:
            _client().get_admin_events(limit=1)
        except BoxError:
            raised = True
    assert raised
    assert route.call_count == 1  # not retried: the requested wait blows the budget
    assert slept == []  # never slept


def test_get_retries_connection_error_then_succeeds(monkeypatch):
    """A transient connection/transport error is retried (the GET is idempotent), then succeeds."""
    slept = _stub_sleep(monkeypatch)
    with respx.mock(assert_all_called=False) as r:
        _token_ok(r)
        route = r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.ConnectError("connection reset"),
                httpx.Response(200, json={"entries": [{"event_id": "ok"}]}),
            ]
        )
        out = _client().get_admin_events(limit=1)
    assert out["entries"][0]["event_id"] == "ok"
    assert route.call_count == 2  # transport error, then a successful retry
    assert len(slept) == 1


def test_get_second_401_fails_fast(monkeypatch):
    """A 401 that persists after one re-auth fails fast — it must not loop re-authing forever."""
    slept = _stub_sleep(monkeypatch)
    with respx.mock(assert_all_called=False) as r:
        token_route = _token_ok(r)
        route = r.get(EVENTS_URL).mock(return_value=httpx.Response(401, json={"code": "unauthorized"}))
        raised = False
        try:
            _client().get_admin_events(limit=1)
        except BoxError:
            raised = True
    assert raised
    assert route.call_count == 2  # one re-auth retry, then the second 401 fails fast (no infinite loop)
    assert token_route.call_count == 2  # initial token + exactly one _on_401 refresh
    assert slept == []  # a 401 re-auth does not back off
