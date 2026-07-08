"""Shared fixtures for boxadm-mcp tests.

Box has two endpoints we touch: the CCG token endpoint and the 2.0 API. The mock
serves a canned token and a configurable events payload so tests never hit Box.
"""

import os

import httpx
import pytest
import respx

os.environ.setdefault("BOX_CLIENT_ID", "test-client-id")
os.environ.setdefault("BOX_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("BOX_ENTERPRISE_ID", "999999")
os.environ.setdefault("BOX_API_BASE", "https://api.box.com")

TOKEN_URL = "https://api.box.com/oauth2/token"
EVENTS_URL = "https://api.box.com/2.0/events"

SAMPLE_EVENT = {
    "type": "event",
    "event_id": "ev-1",
    "event_type": "COLLABORATION_INVITE",
    "created_at": "2026-06-01T10:00:00+09:00",
    "created_by": {"type": "user", "id": "1", "login": "owner@internal.example"},
    "source": {"type": "user", "id": "2", "login": "outsider@example.com"},
}


def make_router(*, events=None, token="tok-abc", expires_in=3600):
    """respx router: canned CCG token + configurable events response.

    ``events`` overrides the full events JSON body; default is an empty page.
    """
    router = respx.mock(assert_all_called=False)
    router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": token, "expires_in": expires_in}))
    body = events if events is not None else {"chunk_size": 0, "next_stream_position": "0", "entries": []}
    router.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=body))
    return router


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the server's cached client around every test for isolation."""
    from boxadm_mcp import server

    server.reset_client()
    yield
    server.reset_client()
