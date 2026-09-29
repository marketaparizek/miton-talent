"""End-to-end: POST /submit stores a candidate row without Notion or e-mail configured."""
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app as chat  # noqa: E402
from talent import db, store  # noqa: E402
from talent.models import Base  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(db.engine())
    monkeypatch.setattr(chat, "NOTION_TOKEN", "")
    monkeypatch.setattr(chat, "RESEND_API_KEY", "")
    monkeypatch.setattr(chat, "SMTP_PASS", "")
    monkeypatch.setattr(chat, "SUBMISSIONS_FILE", str(tmp_path / "submissions.jsonl"))
    monkeypatch.setattr(chat, "SCORING_ENABLED", False)
    chat._hits.clear()
    return TestClient(chat.app)


def test_submit_creates_candidate(client):
    body = {
        "lang": "cs",
        "profile": {"area": ["Product"], "level": ["Specialist role"], "workMode": ["Hybrid"],
                    "status": ["Právě nehledám"]},
        "contact": {"name": "Eva Malá", "email": "eva@example.com",
                    "linkedin": "https://linkedin.com/in/eva-mala/", "note": "Dobrý den"},
        "summary": "Product person, 8 let.",
        "consent": True, "consent_text": "Souhlasím se zpracováním.",
        "messages": [{"role": "user", "content": "Ahoj"}, {"role": "assistant", "content": "Ahoj Evo"}],
    }
    r = client.post("/submit", json=body)
    assert r.status_code == 200 and r.json().get("queued")
    # background task runs inside TestClient before the response returns
    with db.session() as s:
        rows = store.list_candidates(s)
        assert len(rows) == 1
        c = rows[0]
        assert c.full_name == "Eva Malá" and c.linkedin_identifier == "eva-mala"
        assert c.area == ["Product"] and c.stage == "applied" and c.consent
        assert c.transcript[1]["content"] == "Ahoj Evo"
        assert c.events[0].type == "submitted"


def test_submit_without_consent_is_rejected(client):
    r = client.post("/submit", json={"consent": False, "contact": {"email": "x@example.com"}})
    assert r.status_code == 400
    with db.session() as s:
        assert store.list_candidates(s) == []


def test_diag_reports_database(client):
    r = client.get("/diag")
    assert r.status_code == 200
    j = r.json()
    assert j["database"].startswith("sqlite") and j["database_ok"] is True
