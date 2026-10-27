"""The applicant inbox API that Alister's /miton hub and MCP read."""

import datetime as dt
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as chat  # noqa: E402
from talent import db, store  # noqa: E402
from talent.models import Base, Candidate, CandidateEvent  # noqa: E402

SECRET = "test-handoff-secret-shared-with-alister-32plus"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 'a.db'}")
    Base.metadata.create_all(db.engine())
    monkeypatch.setenv("TALENT_HANDOFF_SECRET", SECRET)
    monkeypatch.setenv("ALISTER_BASE_URL", "https://alister.example.test")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    now = dt.datetime.now(dt.timezone.utc)
    with db.session() as s:
        s.add_all([
            Candidate(uid="new1", full_name="Eva Nová", email="eva@example.test", source="talent_chat",
                      lang="cs", stage="applied", first_seen_at=now - dt.timedelta(hours=5),
                      summary="Growth marketérka", recommendation="Low fit", score=30,
                      transcript=[{"role": "user", "content": "ahoj"}], cv_filename="eva.pdf"),
            Candidate(uid="old1", full_name="Jan Starý", email="jan@example.test", source="typeform",
                      lang="cs", stage="applied", first_seen_at=now - dt.timedelta(days=30),
                      recommendation="Call", score=80),
            Candidate(uid="done1", full_name="Petr Hotový", source="talent_chat", stage="rejected",
                      first_seen_at=now - dt.timedelta(days=10)),
            Candidate(uid="src1", full_name="Sourced Person", source="sourcing", stage="applied",
                      first_seen_at=now - dt.timedelta(days=1)),
            Candidate(uid="gone1", full_name="Smazaný", source="talent_chat", stage="applied",
                      first_seen_at=now, deleted_at=now),
        ])
    return TestClient(chat.app)


def _token(purpose="applicants-read", secret=SECRET, ttl=120, **over):
    import jwt
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": "alister", "purpose": purpose, "aud": "miton-talent",
              "iat": now, "exp": now + dt.timedelta(seconds=ttl)}
    if purpose == "applicants-write":
        claims.update({"email": "kolega@miton.cz", "role": "miton", "uid": "7",
                       "jti": os.urandom(8).hex()})
    claims.update(over)
    return jwt.encode(claims, secret, algorithm="HS256")


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_the_inbox_needs_a_read_token(client):
    assert client.get("/api/applicants").status_code == 401
    # The portfolio token reads the portfolio, not people.
    assert client.get("/api/applicants", headers=_h(_token("portfolio-read"))).status_code == 401


def test_the_inbox_lists_unanswered_inbound_people_newest_first(client):
    r = client.get("/api/applicants", headers=_h(_token()))
    assert r.status_code == 200
    data = r.json()
    assert [i["uid"] for i in data["items"]] == ["new1", "old1"]
    assert data["total"] == 2
    assert data["counts"]["applied"] == 2      # sourced and deleted people are not inbox
    assert data["counts"]["rejected"] == 1
    eva = data["items"][0]
    assert eva["has_cv"] is True and "transcript" not in eva
    assert eva["waiting_days"] == 0
    # A rejection waits 48 hours from the application.
    eligible = dt.datetime.fromisoformat(eva["rejection_eligible_at"])
    applied = dt.datetime.fromisoformat(eva["applied_at"])
    assert eligible - applied == dt.timedelta(hours=48)


def test_the_inbox_filters_by_stage_and_text(client):
    data = client.get("/api/applicants?stage=rejected", headers=_h(_token())).json()
    assert [i["uid"] for i in data["items"]] == ["done1"]
    data = client.get("/api/applicants?q=growth", headers=_h(_token())).json()
    assert [i["uid"] for i in data["items"]] == ["new1"]
    assert client.get("/api/applicants?stage=nonsense", headers=_h(_token())).status_code == 400


def test_one_applicant_in_full(client):
    r = client.get("/api/applicants/new1", headers=_h(_token()))
    assert r.status_code == 200
    d = r.json()
    assert d["transcript"] == [{"role": "user", "content": "ahoj"}]
    assert d["cv_filename"] == "eva.pdf"
    assert client.get("/api/applicants/gone1", headers=_h(_token())).status_code == 404
    assert client.get("/api/applicants/nobody", headers=_h(_token())).status_code == 404


def test_a_read_token_cannot_write(client):
    r = client.post("/api/applicants/new1/stage", json={"stage": "rejected"}, headers=_h(_token()))
    assert r.status_code == 401


def test_a_write_moves_the_person_and_records_who(client):
    r = client.post("/api/applicants/old1/stage", json={"stage": "contacted", "reply": "invitation"},
                    headers=_h(_token("applicants-write")))
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True and r.json()["stage"] == "contacted"
    with db.session() as s:
        c = store.find_candidate(s, uid="old1")
        types = [(e.type, e.actor) for e in c.events]
    assert ("outreach_sent", "kolega@miton.cz") in types
    assert ("stage_changed", "kolega@miton.cz") in types
    # Out of the inbox now.
    data = client.get("/api/applicants", headers=_h(_token())).json()
    assert [i["uid"] for i in data["items"]] == ["new1"]


def test_a_write_token_is_spent_on_use(client):
    token = _token("applicants-write")
    assert client.post("/api/applicants/new1/notes", json={"text": "volat v pondělí"},
                       headers=_h(token)).status_code == 200
    assert client.post("/api/applicants/new1/notes", json={"text": "znovu"},
                       headers=_h(token)).status_code == 401
    with db.session() as s:
        notes = [e for e in s.query(CandidateEvent).all() if e.type == "note"]
    assert len(notes) == 1 and notes[0].data["text"] == "volat v pondělí"


@pytest.mark.parametrize("over,why", [
    ({"email": "someone@gmail.com"}, "not a Miton address"),
    ({"role": "user"}, "a plain Alister user"),
    ({"ttl": 3600}, "lifetime beyond the ceiling"),
    ({"secret": "another-secret-that-is-also-32-characters-long"}, "wrong secret"),
])
def test_a_bad_write_token_is_refused(client, over, why):
    ttl = over.pop("ttl", 120)
    secret = over.pop("secret", SECRET)
    token = _token("applicants-write", secret=secret, ttl=ttl, **over)
    r = client.post("/api/applicants/new1/stage", json={"stage": "rejected"}, headers=_h(token))
    assert r.status_code == 401, why


def test_a_write_checks_the_stage_and_reply(client):
    w = lambda: _h(_token("applicants-write"))  # noqa: E731
    assert client.post("/api/applicants/new1/stage", json={"stage": "nope"}, headers=w()).status_code == 400
    assert client.post("/api/applicants/new1/stage", json={"stage": "rejected", "reply": "hug"},
                       headers=w()).status_code == 400
    assert client.post("/api/applicants/gone1/stage", json={"stage": "rejected"}, headers=w()).status_code == 404


def test_an_applicant_token_cannot_open_a_session(client):
    for purpose in ("applicants-read", "applicants-write"):
        r = client.get(f"/auth/callback?token={_token(purpose)}", follow_redirects=False)
        assert "alister.example.test" in r.headers.get("location", "")
