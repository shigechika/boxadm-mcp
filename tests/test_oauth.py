"""Tests for the OAuth login helper (pure parts)."""

from boxadm_mcp.oauth import AUTHORIZE_URL, build_authorize_url


def test_build_authorize_url():
    u = build_authorize_url("cid", "http://localhost:8787/callback", "st8")
    assert u.startswith(AUTHORIZE_URL + "?")
    assert "response_type=code" in u
    assert "client_id=cid" in u
    assert "state=st8" in u
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8787%2Fcallback" in u
