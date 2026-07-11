"""Box Enterprise API clients (read-only).

Two auth modes, same read-only surface (`authenticate()` + `get_admin_events()`):

- ``BoxClient`` — Client Credentials Grant (server-to-server, enterprise subject).
- ``BoxOAuthClient`` — OAuth 2.0 (user auth) with a cached, auto-refreshed token.
  A one-time interactive login (``boxadm-mcp auth``) writes the token cache; the
  client refreshes the access token from the stored refresh token as needed and
  persists the rotated refresh token (Box rotates it on every refresh).

Read-only by design: neither client issues a mutating call. boxadm-mcp surfaces
risk; it never enforces.
"""

import fcntl
import json
import os
import random
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

DEFAULT_TIMEOUT = 30
DEFAULT_API_BASE = "https://api.box.com"
TOKEN_PATH = "/oauth2/token"
API_PREFIX = "/2.0"

# Refresh a token this many seconds before its stated expiry, so a call never
# races a just-expired token.
TOKEN_REFRESH_SKEW = 60

# Read-path retry policy. Box rate-limits at 1000 req/min/user and returns 429 with a
# Retry-After header; it can also return transient 5xx. A read GET is idempotent, so it
# is safe to retry — matching Box's own SDKs (429/5xx, exponential backoff + jitter) and
# gwsadm-mcp's _execute. See issue #11. Without this a transient throttle during the
# parallel _scan() would drop a folder into fetch_errors instead of recovering.
_MAX_RETRIES = 5
_MAX_BACKOFF = 8.0  # seconds; cap on any single backoff (and on an over-large Retry-After)
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse a numeric ``Retry-After`` header (seconds form) if present and non-negative."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        secs = float(raw)  # Box sends the seconds form; an HTTP-date form is ignored (-> backoff)
    except ValueError:
        return None
    return secs if secs >= 0 else None


def _backoff_delay(attempt: int, resp: httpx.Response | None = None) -> float:
    """Server-provided ``Retry-After`` (capped) when available, else full-jitter exponential backoff."""
    if resp is not None:
        after = _retry_after_seconds(resp)
        if after is not None:
            return min(after, _MAX_BACKOFF)
    base = min(2.0**attempt, _MAX_BACKOFF)
    return base + random.uniform(0, base)


class BoxError(Exception):
    """Base error for Box API failures."""


class BoxAuthError(BoxError):
    """Raised when authentication fails (bad creds / app not authorized)."""


class BoxNotAuthenticatedError(BoxAuthError):
    """No usable OAuth token cache — run ``boxadm-mcp auth`` to log in."""


def default_token_cache() -> str:
    """Default OAuth token cache path (override via BOX_TOKEN_CACHE)."""
    return os.path.expanduser("~/.config/boxadm-mcp/token.json")


@contextmanager
def cache_lock(path: str):
    """Exclusive cross-process lock guarding token-cache read/refresh/write.

    Box refresh tokens are single-use and rotate on every refresh; two processes
    sharing one cache file must never refresh concurrently (Box treats reuse of a
    rotated token as compromise and revokes the whole chain). The lock is a
    persistent sidecar ``<cache>.lock`` file rather than the cache itself, because
    ``write_token_cache`` replaces the cache inode (``os.replace``) — a lock held
    on a replaced inode would no longer exclude the next locker.
    """
    lock_path = path + ".lock"
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as e:
        # Wrap into the Box exception hierarchy so callers (health_check, tool
        # handlers) keep their "structured error, never a raw traceback" contract.
        # Typical trigger: the sidecar was created by the wrong user (e.g. `auth`
        # run without sudo -u mcp) — fix ownership or remove the .lock file.
        raise BoxAuthError(f"token cache lock unavailable: {lock_path}: {e}") from e
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            raise BoxAuthError(f"token cache lock failed: {lock_path}: {e}") from e
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


def write_token_cache(path: str, data: dict) -> None:
    """Write the token cache atomically and owner-only (0600).

    Created at 0600 from the start (no world-readable window), written to a temp
    file then ``os.replace``d in, so a crash mid-write can't corrupt the cache and
    trigger a spurious re-login.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, p)  # atomic; the 0600 temp inode becomes the cache


def save_token_cache(path: str, access: str, refresh: str, expires_in: int) -> None:
    """Persist a token-endpoint result. Single source of truth for the cache schema."""
    write_token_cache(
        path,
        {"access_token": access, "refresh_token": refresh, "access_expires_at": int(time.time()) + int(expires_in)},
    )


def _events_params(
    created_after: str | None,
    created_before: str | None,
    event_types: list[str] | None,
    stream_position: str | int | None,
    limit: int,
) -> dict:
    """Build query params for an enterprise ``admin_logs`` events page."""
    params: dict = {"stream_type": "admin_logs", "limit": limit}
    if created_after:
        params["created_after"] = created_after
    if created_before:
        params["created_before"] = created_before
    if event_types:
        params["event_type"] = ",".join(event_types)
    if stream_position is not None:
        params["stream_position"] = stream_position
    return params


def fetch_admin_events(
    client,
    *,
    created_after: str | None = None,
    created_before: str | None = None,
    event_types: list[str] | None = None,
    max_events: int = 5000,
    page_size: int = 500,
    created_by_logins: list[str] | None = None,
) -> tuple[list[dict], bool]:
    """Page through ``admin_logs`` and collect events from the stream.

    Works with either client (both expose ``get_admin_events``). Returns
    ``(events, capped)`` where ``capped`` is True when the ``max_events`` cap was
    hit before the stream was exhausted — so callers can disclose truncation
    rather than silently under-report.

    ``max_events`` bounds the number of events *scanned* from the stream (the
    volume-safety limit), not the number kept. When ``created_by_logins`` is set,
    only events whose ``created_by.login`` is in that set are kept (a client-side
    filter — Box admin_logs has no ``created_by`` query param), so a specific
    accessor can be traced across the whole window while API work stays bounded.
    With no filter, kept == scanned and the behaviour is unchanged.
    """
    logins = set(created_by_logins) if created_by_logins else None
    out: list[dict] = []
    pos: str | int | None = None
    scanned = 0
    capped = False
    while True:
        if scanned >= max_events:
            capped = True
            break
        resp = client.get_admin_events(
            created_after=created_after,
            created_before=created_before,
            event_types=event_types,
            stream_position=pos,
            limit=min(page_size, max_events - scanned),
        )
        entries = resp.get("entries", [])
        scanned += len(entries)
        if logins is None:
            out.extend(entries)
        else:
            out.extend(e for e in entries if ((e.get("created_by") or {}).get("login")) in logins)
        nxt = resp.get("next_stream_position")
        if not entries or nxt is None or str(nxt) == str(pos):
            break
        pos = nxt
    return out[:max_events], capped


class _FolderReadMixin:
    """Read-only folder / collaboration / shared-link getters shared by both clients.

    The subclass provides ``_http`` (an ``httpx.Client``), ``_base``, ``_ensure_token()``
    and ``_on_401()`` (its token-refresh action when a 401 comes back). This mixin supplies
    the shared authenticated GET with retry/backoff on 429 / transient errors.
    """

    def _get(self, path: str, params: dict | None = None) -> dict:
        """Authenticated GET with a 401 re-auth retry and 429 / transient-error backoff.

        On 401 the subclass's ``_on_401()`` refreshes the token once and the request is
        retried immediately — this re-auth is *free*: it does not consume the backoff budget
        (``attempt`` is only advanced by a 429 / 5xx / connection retry), so a 401 that lands
        on the last rate-limit attempt still gets its retry. On 429 / 5xx / a connection error
        the request is retried up to ``_MAX_RETRIES`` times, honoring ``Retry-After`` when
        present else full-jitter exponential backoff. A permission 403 / 404 (and a second
        401) fails fast. When retries are exhausted the last failure is raised as ``BoxError``
        (so callers still see it — e.g. _scan surfaces it in ``fetch_errors`` — rather than a
        transient throttle being masked).
        """
        reauthed = False
        attempt = 0
        while True:
            token = self._ensure_token()
            try:
                resp = self._http.get(
                    self._base + API_PREFIX + path,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 401 and not reauthed:
                    # Token rejected before its deadline: refresh once, retry immediately.
                    # Not counted against the rate-limit backoff budget (attempt unchanged).
                    self._on_401()
                    reauthed = True
                    continue
                if status in _RETRYABLE_STATUS and attempt + 1 < _MAX_RETRIES:
                    time.sleep(_backoff_delay(attempt, e.response))
                    attempt += 1
                    continue
                raise BoxError(f"HTTP {status}: GET {path}") from e
            except httpx.HTTPError as e:
                # Connection / timeout error: transient, and the GET is idempotent.
                if attempt + 1 < _MAX_RETRIES:
                    time.sleep(_backoff_delay(attempt))
                    attempt += 1
                    continue
                raise BoxError(f"connection error: GET {path}: {e}") from e

    def get_folder(self, folder_id: str, *, fields: list[str] | None = None) -> dict:
        return self._get(f"/folders/{folder_id}", {"fields": ",".join(fields)} if fields else None)

    def get_folder_items(self, folder_id: str, *, fields: list[str] | None = None, limit: int = 1000, offset: int = 0) -> dict:
        params: dict = {"limit": limit, "offset": offset}
        if fields:
            params["fields"] = ",".join(fields)
        return self._get(f"/folders/{folder_id}/items", params)

    def get_folder_collaborations(self, folder_id: str) -> dict:
        return self._get(f"/folders/{folder_id}/collaborations")


class BoxClient(_FolderReadMixin):
    """Read-only Box enterprise client using the CCG flow."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        enterprise_id: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._enterprise_id = enterprise_id
        self._base = api_base.rstrip("/")
        self._http = httpx.Client(timeout=timeout)
        self._token: str | None = None
        self._token_deadline = 0.0  # monotonic clock deadline

    def _ensure_token(self) -> str:
        # Snapshot BOTH the token and its deadline before the guard/return:
        # server.py's _scan drives _get from many worker threads at once, and a
        # concurrent 401 handler may reset self._token to None (and its deadline to
        # 0.0) between this check and the return. Reading locals means the guard
        # judges one consistent (token, deadline) pair and can never return a token
        # another thread just nulled — no "Bearer None" request. The two reads are
        # still not atomic, but the only residual is a benign redundant fetch, or a
        # stale token that self-heals via _get's 401 retry — never a bad token
        # presented as fresh, and never the OAuth single-use-refresh class (CCG has
        # no refresh token).
        token, deadline = self._token, self._token_deadline
        if token and time.monotonic() < deadline:
            return token
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "box_subject_type": "enterprise",
            "box_subject_id": self._enterprise_id,
        }
        try:
            resp = self._http.post(self._base + TOKEN_PATH, data=data)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as e:
            raise BoxAuthError(f"token request failed: HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise BoxAuthError(f"token request error: {e}") from e
        try:
            token = body["access_token"]
        except (KeyError, TypeError) as e:
            raise BoxAuthError("token response missing access_token") from e
        self._token = token
        self._token_deadline = time.monotonic() + max(0, int(body.get("expires_in", 3600)) - TOKEN_REFRESH_SKEW)
        return token

    def authenticate(self) -> bool:
        """Obtain (or refresh) the CCG token. Returns True on success, else raises."""
        self._ensure_token()
        return True

    def _on_401(self) -> None:
        # CCG: drop the rejected token so the shared _get's next attempt mints a fresh one.
        self._token = None
        self._token_deadline = 0.0

    @property
    def enterprise_id(self) -> str:
        return self._enterprise_id

    def get_admin_events(
        self,
        *,
        created_after: str | None = None,
        created_before: str | None = None,
        event_types: list[str] | None = None,
        stream_position: str | int | None = None,
        limit: int = 500,
    ) -> dict:
        """Fetch one page of enterprise ``admin_logs`` events (raw Box response)."""
        return self._get("/events", _events_params(created_after, created_before, event_types, stream_position, limit))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BoxClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class BoxOAuthClient(_FolderReadMixin):
    """Read-only Box client using OAuth 2.0 (user auth) with an auto-refreshed token cache."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        token_cache: str | None = None,
        api_base: str = DEFAULT_API_BASE,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._cache_path = token_cache or default_token_cache()
        self._base = api_base.rstrip("/")
        self._http = httpx.Client(timeout=timeout)
        self._access: str | None = None
        self._access_deadline = 0.0  # unix time (persisted across restarts)

    # ------------------------------------------------------------------
    # Token cache
    # ------------------------------------------------------------------
    def _load_cache(self) -> dict:
        try:
            with open(self._cache_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_cache(self, access: str, refresh: str, expires_in: int) -> None:
        save_token_cache(self._cache_path, access, refresh, expires_in)

    # ------------------------------------------------------------------
    # Auth (refresh-token grant)
    # ------------------------------------------------------------------
    def _adopt_valid_access(self, cache: dict) -> str | None:
        """Adopt the cached access token if it is still comfortably valid."""
        access = cache.get("access_token")
        expires_at = cache.get("access_expires_at", 0)
        if access and time.time() < expires_at - TOKEN_REFRESH_SKEW:
            self._access, self._access_deadline = access, expires_at
            return access
        return None

    def _ensure_token(self) -> str:
        if self._access and time.time() < self._access_deadline - TOKEN_REFRESH_SKEW:
            return self._access
        # Unlocked disk fast path: right after another process refreshed, adopt
        # its result without serializing every reader through the lock. A miss
        # (or a torn read) just falls through to the locked slow path.
        access = self._adopt_valid_access(self._load_cache())
        if access:
            return access
        # Slow path: refresh under the cross-process lock. Another process may
        # win the race and rotate the refresh token while we wait for the lock,
        # so re-read the cache once inside it — if a fresh access token appeared,
        # use it and skip our own refresh (refreshing with the pre-lock token
        # would present an already-rotated token and revoke the chain).
        with cache_lock(self._cache_path):
            cache = self._load_cache()
            access = self._adopt_valid_access(cache)
            if access:
                return access
            refresh = cache.get("refresh_token")
            if not refresh:
                raise BoxNotAuthenticatedError("no token cache; run 'boxadm-mcp auth' to log in")
            return self._refresh(refresh)

    def _refresh(self, refresh_token: str) -> str:
        # Caller must hold cache_lock(self._cache_path): the refresh token is
        # single-use and the rotated result is persisted here.
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            resp = self._http.post(self._base + TOKEN_PATH, data=data)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if 400 <= status < 500 and status != 429:
                # Refresh tokens last ~60 days and are single-use; a 4xx here means
                # the chain is broken — re-login is required.
                raise BoxNotAuthenticatedError(f"token refresh failed (HTTP {status}); run 'boxadm-mcp auth'") from e
            # 5xx/429 is a transient server-side failure: the refresh token was
            # not consumed, so the next call can retry with the same token —
            # do NOT steer the operator toward an unnecessary re-login.
            raise BoxAuthError(f"token refresh failed (HTTP {status}); transient — retry later") from e
        except httpx.HTTPError as e:
            # Residual risk with no client-side fix: if Box committed the rotation
            # but the response was lost (timeout/disconnect), the cached refresh
            # token is already consumed and the next retry trips Box's reuse
            # detection (manual re-auth). The lock cannot prevent this class.
            raise BoxAuthError(f"token refresh error: {e}") from e
        try:
            access = body["access_token"]
        except (KeyError, TypeError) as e:
            raise BoxAuthError("token response missing access_token") from e
        new_refresh = body.get("refresh_token", refresh_token)  # Box rotates it
        expires_in = int(body.get("expires_in", 3600))
        self._save_cache(access, new_refresh, expires_in)
        self._access, self._access_deadline = access, int(time.time()) + expires_in
        return access

    def _force_refresh(self) -> None:
        rejected = self._access
        with cache_lock(self._cache_path):
            # The token that just got a 401 may already have been replaced by
            # another process; adopt the newer on-disk one instead of refreshing.
            # If the adopted token is itself revoked (whole-chain revocation),
            # the retried call fails once with a generic 401; the next call then
            # takes the refresh path below and surfaces needs-login.
            cache = self._load_cache()
            access = cache.get("access_token")
            expires_at = cache.get("access_expires_at", 0)
            if access and access != rejected and time.time() < expires_at - TOKEN_REFRESH_SKEW:
                self._access, self._access_deadline = access, expires_at
                return
            refresh = cache.get("refresh_token")
            if not refresh:
                raise BoxNotAuthenticatedError("no token cache; run 'boxadm-mcp auth' to log in")
            self._refresh(refresh)

    def authenticate(self) -> bool:
        """Ensure a valid access token (refreshing if needed). Returns True or raises."""
        self._ensure_token()
        return True

    def _on_401(self) -> None:
        # OAuth: the access token was revoked before its deadline — force a fresh one.
        self._force_refresh()

    def get_admin_events(
        self,
        *,
        created_after: str | None = None,
        created_before: str | None = None,
        event_types: list[str] | None = None,
        stream_position: str | int | None = None,
        limit: int = 500,
    ) -> dict:
        """Fetch one page of enterprise ``admin_logs`` events (raw Box response)."""
        return self._get("/events", _events_params(created_after, created_before, event_types, stream_position, limit))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BoxOAuthClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
