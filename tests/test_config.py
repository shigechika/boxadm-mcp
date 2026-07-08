"""Tests for the org domain allowlist and external-actor detection."""

from boxadm_mcp.config import allowed_domains, is_external


def test_default_allowed_domains_is_empty(monkeypatch):
    monkeypatch.delenv("BOX_ALLOWED_DOMAINS", raising=False)
    assert allowed_domains() == []


def test_allowed_domains_from_env(monkeypatch):
    monkeypatch.setenv("BOX_ALLOWED_DOMAINS", "foo.example, BAR.example ")
    assert allowed_domains() == ["foo.example", "bar.example"]


def test_internal_addresses_are_not_external(monkeypatch):
    monkeypatch.setenv("BOX_ALLOWED_DOMAINS", "example.com,g.example.com")
    assert is_external("staff@example.com") is False
    assert is_external("student@g.example.com") is False
    # subdomain of an allowed domain counts as internal
    assert is_external("user@sub.example.com") is False


def test_outside_addresses_are_external(monkeypatch):
    monkeypatch.setenv("BOX_ALLOWED_DOMAINS", "example.com")
    assert is_external("someone@gmail.com") is True
    # a domain that merely *contains* an allowed domain is still external
    assert is_external("a@notexample.com.evil.com") is True


def test_blank_or_malformed_is_external():
    assert is_external("") is True
    assert is_external(None) is True
    assert is_external("no-at-sign") is True


def test_unconfigured_allowlist_treats_everything_as_external(monkeypatch):
    """No BOX_ALLOWED_DOMAINS set -> fail safe (nothing is trusted as internal)."""
    monkeypatch.delenv("BOX_ALLOWED_DOMAINS", raising=False)
    assert is_external("anyone@example.com") is True
