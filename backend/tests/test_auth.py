"""The Alister handoff and the Miton Talent session: nobody gets in from outside."""
import datetime as dt
import os
import sys

import jwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app as chat  # noqa: E402
from talent import auth as tauth  # noqa: E402
from talent import db  # noqa: E402
from talent.models import Base  # noqa: E402

SECRET = "test-handoff-secret-shared-with-alister-32plus"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(db.engine())
    monkeypatch.setenv("TALENT_HANDOFF_SECRET", SECRET)
    monkeypatch.setenv("ALISTER_BASE_URL", "https://alister.example.test")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    return TestClient(chat.app)


def _token(secret=SECRET, ttl=60, **over):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {
        "sub": "42",
        "email": "kolega@miton.cz",
        "role": "miton",
        "purpose": "talent-handoff",
        "aud": "miton-talent",
        "jti": over.pop("jti", os.urandom(8).hex()),
        "iat": now,
        "exp": now + dt.timedelta(seconds=ttl),
    }
    claims.update(over)
    return jwt.encode(claims, secret, algorithm="HS256")


def _callback(client, token):
    return client.get(f"/auth/callback?token={token}", follow_redirects=False)


# --- The happy path ----------------------------------------------------------


def test_valid_handoff_opens_a_session(client):
    r = _callback(client, _token())
    assert r.status_code == 302
    assert r.headers["location"] == "/admin"
    assert tauth.SESSION_COOKIE in r.cookies
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie

    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json() == {"user_id": "42", "email": "kolega@miton.cz", "role": "miton"}

    r = client.get("/admin")
    assert r.status_code == 200 and "kolega@miton.cz" in r.text


def test_admin_role_on_miton_address_is_admitted(client):
    r = _callback(client, _token(role="admin", email="marketa.parizek@miton.cz"))
    assert r.status_code == 302 and r.headers["location"] == "/admin"


def test_logout_clears_the_session(client):
    _callback(client, _token())
    r = client.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://alister.example.test"
    assert client.get("/auth/me").status_code == 401


# --- Everything that must be refused ----------------------------------------


def _assert_refused(client, token):
    r = _callback(client, token)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "https://alister.example.test/talent"
    assert tauth.SESSION_COOKIE not in r.cookies
    assert client.get("/auth/me").status_code == 401


def test_outside_address_is_refused_even_with_a_valid_signature(client):
    _assert_refused(client, _token(email="someone@example.com"))
    _assert_refused(client, _token(email="kolega@miton.cz.evil.com"))


def test_user_role_is_refused(client):
    _assert_refused(client, _token(role="user"))


def test_wrong_secret_is_refused(client):
    _assert_refused(client, _token(secret="not-the-shared-secret-but-long-enough-x"))


def test_alister_session_jwt_is_not_a_door_ticket(client):
    """A token shaped like an Alister session cookie (no purpose/aud/jti) is useless here."""
    now = dt.datetime.now(dt.timezone.utc)
    session_like = jwt.encode(
        {"sub": "42", "email": "kolega@miton.cz", "role": "miton", "iat": now,
         "exp": now + dt.timedelta(days=7)},
        SECRET, algorithm="HS256",
    )
    _assert_refused(client, session_like)


def test_wrong_purpose_or_audience_is_refused(client):
    _assert_refused(client, _token(purpose="mfa"))
    _assert_refused(client, _token(aud="alister"))


def test_expired_token_is_refused(client):
    _assert_refused(client, _token(ttl=-30))


def test_overlong_lifetime_is_refused(client):
    _assert_refused(client, _token(ttl=3600))


def test_token_is_single_use(client):
    token = _token(jti="once")
    assert _callback(client, _token(jti="once")).status_code == 302
    fresh = TestClient(chat.app)
    r = fresh.get(f"/auth/callback?token={token}", follow_redirects=False)
    assert r.headers["location"] == "https://alister.example.test/talent"
    assert fresh.get("/auth/me").status_code == 401


def test_missing_or_garbage_token(client):
    assert _callback(client, "").status_code == 400
    _assert_refused(client, "not.a.jwt")


# --- The session cookie itself -----------------------------------------------


def test_forged_cookie_is_ignored(client):
    client.cookies.set(tauth.SESSION_COOKIE, "forged-value")
    assert client.get("/auth/me").status_code == 401
    assert client.get("/admin", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 302


def test_cookie_signed_with_handoff_secret_directly_is_ignored(client):
    """The session key is derived from the handoff secret; the secret itself must not sign sessions."""
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer(SECRET, salt="mt-session-v1").dumps(
        {"user_id": "1", "email": "kolega@miton.cz", "role": "miton"}
    )
    client.cookies.set(tauth.SESSION_COOKIE, forged)
    assert client.get("/auth/me").status_code == 401


def test_session_for_outside_address_is_rejected_on_read(client):
    """Even a correctly signed session is refused once the address rule fails."""
    from fastapi import Response

    resp = Response()
    tauth.issue_session(resp, {"user_id": "1", "email": "x@example.com", "role": "miton"})
    value = resp.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    client.cookies.set(tauth.SESSION_COOKIE, value)
    assert client.get("/auth/me").status_code == 401


# --- Public routes stay public -----------------------------------------------


def test_widget_routes_need_no_session(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/widget.js").status_code in (200, 404)  # bundle may be absent locally


def test_short_handoff_secret_refuses_to_run(client, monkeypatch):
    monkeypatch.setenv("TALENT_HANDOFF_SECRET", "short")
    lenient = TestClient(chat.app, raise_server_exceptions=False)
    r = lenient.get(f"/auth/callback?token={_token(secret='short')}", follow_redirects=False)
    assert r.status_code == 500
    assert tauth.SESSION_COOKIE not in r.cookies
