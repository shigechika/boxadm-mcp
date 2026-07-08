"""Tests for BoxOAuthClient (token cache, refresh, rotation, 401 retry, cross-process lock)."""

import json
import os
import stat
import threading
import time
import urllib.parse
from contextlib import contextmanager

import httpx
import respx

import boxadm_mcp.client as client_mod
from boxadm_mcp.client import BoxAuthError, BoxNotAuthenticatedError, BoxOAuthClient, cache_lock, write_token_cache
from tests.conftest import EVENTS_URL, TOKEN_URL


def test_write_token_cache_is_0600_and_atomic(tmp_path):
    path = tmp_path / "sub" / "token.json"  # parent dir is created
    write_token_cache(str(path), {"access_token": "a", "refresh_token": "r", "access_expires_at": 123})
    assert json.loads(path.read_text())["refresh_token"] == "r"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not (tmp_path / "sub" / "token.json.tmp").exists()  # temp cleaned up by replace


def _write_cache(path, *, access="cached-access", refresh="refresh-1", expires_in=3600):
    path.write_text(json.dumps({"access_token": access, "refresh_token": refresh, "access_expires_at": int(time.time()) + expires_in}))


def _client(path):
    return BoxOAuthClient("cid", "secret", token_cache=str(path), api_base="https://api.box.com")


def test_no_cache_raises_needs_login(tmp_path):
    c = _client(tmp_path / "token.json")  # file absent
    try:
        c.authenticate()
        raised = False
    except BoxNotAuthenticatedError:
        raised = True
    assert raised


def test_valid_cached_access_is_used_without_refresh(tmp_path):
    cache = tmp_path / "token.json"
    _write_cache(cache, access="good", expires_in=3600)
    with respx.mock(assert_all_called=False) as r:
        token_route = r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={}))
        events_route = r.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"entries": []}))
        c = _client(cache)
        c.get_admin_events(limit=1)
    assert token_route.call_count == 0  # cached access still valid → no token call
    assert events_route.call_count == 1


def test_expired_access_refreshes_and_rotates_refresh_token(tmp_path):
    cache = tmp_path / "token.json"
    _write_cache(cache, access="stale", refresh="refresh-1", expires_in=-10)  # already expired
    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "fresh", "refresh_token": "refresh-2", "expires_in": 3600}))
        r.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"entries": []}))
        c = _client(cache)
        c.get_admin_events(limit=1)
    saved = json.loads(cache.read_text())
    assert saved["access_token"] == "fresh"
    assert saved["refresh_token"] == "refresh-2"  # rotated refresh token persisted


def test_refresh_failure_raises_needs_login(tmp_path):
    cache = tmp_path / "token.json"
    _write_cache(cache, access="stale", refresh="dead", expires_in=-10)
    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        c = _client(cache)
        try:
            c.authenticate()
            raised = False
        except BoxNotAuthenticatedError:
            raised = True
    assert raised


def test_cache_lock_creates_0600_sidecar(tmp_path):
    path = tmp_path / "sub" / "token.json"  # parent dir is created
    with cache_lock(str(path)):
        pass
    lock = tmp_path / "sub" / "token.json.lock"
    assert lock.exists()
    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600


def test_concurrent_refresh_is_single_flight(tmp_path):
    """Two clients sharing one cache must never both refresh (Box revokes the
    chain on refresh-token reuse). Whoever wins the lock refreshes; the loser
    re-reads the cache inside the lock and adopts the fresh token instead."""
    cache = tmp_path / "token.json"
    _write_cache(cache, access="stale", refresh="refresh-1", expires_in=-10)
    a, b = _client(cache), _client(cache)
    results: dict = {}
    a_holds_lock = threading.Event()  # A is provably inside its (locked) refresh

    def slow_token_response(request):
        a_holds_lock.set()
        time.sleep(0.5)  # keep the lock held while B contends on it
        return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "refresh-2", "expires_in": 3600})

    with respx.mock(assert_all_called=False) as r:
        token_route = r.post(TOKEN_URL).mock(side_effect=slow_token_response)
        ta = threading.Thread(target=lambda: results.update(a=a._ensure_token()))
        tb = threading.Thread(target=lambda: results.update(b=b._ensure_token()))
        ta.start()
        assert a_holds_lock.wait(5)  # B starts only once A demonstrably holds the lock
        tb.start()
        ta.join()
        tb.join()
    assert results["a"] == "fresh"
    assert results["b"] == "fresh"
    assert token_route.call_count == 1  # single flight: the loser adopted, not refreshed
    assert json.loads(cache.read_text())["refresh_token"] == "refresh-2"


def test_slow_path_rereads_refresh_token_inside_lock(tmp_path, monkeypatch):
    """The refresh token must be read AFTER acquiring the lock: another process
    may rotate it while we wait. Presenting the pre-lock token would trip Box's
    reuse detection and revoke the chain. (Kills the read-before-lock mutant.)"""
    cache = tmp_path / "token.json"
    _write_cache(cache, access="stale", refresh="refresh-1", expires_in=-10)
    real_lock = client_mod.cache_lock

    @contextmanager
    def lock_with_interleaved_rotation(path):
        with real_lock(path):
            # Simulate another process having refreshed while we waited for the
            # lock: the refresh token rotated, the access token is expired again.
            _write_cache(cache, access="stale2", refresh="refresh-2", expires_in=-10)
            yield

    monkeypatch.setattr(client_mod, "cache_lock", lock_with_interleaved_rotation)

    def token_response(request):
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        if form["refresh_token"] == "refresh-2":
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "refresh-3", "expires_in": 3600})
        return httpx.Response(400, json={"error": "invalid_grant"})  # rotated-token reuse

    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(side_effect=token_response)
        c = _client(cache)
        assert c._ensure_token() == "fresh"
    assert json.loads(cache.read_text())["refresh_token"] == "refresh-3"


def test_login_cache_write_waits_for_lock(tmp_path):
    """oauth.py's interactive-login write must take the same cross-process lock
    as the refresh path (no interleaving with a concurrent rotation)."""
    from boxadm_mcp.oauth import _write_cache as login_write

    cache = str(tmp_path / "token.json")
    done = threading.Event()

    def writer():
        login_write(cache, {"access_token": "a", "refresh_token": "r", "expires_in": 10})
        done.set()

    with cache_lock(cache):
        t = threading.Thread(target=writer)
        t.start()
        assert not done.wait(0.3)  # blocked while we hold the lock
    assert done.wait(5)  # completes once released
    t.join()
    assert json.loads(open(cache).read())["refresh_token"] == "r"


def test_cache_lock_failure_raises_box_error(tmp_path):
    """Lock acquisition failures must surface as BoxAuthError (structured error
    contract of health_check / tool handlers), not a raw OSError."""
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    os.chmod(ro_dir, 0o555)  # .lock cannot be created here
    try:
        raised = None
        try:
            with cache_lock(str(ro_dir / "token.json")):
                pass
        except BoxAuthError as e:
            raised = e
        assert raised is not None and "lock" in str(raised)
    finally:
        os.chmod(ro_dir, 0o755)


def test_refresh_5xx_is_transient_not_needs_login(tmp_path):
    """A 5xx from the token endpoint does not consume the refresh token — it must
    surface as transient BoxAuthError, not steer the operator to re-login."""
    cache = tmp_path / "token.json"
    _write_cache(cache, access="stale", refresh="refresh-1", expires_in=-10)
    with respx.mock(assert_all_called=False) as r:
        r.post(TOKEN_URL).mock(return_value=httpx.Response(503, json={"error": "unavailable"}))
        c = _client(cache)
        raised = None
        try:
            c.authenticate()
        except BoxNotAuthenticatedError as e:
            raised = ("needs-login", e)
        except BoxAuthError as e:
            raised = ("transient", e)
    assert raised is not None and raised[0] == "transient"
    assert json.loads(cache.read_text())["refresh_token"] == "refresh-1"  # not consumed, retry-able


def test_force_refresh_adopts_other_process_rotation(tmp_path):
    """A 401 on a token another process already replaced must adopt the on-disk
    token instead of burning the (single-use) refresh token again."""
    cache = tmp_path / "token.json"
    _write_cache(cache, access="newer", refresh="refresh-2", expires_in=3600)
    c = _client(cache)
    c._access, c._access_deadline = "old-401", time.time() + 3600
    with respx.mock(assert_all_called=False) as r:
        token_route = r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={}))
        c._force_refresh()
    assert token_route.call_count == 0
    assert c._access == "newer"


def test_get_retries_once_on_401_with_forced_refresh(tmp_path):
    cache = tmp_path / "token.json"
    _write_cache(cache, access="good", refresh="refresh-1", expires_in=3600)  # access looks valid...
    with respx.mock(assert_all_called=False) as r:
        token_route = r.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "fresh", "refresh_token": "refresh-2", "expires_in": 3600})
        )
        events_route = r.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(401, json={"code": "unauthorized"}),  # ...but server says revoked
                httpx.Response(200, json={"entries": [{"event_id": "x"}]}),
            ]
        )
        c = _client(cache)
        out = c.get_admin_events(limit=1)
    assert out["entries"][0]["event_id"] == "x"
    assert events_route.call_count == 2
    assert token_route.call_count == 1  # forced refresh after the 401
