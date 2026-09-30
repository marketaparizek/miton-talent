"""Who may open the Miton Talent back office, and how they get in.

Miton Talent has no passwords and no users table. Alister is the only place a
person signs in; it holds the account tiers (admin / miton / user) and only
the first two, on an @miton.cz address, may come here. The door:

  1. In Alister the person opens /talent. Alister's API mints a one-minute
     JWT (purpose "talent-handoff", audience "miton-talent", a fresh jti),
     signed with TALENT_HANDOFF_SECRET, a secret the two services share and
     nothing else knows, and redirects the browser to /auth/callback?token=..
  2. /auth/callback verifies the signature, the purpose, the audience, the
     expiry, the role and the address, and refuses a jti it has seen before
     (used_handoff_tokens). Then it sets this service's own session cookie
     and sends the person to /admin.
  3. Every back-office route depends on ``require_user``: a valid cookie whose
     address is still @miton.cz. Without one, a browser is sent back to
     Alister's /talent page (which signs the person in and returns them);
     an API call gets 401.

The chat widget (/chat, /submit, /widget.js, the embed page) stays public:
it runs on the miton.cz career page for applicants. Nothing here touches it.

Why not a shared cookie: talent.miton.cz and alisterai.com are different
sites, so no cookie can span them. Why not verify Alister's session JWT with
Alister's secret: that secret mints Alister sessions, and a service that can
verify can also forge. The handoff secret can only produce one-minute door
tickets for this service.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import os
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from talent import db
from talent.models import UsedHandoffToken

log = logging.getLogger("miton-talent.auth")

MITON_DOMAIN = "@miton.cz"
ALLOWED_ROLES = ("admin", "miton")
HANDOFF_PURPOSE = "talent-handoff"
# A second purpose over the same secret: Alister's server asking this service
# for the portfolio snapshot, so the /miton hub can draw it in Alister's own
# design. It carries no identity, opens no session, and only ever reads.
SERVICE_READ_PURPOSE = "portfolio-read"
SERVICE_READ_MAX_AGE_SECONDS = 300
HANDOFF_AUDIENCE = "miton-talent"
HANDOFF_MAX_AGE_SECONDS = 120   # Alister issues 60 s; this is the ceiling we accept
HANDOFF_LEEWAY_SECONDS = 10     # clock skew between the two services (same box today)

SESSION_COOKIE = "mt_session"
SESSION_MAX_AGE_SECONDS = int(os.environ.get("SESSION_MAX_AGE_DAYS", "30")) * 24 * 3600

router = APIRouter(prefix="/auth", tags=["auth"])


# --- Configuration (read at call time so tests can set it) -------------------


MIN_SECRET_LENGTH = 32  # HS256 wants a key at least as long as its hash (RFC 7518 §3.2)


def handoff_secret() -> str:
    secret = os.environ.get("TALENT_HANDOFF_SECRET", "").strip()
    if not secret:
        raise RuntimeError("TALENT_HANDOFF_SECRET is not set; the Alister handoff cannot work")
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"TALENT_HANDOFF_SECRET must be at least {MIN_SECRET_LENGTH} characters")
    return secret


def _session_secret() -> bytes:
    """A key for the session cookie derived from the handoff secret, so one
    shared secret configures both services and a handoff token can still
    never be mistaken for a session (different algorithm, different key)."""
    explicit = os.environ.get("SESSION_SECRET", "").strip()
    if explicit:
        return explicit.encode()
    return hmac.new(handoff_secret().encode(), b"miton-talent-session", hashlib.sha256).digest()


def alister_base_url() -> str:
    return (os.environ.get("ALISTER_BASE_URL", "https://alisterai.com")).rstrip("/")


def _cookie_secure() -> bool:
    return os.environ.get("APP_BASE_URL", "").lower().startswith("https://")


def is_miton_email(email: Optional[str]) -> bool:
    return (email or "").strip().lower().endswith(MITON_DOMAIN)


# --- Handoff token -----------------------------------------------------------


class HandoffRejected(Exception):
    """A handoff token that must not open a session. ``reason`` is logged,
    never shown in detail to the browser."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def verify_handoff_token(token: str) -> dict:
    """Return the identity a valid, unused handoff token carries, else raise."""
    try:
        claims = jwt.decode(
            token,
            handoff_secret(),
            algorithms=["HS256"],
            audience=HANDOFF_AUDIENCE,
            leeway=HANDOFF_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "jti", "aud", "sub", "email", "role", "purpose"]},
        )
    except jwt.InvalidTokenError as e:
        raise HandoffRejected(f"invalid token: {type(e).__name__}") from e

    if claims.get("purpose") != HANDOFF_PURPOSE:
        raise HandoffRejected("wrong purpose")
    # Belt and braces on top of exp: a token older than the ceiling is out even
    # if a misconfigured issuer gave it a long life.
    if int(claims["exp"]) - int(claims["iat"]) > HANDOFF_MAX_AGE_SECONDS:
        raise HandoffRejected("lifetime too long")
    role = claims.get("role")
    email = (claims.get("email") or "").strip().lower()
    if role not in ALLOWED_ROLES:
        raise HandoffRejected(f"role not allowed: {role}")
    if not is_miton_email(email):
        raise HandoffRejected("address is not @miton.cz")

    _consume_jti(str(claims["jti"]), dt.datetime.fromtimestamp(int(claims["exp"]), dt.timezone.utc))
    return {"user_id": str(claims["sub"]), "email": email, "role": role}


class ServiceTokenRejected(Exception):
    """A service token that must not be answered."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def verify_service_token(token: str, purpose: str = SERVICE_READ_PURPOSE) -> dict:
    """Check a token minted by the other service with the shared secret.

    Different from the handoff in three ways, all of them on purpose: it says
    which service is asking rather than which person, it is not recorded
    against replay because replaying a read changes nothing, and it may never
    open a session. It is the only way in that is not a browser with a cookie.
    """
    try:
        claims = jwt.decode(
            token,
            handoff_secret(),
            algorithms=["HS256"],
            audience=HANDOFF_AUDIENCE,
            leeway=HANDOFF_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "aud", "purpose"]},
        )
    except jwt.InvalidTokenError as e:
        raise ServiceTokenRejected(f"invalid token: {type(e).__name__}") from e
    if claims.get("purpose") != purpose:
        raise ServiceTokenRejected("wrong purpose")
    if int(claims["exp"]) - int(claims["iat"]) > SERVICE_READ_MAX_AGE_SECONDS:
        raise ServiceTokenRejected("lifetime too long")
    return {"service": str(claims.get("sub") or "unknown")}


def require_service(request: Request) -> dict:
    """FastAPI dependency: an Authorization: Bearer <token> header, nothing else."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="service token required")
    try:
        return verify_service_token(token.strip())
    except ServiceTokenRejected as e:
        log.warning("service token refused: %s", e.reason)
        raise HTTPException(status_code=401, detail="service token refused") from e


def _consume_jti(jti: str, expires_at: dt.datetime) -> None:
    """Record the token id; a second use inside its lifetime is a replay."""
    now = dt.datetime.now(dt.timezone.utc)
    with db.session() as s:
        # Housekeeping: rows past their expiry can never be replayed anyway.
        s.execute(delete(UsedHandoffToken).where(UsedHandoffToken.expires_at < now - dt.timedelta(hours=1)))
        s.add(UsedHandoffToken(jti=jti, expires_at=expires_at, used_at=now))
        try:
            s.flush()
        except IntegrityError as e:
            raise HandoffRejected("token already used") from e


# --- Session cookie ----------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_session_secret(), salt="mt-session-v1")


def issue_session(response, identity: dict) -> None:
    value = _serializer().dumps(
        {"user_id": identity["user_id"], "email": identity["email"], "role": identity["role"]}
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=value,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def clear_session(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user(request: Request) -> Optional[dict]:
    """The signed-in Miton person, or None. Re-checks the address on every
    request so a cookie can never outlive the rule that admitted it."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("role") not in ALLOWED_ROLES or not is_miton_email(data.get("email")):
        return None
    return {"user_id": str(data.get("user_id")), "email": data["email"], "role": data["role"]}


class LoginRequired(Exception):
    """Raised by ``require_user``; app.py turns it into a redirect to Alister
    for browsers and a 401 for API callers."""


def require_user(request: Request) -> dict:
    """FastAPI dependency for every back-office route."""
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def login_redirect_url() -> str:
    """Where an unauthenticated browser goes: Alister's /talent page, which
    signs the person in if needed and hands them straight back here."""
    return f"{alister_base_url()}/talent"


def login_required_response(request: Request):
    accepts = request.headers.get("accept", "")
    if "text/html" in accepts:
        return RedirectResponse(url=login_redirect_url(), status_code=302, headers={"Cache-Control": "no-store"})
    return JSONResponse({"detail": "Sign in through Alister"}, status_code=401)


# --- Routes ------------------------------------------------------------------


@router.get("/callback")
def callback(request: Request, token: str = ""):
    """The end of the Alister handoff: token in, session cookie out."""
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")
    try:
        identity = verify_handoff_token(token)
    except HandoffRejected as e:
        log.warning("handoff rejected: %s ip=%s", e.reason, request.client.host if request.client else "?")
        # Send the person back to Alister to try again; no detail leaks.
        return RedirectResponse(url=login_redirect_url(), status_code=302, headers={"Cache-Control": "no-store"})
    log.info("handoff accepted email=%s role=%s", identity["email"], identity["role"])
    response = RedirectResponse(url="/admin", status_code=302, headers={"Cache-Control": "no-store"})
    issue_session(response, identity)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse(url=alister_base_url(), status_code=302, headers={"Cache-Control": "no-store"})
    clear_session(response)
    return response


@router.get("/me")
def me(request: Request):
    user = current_user(request)
    if user is None:
        return login_required_response(request)
    return user
