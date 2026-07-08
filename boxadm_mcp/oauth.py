"""Interactive OAuth 2.0 login — writes the token cache used by BoxOAuthClient.

Run once via ``boxadm-mcp auth``: opens the Box authorize URL, captures the
redirect on the local callback, exchanges the code for tokens, and persists the
refresh token (chmod 600). After this, the MCP server refreshes unattended.
"""

import http.server
import time
import urllib.parse
import webbrowser

import httpx

from boxadm_mcp.client import DEFAULT_API_BASE, TOKEN_PATH, cache_lock, default_token_cache, save_token_cache

AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize"
DEFAULT_REDIRECT_URI = "http://localhost:8787/callback"


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Construct the Box OAuth 2.0 authorize URL (pure, for testing)."""
    return (
        AUTHORIZE_URL + "?" + urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state})
    )


def _write_cache(token_cache: str, body: dict) -> None:
    # Take the same cross-process lock as BoxOAuthClient's refresh path, so an
    # interactive re-login can't interleave with a concurrent refresh rotation.
    with cache_lock(token_cache):
        save_token_cache(token_cache, body["access_token"], body["refresh_token"], int(body.get("expires_in", 3600)))


def login(
    client_id: str,
    client_secret: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    token_cache: str | None = None,
    api_base: str = DEFAULT_API_BASE,
    open_browser: bool = True,
) -> str:
    """Run the authorization-code flow and write the token cache. Returns its path."""
    token_cache = token_cache or default_token_cache()
    state = "boxadm-" + str(int(time.time()))
    authorize = build_authorize_url(client_id, redirect_uri, state)

    parsed = urllib.parse.urlparse(redirect_uri)
    host, port, cb_path = parsed.hostname or "localhost", parsed.port or 80, parsed.path or "/"
    captured: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path)
            if q.path != cb_path:
                self.send_response(404)
                self.end_headers()
                return
            p = urllib.parse.parse_qs(q.query)
            captured["code"] = p.get("code", [None])[0]
            captured["error"] = p.get("error", [None])[0]
            captured["state"] = p.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization complete. You can close this tab and return to the terminal.")

        def log_message(self, *a):
            pass

    print("Open this URL in your browser to authorize:\n" + authorize, flush=True)
    if open_browser:
        try:
            webbrowser.open(authorize)
        except Exception:
            pass

    srv = http.server.HTTPServer((host, port), Handler)
    srv.timeout = 600
    while "code" not in captured and "error" not in captured:
        srv.handle_request()

    if captured.get("error") or not captured.get("code"):
        raise RuntimeError(f"authorization failed: {captured.get('error')}")
    if captured.get("state") != state:
        raise RuntimeError("state mismatch — possible CSRF; aborting")

    data = {
        "grant_type": "authorization_code",
        "code": captured["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    resp = httpx.post(api_base.rstrip("/") + TOKEN_PATH, data=data, timeout=30)
    resp.raise_for_status()
    _write_cache(token_cache, resp.json())
    return token_cache
