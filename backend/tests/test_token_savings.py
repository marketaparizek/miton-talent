"""Token savings: the chat's cached prefix, and unchanged careers pages that skip the model.

No test here talks to Anthropic: the chat client and the page reader are fakes.
"""
import logging
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app as chat  # noqa: E402
from talent import db  # noqa: E402
from talent.models import Base, PortfolioCompanyRun, PortfolioRole  # noqa: E402
from talent.portfolio import adapters, extract, scrape, store  # noqa: E402


# --- chat: prompt caching ----------------------------------------------------


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="tool_use", name="record_reply",
                                input={"reply": "Ahoj!", "profile": {}, "summary": "", "stage": "exploring"})
        usage = SimpleNamespace(input_tokens=40, output_tokens=60,
                                cache_creation_input_tokens=0, cache_read_input_tokens=1700)
        return SimpleNamespace(content=[block], usage=usage, model=kwargs.get("model"))


@pytest.fixture()
def fake_model(tmp_path, monkeypatch):
    # The chat keeps the conversation and its limits in the database, so each
    # test gets a fresh one.
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 'chat.db'}")
    Base.metadata.create_all(db.engine())
    monkeypatch.delenv("LLM_DAILY_BUDGET", raising=False)
    fake = FakeMessages()
    monkeypatch.setattr(chat, "client", SimpleNamespace(messages=fake))
    return fake


def _talk(http, lang, turns):
    """Send ``turns`` user messages in one server-side conversation."""
    cid = ""
    for i in range(turns):
        r = http.post("/chat", json={"lang": lang, "conversation_id": cid, "message": f"zprava cislo {i}"},
                      headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 200, r.text
        cid = r.json()["conversation_id"]


@pytest.mark.parametrize("lang", ["cs", "en"])
def test_the_static_prompt_carries_the_breakpoint_and_the_wrap_up_comes_after_it(fake_model, lang):
    http = TestClient(chat.app)
    _talk(http, lang, chat.SOFT_CLOSE_TURNS)

    early, late = fake_model.calls[0], fake_model.calls[-1]
    # before the soft close: one block, the static prompt, cached
    assert early["system"] == [{"type": "text", "text": chat.SYSTEM[lang], "cache_control": {"type": "ephemeral"}}]
    # from the soft close on: the cached block is byte-identical, the wrap-up follows
    # it without a marker of its own, so the cached prefix (tools + system[0]) holds
    assert late["system"][0] == early["system"][0]
    assert len(late["system"]) == 2
    assert "cache_control" not in late["system"][1]
    assert late["system"][1]["text"] == chat.WRAP_UP[lang].strip()
    # tools render before system: they must not change between turns either
    assert early["tools"] == late["tools"] == [chat.REPLY_TOOL]
    assert early["tool_choice"] == late["tool_choice"]


def test_the_breakpoint_holds_nothing_per_request():
    blocks_a = chat._chat_system("cs", 1)
    blocks_b = chat._chat_system("cs", 2)
    assert blocks_a == blocks_b
    assert chat._chat_system("cs", chat.SOFT_CLOSE_TURNS)[0] == blocks_a[0]


def test_the_chat_logs_usage_counts_and_no_contents(fake_model, caplog):
    caplog.set_level(logging.INFO, logger="miton-talent.usage")
    _talk(TestClient(chat.app), "cs", 1)
    lines = [r.getMessage() for r in caplog.records if r.name == "miton-talent.usage"]
    assert lines == [f"anthropic usage feature=chat model={chat.MODEL} input=40 output=60 cache_write=0 cache_read=1700"]
    assert "zprava" not in lines[0] and "Ahoj" not in lines[0]


# --- portfolio: an unchanged page reuses its last successful read --------------


PAGE = "<html><body><h1>Kariéra</h1>" + "<p>Hledáme lidi do týmu, kteří chtějí stavět produkt.</p>" * 12 + \
       "<h2>Backend Engineer</h2><p>Praha</p><h2>Designer</h2><p>Brno</p></body></html>"
PAGE_CHANGED = PAGE.replace("</body>", "<h2>Data Analyst</h2><p>Praha</p></body>")


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 'p.db'}")
    Base.metadata.create_all(db.engine())
    monkeypatch.setenv("PORTFOLIO_CLASSIFY_LLM", "0")
    monkeypatch.setattr(extract, "check_model", lambda: None)
    with db.session() as s:
        store.upsert_company(s, "page-co", name="Page Co", careers_url="https://page.test/kariera", adapter="html")
        s.commit()
        yield s


class Site:
    """The careers page as the fetcher sees it, and a model that counts its calls."""

    def __init__(self, monkeypatch):
        self.html = PAGE
        self.model_calls = 0
        self.model_fails = False
        site = self

        class Response:
            status_code = 200

            @property
            def text(self):
                return site.html

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **kw):
                return Response()

        def model(text, *, url, company=""):
            site.model_calls += 1
            if site.model_fails:
                raise extract.ExtractorUnavailable("model answer was not JSON")
            titles = [t for t in ("Backend Engineer", "Designer", "Data Analyst") if t in text]
            return [{"title": t, "location": "Praha", "team": None, "employment_type": None, "url": url}
                    for t in titles], "two roles listed"

        monkeypatch.setattr(adapters, "_client", lambda: Client())
        monkeypatch.setattr(extract, "roles_from_text", model)


@pytest.fixture()
def site(monkeypatch):
    return Site(monkeypatch)


def _open_titles(session):
    session.expire_all()
    return sorted(r.title for r in session.query(PortfolioRole).filter(PortfolioRole.closed_at.is_(None)))


def _company_run(session, run_id):
    session.expire_all()
    return session.query(PortfolioCompanyRun).filter_by(run_id=run_id).one()


def test_an_unchanged_page_reuses_the_last_read_without_the_model(session, site):
    first = scrape.run(actor="cron", parallel=1)
    assert site.model_calls == 1
    crun = _company_run(session, first["run_id"])
    assert crun.status == "ok" and crun.page_hash and crun.extracted["note"] == "two roles listed"
    assert crun.reused_from_run_id is None

    second = scrape.run(actor="cron", parallel=1)
    assert site.model_calls == 1                       # no model call this week
    assert second["per_company"][0] == {"status": "ok", "roles": 2, "new": 0, "closed": 0,
                                        "company": "Page Co", "reused_from_run_id": first["run_id"]}
    crun2 = _company_run(session, second["run_id"])
    assert crun2.status == "ok" and crun2.page_hash == crun.page_hash
    assert crun2.reused_from_run_id == first["run_id"]  # the run log says it was reused
    assert crun2.note == "two roles listed"
    assert _open_titles(session) == ["Backend Engineer", "Designer"]

    # a reuse of a reuse still names the run where the model actually read the page
    third = scrape.run(actor="cron", parallel=1)
    assert site.model_calls == 1
    assert _company_run(session, third["run_id"]).reused_from_run_id == first["run_id"]
    snap = store.snapshot(session)
    assert snap["companies"][0]["reused_from_run_id"] == first["run_id"]


def test_a_changed_page_goes_to_the_model_and_diffs_as_usual(session, site):
    scrape.run(actor="cron", parallel=1)
    site.html = PAGE_CHANGED
    second = scrape.run(actor="cron", parallel=1)
    assert site.model_calls == 2
    assert _company_run(session, second["run_id"]).reused_from_run_id is None
    assert second["per_company"][0]["new"] == 1 and second["per_company"][0]["closed"] == 0
    assert _open_titles(session) == ["Backend Engineer", "Data Analyst", "Designer"]


def test_after_a_failed_read_the_model_is_asked_again(session, site):
    scrape.run(actor="cron", parallel=1)

    # the page changes and the model fails on it
    site.html, site.model_fails = PAGE_CHANGED, True
    failed = scrape.run(actor="cron", parallel=1)
    assert site.model_calls == 2
    crun = _company_run(session, failed["run_id"])
    assert crun.status == "failed" and crun.page_hash is None and crun.extracted is None
    assert _open_titles(session) == ["Backend Engineer", "Designer"]   # an unread page closes nothing

    # the page is back to what the last good read saw, but the latest read
    # failed: that good read may not stand in for this week, the model is asked
    site.html, site.model_fails = PAGE, False
    third = scrape.run(actor="cron", parallel=1)
    assert site.model_calls == 3
    assert _company_run(session, third["run_id"]).reused_from_run_id is None


def test_only_a_successful_html_read_can_be_reused(session):
    comp = store.company_by_slug(session, "page-co")
    run = store.start_run(session, actor="cron", companies_total=1)
    store.record_company_result(session, run, comp, status="blocked", adapter="html",
                                page_hash="x" * 64, extracted={"rows": [], "note": None})
    store.finish_run(session, run)
    crun = _company_run(session, run.id)
    assert crun.page_hash is None and crun.extracted is None
    assert store.previous_read(session, comp.id) is None


def test_the_fingerprint_follows_text_url_and_prompt(monkeypatch):
    base = extract.page_hash("text", url="https://a.test")
    assert base == extract.page_hash("text", url="https://a.test")
    assert base != extract.page_hash("text!", url="https://a.test")
    assert base != extract.page_hash("text", url="https://b.test")
    monkeypatch.setattr(extract, "PROMPT", extract.PROMPT + " ")
    assert base != extract.page_hash("text", url="https://a.test")
