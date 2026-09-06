"""Bearer token authentication middleware."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

from fastapi import Header, HTTPException, Request, Response, Security
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from starlette.requests import HTTPConnection

from openbrowse import auth_throttle
from openbrowse.config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
# @nonobvious(forced-by): auto_error off, because it would answer a fresh
# install's first request with a password challenge before any code could run,
# and a fresh install has no password. require_dashboard_auth issues the
# challenge itself, once there is something to challenge against.
_basic = HTTPBasic(auto_error=False)

SETUP_PATH = "/setup"

# @nonobvious(forced-by): RFC 7617 requires a realm on a Basic challenge, and
# browsers key their stored credentials on (origin, realm). Omitting it leaves
# that key undefined, which is why a cached password would be dropped without
# anything having expired. charset says how to encode a non-ASCII password,
# which is UTF-8 here because that is what this module decodes.
_BASIC_CHALLENGE = 'Basic realm="OpenBrowse", charset="UTF-8"'


SESSION_COOKIE = "openbrowse_session"

# Bumped only if the token layout changes, so old tokens stop verifying rather
# than being misread as a newer shape.
_SESSION_CONTEXT = b"openbrowse.dashboard.session.v1"


def _session_key() -> bytes:
    """The key that signs a dashboard session.

    Derived from the password rather than generated at startup, for two
    reasons: a restart does not sign everyone out, and changing the password
    invalidates every session issued under the old one, which is most of what
    changing a password is for.
    """
    return hmac.new(
        _SESSION_CONTEXT, _expected_dashboard_password().encode(), hashlib.sha256
    ).digest()


def _sign(payload: str) -> str:
    return hmac.new(_session_key(), payload.encode(), hashlib.sha256).hexdigest()


def _session_token(user: str, expires_at: int) -> str:
    name = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
    payload = f"{name}.{expires_at}"
    return f"{payload}.{_sign(payload)}"


def session_user(conn: HTTPConnection) -> str | None:
    """The signed-in user this request carries, or None.

    A cookie is the difference between a session and a cached password. A
    browser drops a stored password the moment any request comes back 401,
    which turns one refused poll into being signed out of a page that was
    working; it does not do that to a cookie.
    """
    if not _expected_dashboard_password():
        return None
    raw = conn.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    payload, _, signature = raw.rpartition(".")
    if not payload or not signature:
        return None
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    name, _, expiry = payload.partition(".")
    try:
        if int(expiry) <= time.time():
            return None
        user = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(user, settings.dashboard_user):
        return None
    return user


def issue_session(request: Request, response: Response, user: str) -> None:
    """Start or extend a signed-in session on the response.

    # @nonobvious(forced-by): called from middleware, not from the dependency
    # that authenticates. FastAPI does not merge a dependency's response
    # headers into a Response a route returned itself, and every dashboard
    # route returns one, so a cookie set there would silently never be sent.
    """
    days = settings.dashboard_session_days
    # 0 means the session should last only as long as the browser is open, so
    # the cookie gets no Max-Age; the token still carries an expiry, because a
    # token nothing can date is a token that never stops working.
    lifetime = (days or 1) * 86400
    response.set_cookie(
        SESSION_COOKIE,
        _session_token(user, int(time.time()) + lifetime),
        max_age=lifetime if days else None,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _session_is_stale(conn: HTTPConnection) -> bool:
    """Whether a valid session is over halfway through its life.

    Renewing on every request would put a Set-Cookie on every poll; renewing
    only past the halfway mark keeps the session sliding without the noise.
    """
    raw = conn.cookies.get(SESSION_COOKIE) or ""
    payload, _, _ = raw.rpartition(".")
    _, _, expiry = payload.partition(".")
    try:
        remaining = int(expiry) - time.time()
    except ValueError:
        return True
    lifetime = (settings.dashboard_session_days or 1) * 86400
    return remaining < lifetime / 2


def _fetch_context(conn: HTTPConnection) -> str:
    """How the browser says it made this request.

    A dashboard 401 is otherwise unexplainable after the fact: it cannot be
    told whether a password was absent or wrong, nor whether the request was a
    page someone opened or a poll running behind one. Both answers change what
    is worth doing about it, so both are recorded.
    """
    dest = conn.headers.get("sec-fetch-dest") or "?"
    mode = conn.headers.get("sec-fetch-mode") or "?"
    site = conn.headers.get("sec-fetch-site") or "?"
    return f"dest={dest} mode={mode} site={site}"


def challenge_headers(conn: HTTPConnection) -> dict[str, str]:
    """Headers for a 401, which may or may not ask the browser to log in.

    A Basic challenge answering a background fetch makes the browser throw a
    login box over a page the reader is already using. The dashboard polls
    every ten seconds, so one lapsed credential becomes an interruption almost
    immediately, over and over. A poll is left to fail quietly instead; the
    next page load is a moment where asking for the password makes sense, and
    it still carries the challenge.
    """
    if conn.headers.get("sec-fetch-dest") == "empty":
        return {}
    return {"WWW-Authenticate": _BASIC_CHALLENGE}


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    x_browser_use_api_key: str | None = Header(default=None),
) -> str:
    """Validate the API key from the X-Browser-Use-API-Key header (SDK) or a bearer token."""
    ip = auth_throttle.client_ip(request)
    auth_throttle.enforce(ip)
    if not settings.api_key:
        if settings.allow_insecure_no_auth:
            return "dev"
        raise HTTPException(status_code=401, detail="Server authentication is not configured")
    presented = x_browser_use_api_key
    if presented is None and credentials is not None:
        presented = credentials.credentials
    if presented is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not hmac.compare_digest(presented, settings.api_key):
        auth_throttle.throttle.record_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid API key")
    auth_throttle.throttle.record_success(ip)
    return presented


def _expected_dashboard_password() -> str:
    return settings.dashboard_password or settings.api_key


def check_dashboard_credentials(username: str, password: str) -> bool:
    """Constant-time check of Basic credentials against the dashboard user and password."""
    expected = _expected_dashboard_password()
    if not expected:
        return settings.allow_insecure_no_auth
    user_ok = hmac.compare_digest(username, settings.dashboard_user)
    pass_ok = hmac.compare_digest(password, expected)
    return user_ok and pass_ok


def dashboard_auth_ok(authorization: str | None, conn: HTTPConnection | None = None) -> bool:
    """Verify a raw Basic ``Authorization`` header.

    For routes that cannot use the ``Security(HTTPBasic)`` dependency cleanly, such as
    WebSocket handshakes and the noVNC asset passthrough. When ``conn`` is given,
    failed attempts with presented credentials count towards the per-IP backoff
    and locked-out IPs are refused outright.
    """
    ip = auth_throttle.client_ip(conn) if conn is not None else None
    if ip is not None and auth_throttle.throttle.retry_after(ip) > 0:
        return False
    if not _expected_dashboard_password():
        return settings.allow_insecure_no_auth
    # A WebSocket handshake carries cookies reliably where it carries a stored
    # password only sometimes, so the session is the better answer here too.
    if conn is not None and session_user(conn) is not None:
        return True
    if not authorization:
        return False
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        username, sep, password = base64.b64decode(encoded).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    if not sep:
        return False
    ok = check_dashboard_credentials(username, password)
    if ip is not None:
        if ok:
            auth_throttle.throttle.record_success(ip)
        else:
            auth_throttle.throttle.record_failure(ip)
    return ok


async def require_dashboard_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Security(_basic),
) -> str:
    """Gate the dashboard behind HTTP Basic auth (dashboard user + password, or the API key).

    Until this instance has a credential of its own, the dashboard has nothing to
    check and sends the visitor to the setup wizard instead. That is the whole
    reason no default password ships: there is never a working credential that
    the person who installed OpenBrowse did not choose.
    """
    ip = auth_throttle.client_ip(request)
    auth_throttle.enforce(ip)
    if not _expected_dashboard_password():
        if settings.allow_insecure_no_auth:
            return "dev"
        raise HTTPException(
            status_code=303,
            detail="OpenBrowse is not configured yet; continue at /setup.",
            headers={"Location": SETUP_PATH},
        )
    signed_in = session_user(request)
    if signed_in is not None:
        auth_throttle.throttle.record_success(ip)
        if _session_is_stale(request):
            request.state.issue_session_for = signed_in
        return signed_in
    if credentials is None:
        logger.info(
            "Dashboard 401 on %s: no credentials presented (%s)",
            request.url.path,
            _fetch_context(request),
        )
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers=challenge_headers(request),
        )
    if not check_dashboard_credentials(credentials.username, credentials.password):
        auth_throttle.throttle.record_failure(ip)
        # The username is reported only as known or not: a mistyped password
        # lands in that field often enough that logging it verbatim would put
        # passwords in the journal.
        logger.warning(
            "Dashboard 401 on %s: credentials rejected, username %s (%s)",
            request.url.path,
            "known" if credentials.username == settings.dashboard_user else "unknown",
            _fetch_context(request),
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers=challenge_headers(request),
        )
    auth_throttle.throttle.record_success(ip)
    request.state.issue_session_for = credentials.username
    return credentials.username
