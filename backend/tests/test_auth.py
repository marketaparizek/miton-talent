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
    r = client.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://alister.example.test"
    assert client.get("/auth/me").status_code == 401


def test_logout_is_post_only(client):
    _callback(client, _token())
    assert client.get("/auth/logout", follow_redirects=False).status_code == 405
    assert client.get("/auth/me").status_code == 200  # a GET (a link, an <img>) signed nobody out


def test_logout_revokes_the_session_on_the_server(client):
    """A copy of the cookie taken before logout opens nothing afterwards."""
    _callback(client, _token())
    stolen = client.cookies.get(tauth.SESSION_COOKIE)
    client.post("/auth/logout", follow_redirects=False)
    other = TestClient(chat.app)
    other.cookies.set(tauth.SESSION_COOKIE, stolen)
    assert other.get("/auth/me").status_code == 401


def test_revoking_an_address_ends_its_sessions(client):
    _callback(client, _token())
    assert client.get("/auth/me").status_code == 200
    assert tauth.revoke_sessions_for("KOLEGA@miton.cz") == 1
    assert client.get("/auth/me").status_code == 401


def test_session_lasts_hours_not_weeks(client):
    assert tauth.SESSION_MAX_AGE_SECONDS <= 24 * 3600
    r = _callback(client, _token())
    assert f"Max-Age={tauth.SESSION_MAX_AGE_SECONDS}" in r.headers["set-cookie"]


def test_expired_session_row_is_refused(client):
    from talent.models import AdminSession

    _callback(client, _token())
    with db.session() as s:
        for row in s.query(AdminSession).all():
            row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    assert client.get("/auth/me").status_code == 401


def test_cookie_stores_no_identity(client):
    """The cookie is a signed session id; the address and role live on the server."""
    r = _callback(client, _token())
    data = tauth._serializer().loads(r.cookies[tauth.SESSION_COOKIE])
    assert set(data) == {"sid"}


def test_admin_routes_cannot_be_framed(client):
    _callback(client, _token())
    r = client.get("/admin")
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["x-frame-options"] == "DENY"
    embed = client.get("/")
    assert embed.headers["content-security-policy"].startswith("frame-ancestors 'self' ")
    assert "x-frame-options" not in embed.headers


def test_no_public_api_docs(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


# --- Production refuses weak configuration -----------------------------------


def test_production_needs_its_own_session_secret(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://talent.example.test")
    monkeypatch.setenv("TALENT_HANDOFF_SECRET", SECRET)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        tauth.check_production_config()
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        tauth._session_secret()
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    with pytest.raises(RuntimeError, match="differ"):
        tauth.check_production_config()
    monkeypatch.setenv("SESSION_SECRET", "short")
    with pytest.raises(RuntimeError, match="at least"):
        tauth.check_production_config()
    monkeypatch.setenv("SESSION_SECRET", "a-separate-session-secret-of-32-plus-chars")
    tauth.check_production_config()


def test_local_runs_without_a_session_secret(monkeypatch):
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("TALENT_HANDOFF_SECRET", SECRET)
    tauth.check_production_config()
    assert tauth._session_secret()


def test_production_refuses_sqlite_fallback(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://talent.example.test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        db.database_url()
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1/x")
    assert db.database_url().startswith("postgresql+psycopg://")
    monkeypatch.delenv("APP_BASE_URL")
    monkeypatch.delenv("DATABASE_URL")
    assert db.database_url().startswith("sqlite")


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


def test_old_identity_cookie_is_ignored(client):
    """A v1 cookie (identity inside, 30 days) signed with the right key opens nothing now."""
    from itsdangerous import URLSafeTimedSerializer

    old = URLSafeTimedSerializer(tauth._session_secret(), salt="mt-session-v1").dumps(
        {"user_id": "1", "email": "kolega@miton.cz", "role": "miton"})
    client.cookies.set(tauth.SESSION_COOKIE, old)
    assert client.get("/auth/me").status_code == 401
    newsalt = tauth._serializer().dumps({"user_id": "1", "email": "kolega@miton.cz", "role": "miton"})
    client.cookies.set(tauth.SESSION_COOKIE, newsalt)
    assert client.get("/auth/me").status_code == 401


def test_forged_cookie_is_ignored(client):
    client.cookies.set(tauth.SESSION_COOKIE, "forged-value")
    assert client.get("/auth/me").status_code == 401
    assert client.get("/admin", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 302


def test_cookie_signed_with_handoff_secret_directly_is_ignored(client):
    """The session key is derived from the handoff secret; the secret itself must not sign sessions."""
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer(SECRET, salt="mt-session-v2").dumps({"sid": "x" * 43})
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
