"""Tests for the per-user lookup tool (get_user).

The tool wraps a Box endpoint whose contract is easy to misread: ``filter_term``
is a PREFIX SEARCH over display name and login, so the response is a candidate
set, not an answer. Nearly every test here guards the same class of regression —
a fuzzy hit being reported as the account that was asked about.
"""

import json
import time

import httpx
import pytest
import respx

from boxadm_mcp import server
from tests.conftest import TOKEN_URL, USERS_URL


def _call(tool):
    """FastMCP wraps functions; call the underlying fn."""
    return getattr(tool, "fn", tool)


#: The account under test, and two hits Box's prefix search returns alongside it:
#: a colleague whose DISPLAY NAME shares the prefix, and a longer login that
#: starts with the same characters. Both are the wrong answer.
ASKED_FOR = "taro@example.com"
EXACT_ENTRY = {
    "type": "user",
    "id": "1001",
    "name": "Taro Example",
    "login": ASKED_FOR,
    "status": "inactive",
    "role": "user",
    "space_used": 12,
    "space_amount": 100,
}
NAME_HIT = {"type": "user", "id": "1002", "name": "Taro Other", "login": "hanako@example.com", "status": "active"}
PREFIX_HIT = {"type": "user", "id": "1003", "name": "Taro Prefix", "login": "taro2@example.com", "status": "active"}


def _users_router(body, *, status=200):
    """respx router: CCG token + one canned ``GET /2.0/users`` response."""
    r = respx.mock(assert_all_called=False)
    r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    route = r.get(USERS_URL).mock(return_value=httpx.Response(status, json=body))
    return r, route


def _page(entries):
    return {"total_count": len(entries), "entries": entries, "limit": 100, "offset": 0}


def test_get_user_returns_the_exact_login_not_the_first_hit():
    """Guards the central regression: a prefix hit reported as the requested account.

    Box returns the display-name match first here, so a tool that trusted
    ``entries[0]`` would answer about a different person — with a status field
    that reads as authoritative. Only the exact login may land in ``user``.
    """
    r, _ = _users_router(_page([NAME_HIT, EXACT_ENTRY, PREFIX_HIT]))
    with r:
        out = _call(server.get_user)(login=ASKED_FOR)
    assert out["found"] is True
    assert out["user"]["login"] == ASKED_FOR
    assert out["user"]["status"] == "inactive"  # the answer the ticket needs


def test_get_user_reports_not_found_instead_of_a_fuzzy_hit():
    """Guards against a confident wrong answer when the login does not exist.

    The search still returns rows (they share the prefix), so "the response was
    non-empty" must not be read as "found": ``found`` is false, ``user`` is null,
    and the hits are only counted.
    """
    r, _ = _users_router(_page([NAME_HIT, PREFIX_HIT]))
    with r:
        out = _call(server.get_user)(login=ASKED_FOR)
    assert out["found"] is False
    assert out["user"] is None
    assert "No account carries this exact login" in out["note"]
    assert out["other_prefix_hits"] == 2


def test_get_user_never_identifies_a_non_exact_hit():
    """Guards the enumeration hole this tool's first draft shipped with.

    ``filter_term`` prefix-matches display name AND login, so unrelated colleagues
    land in the result set. Identifying them returned a page of the enterprise user
    directory (a one-character login produced 100 real accounts) while the docs said
    enumeration was impossible. Non-exact hits are counted and never named.
    """
    r, _ = _users_router(_page([NAME_HIT, PREFIX_HIT]))
    with r:
        out = _call(server.get_user)(login=ASKED_FOR)
    assert out["found"] is False
    assert out["other_prefix_hits"] == 2
    blob = json.dumps(out, ensure_ascii=False)
    for stranger in ("hanako@example.com", "taro2@example.com", "Taro Other", "Taro Prefix", "1002", "1003"):
        assert stranger not in blob


@pytest.mark.parametrize("bad", ["a", "Taro", "taro", "  taro  ", "@", "taro@"])
def test_get_user_refuses_a_term_that_is_not_a_login(bad):
    """Guards the same hole at the input: the request itself is the leak.

    Box has no minimum length for ``filter_term``, so a display name or a single
    character is a prefix search over the whole directory. Refusing before the call
    means the strangers' records are never fetched at all.
    """
    r, route = _users_router(_page([]))
    with r:
        out = _call(server.get_user)(login=bad)
    assert "error" in out
    assert "found" not in out
    assert not route.called  # never reached Box


@pytest.mark.parametrize(
    "asked, stored",
    [
        ("TARO@Example.COM", ASKED_FOR),  # caller typed it capitalised
        (ASKED_FOR, "Taro@Example.com"),  # Box echoes the casing it stored
        ("  taro@example.com  ", ASKED_FOR),  # padding from a copy-pasted ticket
    ],
)
def test_get_user_match_is_case_insensitive_and_trimmed(asked, stored):
    """Guards against a case or whitespace difference reading as "no such account".

    Logins are case-insensitive addresses and arrive pasted out of tickets, so an
    exact-match filter that compared raw strings would turn a real account into a
    clean-looking not-found — the failure mode the exact matching was added to avoid.
    """
    r, _ = _users_router(_page([{**EXACT_ENTRY, "login": stored}]))
    with r:
        out = _call(server.get_user)(login=asked)
    assert out["found"] is True
    assert out["requested_login"] == asked.strip()


def test_get_user_requests_filter_term_and_the_explicit_field_list():
    """Guards the request itself: the status/quota fields must be asked for.

    Box answers ``GET /users`` with id/type/name/login only unless ``fields`` says
    otherwise, so dropping the list would return a well-formed user object that
    silently lacks ``status`` — the one field the tool exists to report.
    """
    r, route = _users_router(_page([EXACT_ENTRY]))
    with r:
        _call(server.get_user)(login=ASKED_FOR)
        # Inside the block on purpose: respx rolls a pre-registered route's call
        # log back on exit, so the request is only inspectable while mocking.
        params = route.calls.last.request.url.params
        assert params["filter_term"] == ASKED_FOR
        requested = set(params["fields"].split(","))
        assert {"status", "role", "enterprise", "space_used", "space_amount"} <= requested
        # user_type is sent explicitly because Box does not document what omitting
        # it means. An account that LEFT the enterprise (the tool's headline
        # diagnostic: "enterprise absent") is an EXTERNAL user, so a narrower value
        # would answer "no such account" about a person who plainly exists. It
        # cannot widen the search: Box returns an external user only on a COMPLETE
        # login match, which is already this tool's contract.
        assert params["user_type"] == "all"


def test_get_user_refuses_an_empty_login_without_calling_box():
    """Guards against the enumeration an empty ``filter_term`` would trigger.

    Box treats an empty term as "no filter" and answers with a page of the
    directory, so a blank argument from an LLM must be refused before the request
    goes out, not filtered afterwards.
    """
    r, route = _users_router(_page([]))
    with r:
        out = _call(server.get_user)(login="   ")
        assert route.call_count == 0  # never reached Box (asserted before respx rolls the route back)
    assert "error" in out


def test_get_user_discloses_a_truncated_search():
    """Guards the repo-wide rule that partial coverage is never reported as complete.

    A not-found computed over a truncated result set is inconclusive, not negative:
    ``capped`` must say so and the note must not read as "no such account".
    """
    body = {"total_count": 250, "entries": [NAME_HIT, PREFIX_HIT], "limit": 100, "offset": 0}
    r, _ = _users_router(body)
    with r:
        out = _call(server.get_user)(login=ASKED_FOR)
    assert out["capped"] is True
    assert out["found"] is False
    assert "INCONCLUSIVE" in out["note"]


def test_get_user_capped_is_false_on_a_complete_result():
    """Guards against ``capped`` being stuck true, which would make every answer inconclusive."""
    r, _ = _users_router(_page([EXACT_ENTRY]))
    with r:
        out = _call(server.get_user)(login=ASKED_FOR)
    assert out["capped"] is False
    assert out["search_hits"] == 1


def test_get_user_permission_failure_names_the_likely_cause():
    """Guards against a bare HTTP status on the endpoint's known permission failure.

    The 403 observed in production was resolved by granting the 'Manage users'
    application scope and re-authorising, so the hint must name that scope, the
    re-authorisation step (a scope change does not reach refresh-token-minted
    tokens), and the authorising user's role — not leave an operator with
    "HTTP 403".
    """
    r, _ = _users_router({"code": "access_denied_insufficient_permissions"}, status=403)
    with r:
        out = _call(server.get_user)(login=ASKED_FOR)
    assert "HTTP 403" in out["error"]
    assert "role" in out["likely_cause"] and "Scopes" in out["likely_cause"]
    assert "Manage users" in out["likely_cause"]
    assert "re-authorised" in out["likely_cause"]
    assert "found" not in out  # a permission failure is not a statement about the account


def test_get_user_hint_is_only_for_permission_failures():
    """Guards against a throttle or outage being explained away as a permissions problem."""
    assert server._user_lookup_hint("HTTP 403: GET /users") is not None
    assert server._user_lookup_hint("HTTP 401: GET /users") is not None
    assert server._user_lookup_hint("HTTP 429: GET /users") is None
    assert server._user_lookup_hint("connection error: GET /users: timed out") is None


def test_get_user_missing_env_returns_error(monkeypatch):
    """Guards the shared contract that a missing env var is a structured error, not a traceback."""
    monkeypatch.delenv("BOX_CLIENT_ID", raising=False)
    out = _call(server.get_user)(login=ASKED_FOR)  # no router: must not reach the network
    assert "BOX_CLIENT_ID" in out["error"]


def test_get_user_expired_oauth_session_surfaces_needs_login(monkeypatch, tmp_path):
    """Guards the fleet-wide convention that an expired OAuth session says so.

    Without the token cache the lookup cannot run, and the actionable answer is
    "run boxadm-mcp auth" — not a Box error, and emphatically not ``found: false``,
    which would report a live account as missing because nobody was logged in.
    """
    monkeypatch.setenv("BOX_AUTH_MODE", "oauth")
    monkeypatch.setenv("BOX_TOKEN_CACHE", str(tmp_path / "absent.json"))
    out = _call(server.get_user)(login=ASKED_FOR)  # no router: must not reach the network
    assert out["error"].startswith("needs-login:")
    assert "boxadm-mcp auth" in out["error"]
    assert "found" not in out  # never a statement about the account


def test_get_user_works_in_oauth_mode(monkeypatch, tmp_path):
    """Guards the one-method-serves-both-clients arrangement.

    The lookup lives on ``_FolderReadMixin`` precisely so CCG and OAuth share it;
    a copy landing on ``BoxClient`` alone would leave the deployed auth mode — oauth —
    without the tool, and every other test here runs under CCG.
    """
    monkeypatch.setenv("BOX_AUTH_MODE", "oauth")
    cache = tmp_path / "token.json"
    cache.write_text(json.dumps({"access_token": "good", "refresh_token": "r", "access_expires_at": int(time.time()) + 3600}))
    monkeypatch.setenv("BOX_TOKEN_CACHE", str(cache))
    with respx.mock(assert_all_called=False) as r:
        token_route = r.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={}))
        r.get(USERS_URL).mock(return_value=httpx.Response(200, json=_page([EXACT_ENTRY])))
        out = _call(server.get_user)(login=ASKED_FOR)
    assert out["found"] is True
    assert token_route.call_count == 0  # the cached access token was reused, not re-minted
