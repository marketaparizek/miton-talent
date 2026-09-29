import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from talent import db, store, vocab  # noqa: E402
from talent.models import Base  # noqa: E402


@pytest.fixture()
def session():
    db.reset_for_tests("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(db.engine())
    with db.session() as s:
        yield s


class Msg:
    def __init__(self, role, content):
        self.role, self.content = role, content


def _submit(s, uid="abc", email="jan@example.com", linkedin="https://www.linkedin.com/in/Jan-Novak-1/"):
    return store.create_from_submission(
        s, uid=uid,
        profile={"area": ["Software engineering", "Nonsense"], "level": "Mid level",
                 "workMode": ["Remote"], "status": "Aktivně hledám"},
        contact={"name": "Jan Novák", "email": email, "linkedin": linkedin, "note": "Ahoj"},
        summary="Backend dev, 5 let Pythonu.", consent=True, consent_text="souhlasím",
        cv_name="cv.pdf", lang="cs",
        messages=[Msg("user", "Ahoj"), Msg("assistant", "Ahoj, co děláš?"), Msg("system", "x")],
    )


def test_submission_creates_candidate_with_clean_vocab(session):
    row, created = _submit(session)
    assert created
    assert row.stage == "applied"
    assert row.area == ["Software engineering"]          # "Nonsense" dropped
    assert row.level == ["Mid level"]                     # scalar wrapped into a list
    assert row.linkedin_identifier == "jan-novak-1"
    assert row.email == "jan@example.com"
    assert row.transcript == [{"role": "user", "content": "Ahoj"},
                              {"role": "assistant", "content": "Ahoj, co děláš?"}]
    assert row.consent and row.consent_at is not None
    assert [e.type for e in row.events] == ["submitted"]


def test_submission_is_idempotent_on_uid(session):
    row1, _ = _submit(session)
    row2, created = _submit(session)
    assert not created and row1.id == row2.id


def test_find_by_linkedin_then_email(session):
    row, _ = _submit(session)
    assert store.find_candidate(session, linkedin_url="linkedin.com/in/JAN-NOVAK-1").id == row.id
    assert store.find_candidate(session, email="JAN@example.com").id == row.id
    assert store.find_candidate(session, email="nobody@example.com") is None


def test_score_and_stage_log_events(session):
    row, _ = _submit(session)
    store.apply_score(session, row, score=71, company_tier="T2", education_tier="T9",
                      fit_areas=["AI", "Bogus"], recommendation="Potential fit", reasoning="ok")
    assert row.score == 71 and row.education_tier is None and row.fit_areas == ["AI"]
    store.set_stage(session, row, "contacted", actor="marketa@miton.cz")
    store.set_stage(session, row, "contacted")  # no-op, no second event
    assert [e.type for e in row.events] == ["submitted", "scored", "stage_changed"]
    assert row.events[-1].data == {"from": "applied", "to": "contacted"}
    with pytest.raises(ValueError):
        store.set_stage(session, row, "bogus")


def test_search_and_follow_up_queue(session):
    row, _ = _submit(session)
    s = store.create_search(session, company_name="Deepscout", role="GTM člověk")
    link = store.add_to_search(session, s, row, miton_comment="strong GTM path")
    assert link.position == 1
    assert store.add_to_search(session, s, row).id == link.id  # idempotent
    assert any(e.type == "shared_with_founder" and e.search_id == s.id for e in row.events)

    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6)
    store.add_event(session, row, "outreach_sent", actor="marketa", data={"channel": "email"},
                    happened_at=long_ago)
    session.flush()
    due = store.follow_up_due(session)
    assert [c.id for c, _ in due] == [row.id]
    store.add_event(session, row, "outreach_replied", data={"channel": "email"})
    session.flush()
    assert store.follow_up_due(session) == []


def test_vocab_matches_chat_lists():
    # app.py ALLOWED_* must stay identical; this guards the copy in vocab.py.
    import app  # noqa: WPS433
    assert app.ALLOWED_AREA == vocab.AREA
    assert app.ALLOWED_LEVEL == vocab.LEVEL
    assert app.ALLOWED_MODE == vocab.WORK_MODE
    assert app.ALLOWED_STATUS == vocab.SEARCH_STATUS
    assert app.ALLOWED_FIT == vocab.FIT_AREAS
