"""POST /api/game-application: the playable job ads' form, and the page that serves one."""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app as chat  # noqa: E402
from talent import db, store  # noqa: E402
from talent.models import Base  # noqa: E402

GOOD = {"linkedin": "https://www.linkedin.com/in/Jan-Novak-1a2b/", "github": "https://github.com/jan",
        "email": "jan@example.com", "role": "yasan-senior-backend-engineer", "source": "yasan-game",
        "consent": True}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(db.engine())
    monkeypatch.setattr(chat, "NOTION_TOKEN", "")
    monkeypatch.setattr(chat, "RESEND_API_KEY", "")
    monkeypatch.setattr(chat, "SMTP_PASS", "")
    monkeypatch.setattr(chat, "SUBMISSIONS_FILE", str(tmp_path / "submissions.jsonl"))
    return TestClient(chat.app)


def _rows():
    with db.session() as s:
        return [(c.full_name, c.email, c.linkedin_identifier, c.source, c.stage, c.sourced_for,
                 c.note, c.consent_version, c.events[0].type, c.events[0].data) for c in store.list_candidates(s)]


def test_creates_applied_candidate_with_source_and_role(client):
    r = client.post("/api/game-application", json=GOOD)
    assert r.status_code == 200 and r.json()["queued"]
    (name, email, ident, source, stage, applied_for, note, consent, ev, data), = _rows()
    assert (email, ident, source, stage) == ("jan@example.com", "jan-novak-1a2b", "yasan-game", "applied")
    assert applied_for == ["yasan-senior-backend-engineer"]
    assert note == "GitHub or project: https://github.com/jan"
    assert consent == "game-2026-en" and ev == "submitted" and data["role"] == "yasan-senior-backend-engineer"


def test_linkedin_only_is_enough_and_names_the_row(client):
    r = client.post("/api/game-application", json={"linkedin": "linkedin.com/in/eva", "source": "yasan-game",
                                                   "consent": True})
    assert r.status_code == 200
    assert _rows()[0][0] == "eva"


def test_email_goes_out_like_the_chat(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(chat, "_send_cv_email",
                        lambda profile, contact, cv, summary, lang, origin="": sent.update(contact=contact, origin=origin) or True)
    client.post("/api/game-application", json=GOOD)
    assert sent["origin"] == "yasan-game, yasan-senior-backend-engineer"
    assert sent["contact"]["linkedin"].startswith("https://www.linkedin.com/in/")


@pytest.mark.parametrize("patch", [
    {"linkedin": ""},
    {"linkedin": "https://github.com/jan"},
    {"linkedin": "https://linkedin.com.evil.io/in/jan"},
    {"linkedin": "https://evil.io/?linkedin.com/in/jan"},
    {"linkedin": "javascript:alert(1)//linkedin.com/in/x"},
    {"linkedin": "https://www.linkedin.com/"},
    {"github": "not a url"},
    {"email": "nope"},
    {"source": "anything"},
    {"role": "Robert'); DROP"},
    {"consent": False},
])
def test_rejects_bad_input(client, patch):
    r = client.post("/api/game-application", json={**GOOD, **patch})
    assert r.status_code == 400
    assert _rows() == []


def test_rate_limited_with_429(client, monkeypatch):
    monkeypatch.setattr(chat, "GAME_RATE_LIMIT", 2)
    codes = [client.post("/api/game-application", json=GOOD).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_game_applicants_reach_the_inbox():
    from talent import applicants
    assert "yasan-game" in applicants.INBOUND_SOURCES


def test_serves_the_page_pointing_at_the_endpoint(client):
    r = client.get("/yasan-quest")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "submitUrl: '/api/game-application'" in r.text


def test_page_view_is_forwarded_only_for_known_pages(client, monkeypatch):
    sent = []
    monkeypatch.setattr(chat, "ALISTER_PAGEVIEW_URL", "http://alister.test/api/v1/pageviews")
    monkeypatch.setattr(chat, "_forward_page_view", lambda payload, ua: sent.append((payload, ua)))
    ua = {"User-Agent": "Mozilla/5.0"}
    assert client.post("/api/pageview", json={"path": "/yasan-quest/", "referrer": "https://www.linkedin.com/",
                                              "session_id": "abc"}, headers=ua).status_code == 204
    assert client.post("/api/pageview", json={"path": "/admin"}, headers=ua).status_code == 204
    assert sent == [({"path": "/yasan-quest", "referrer": "https://www.linkedin.com/", "utm_source": None,
                      "session_id": "abc"}, "Mozilla/5.0")]


def test_page_view_is_a_no_op_when_off(client, monkeypatch):
    monkeypatch.setattr(chat, "ALISTER_PAGEVIEW_URL", "")
    monkeypatch.setattr(chat, "_forward_page_view", lambda *a: pytest.fail("must not forward"))
    assert client.post("/api/pageview", json={"path": "/yasan-quest"}).status_code == 204
