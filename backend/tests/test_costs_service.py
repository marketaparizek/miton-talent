"""Alister's admin Costs tab reads and writes running costs over service tokens."""
import datetime as dt
import os
import sys

import jwt
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_costs import fresh_db, http, rows  # noqa: E402,F401
from tests.test_usage import SECRET  # noqa: E402


def tok(purpose, role="admin", email="admin@alister.com", jti=None, ttl=60):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": "alister", "purpose": purpose, "aud": "miton-talent", "iat": now,
              "exp": now + dt.timedelta(seconds=ttl)}
    if purpose == "costs-write":
        claims.update(email=email, role=role, uid="1", jti=jti or os.urandom(8).hex())
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


def test_snapshot_needs_the_read_purpose(http):
    assert http.get("/api/costs/snapshot").status_code == 401
    assert http.get("/api/costs/snapshot", headers=tok("portfolio-read")).status_code == 401
    r = http.get("/api/costs/snapshot", headers=tok("costs-read"))
    assert r.status_code == 200 and set(r.json()) == {"talent", "costs"}


def test_manual_write_is_admin_only_and_records_who(http):
    body = {"provider": "brightdata", "month": "2026-11", "amount": 100, "currency": "USD", "estimate": True}
    assert http.post("/api/costs/manual", json=body, headers=tok("costs-read")).status_code == 401
    assert http.post("/api/costs/manual", json=body,
                     headers=tok("costs-write", role="miton", email="kolega@miton.cz")).status_code == 401
    assert http.post("/api/costs/manual", json=body, headers=tok("costs-write")).status_code == 200
    (r,) = rows("brightdata")
    assert r.source == "estimate" and r.updated_by == "admin@alister.com"


def test_a_write_token_works_once(http):
    h = tok("costs-write", jti="same-jti-1")
    body = {"provider": "hetzner", "month": "2026-10", "amount": 40, "currency": "EUR"}
    assert http.post("/api/costs/manual", json=body, headers=h).status_code == 200
    assert http.post("/api/costs/manual", json=body, headers=h).status_code == 401


def test_sync_over_the_service_api(http):
    r = http.post("/api/costs/sync", json={}, headers=tok("costs-write"))
    assert r.status_code == 200 and r.json()["result"]["openai"]["error"] == "chybí klíč"
