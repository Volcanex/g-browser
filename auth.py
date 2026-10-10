"""Single-password cookie gate for the gbrowser panel.

This panel controls the owner's fully logged-in Chrome identity. Anyone who
reaches it is them. It is a loaded gun. The gate is one shared password
(`GBROWSER_PASSWORD`, from the passport); the debug port is loopback-only
and never exposed. Machine consumers of `/run` authenticate with a separate
service token, not this cookie. A successful login sets the cookie to
`GBROWSER_COOKIE_VALUE`, a second secret, so the cookie cannot be forged.

Pattern lifted from gcommand/core/api/admin.py, with the same hardening (per-IP
rate limit, constant-time compare, Secure cookie), namespaced to GBROWSER.
"""

from __future__ import annotations

import hmac
import os
import sys
import time
from collections import deque

from fastapi import HTTPException, Request, Response

COOKIE = os.environ.get("GBROWSER_COOKIE", "gbrowser_admin")
# The cookie value is the session secret: whoever holds it is logged in. It
# used to be the constant "ok", which let anyone forge the cookie, so there is
# no default now and server.py refuses to start without one.
COOKIE_VALUE = os.environ.get("GBROWSER_COOKIE_VALUE", "")

_RATE_WINDOW = 60.0
_RATE_LIMIT = 5
_LOGIN_ATTEMPTS: dict[str, deque] = {}

_pw = os.environ.get("GBROWSER_PASSWORD")
if not _pw:
    print(
        "WARNING: GBROWSER_PASSWORD unset, so the panel is ungated. Set it in "
        "the instance passport (gp-core/.env) before this is reachable.",
        file=sys.stderr,
    )


def _password() -> str:
    return os.environ.get("GBROWSER_PASSWORD", "")


def _cookie_secure() -> bool:
    return os.environ.get("GBROWSER_COOKIE_SECURE", "1").lower() not in ("0", "false", "no")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _too_many_failures(ip: str) -> bool:
    bucket = _LOGIN_ATTEMPTS.get(ip)
    if not bucket:
        return False
    cutoff = time.monotonic() - _RATE_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return len(bucket) >= _RATE_LIMIT


def is_admin(request: Request) -> bool:
    """True when the request (or websocket) carries the admin cookie. An
    unset value never matches, so a misconfigured deploy fails closed."""
    sent = request.cookies.get(COOKIE, "")
    return bool(COOKIE_VALUE) and hmac.compare_digest(sent.encode(), COOKIE_VALUE.encode())


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(401, "admin only")


def do_login(password: str, request: Request, response: Response) -> dict:
    ip = _client_ip(request)
    if _too_many_failures(ip):
        raise HTTPException(429, "too many attempts; slow down")
    expected = _password()
    # An unset password must never authenticate (empty == empty would let
    # anyone in). Fail closed.
    if not expected or not hmac.compare_digest(password, expected):
        _LOGIN_ATTEMPTS.setdefault(ip, deque()).append(time.monotonic())
        raise HTTPException(401, "wrong password")
    _LOGIN_ATTEMPTS.pop(ip, None)
    response.set_cookie(
        COOKIE, COOKIE_VALUE,
        max_age=60 * 60 * 24 * 30,
        httponly=True, samesite="lax", secure=_cookie_secure(), path="/",
    )
    return {"ok": True}


def require_service(request: Request) -> None:
    """Gate `/run` for machine consumers. Accept either the admin cookie
    (a human driving from the panel) or the service token header. The token
    lives in the passport (`GBROWSER_SERVICE_TOKEN`); consumers send it as
    `X-Gbrowser-Token`."""
    if is_admin(request):
        return
    token = os.environ.get("GBROWSER_SERVICE_TOKEN", "")
    sent = request.headers.get("x-gbrowser-token", "")
    if not token or not hmac.compare_digest(sent, token):
        raise HTTPException(401, "service token required")
