"""Portfolio open roles: the fingerprint, the classifier, the weekly diff, the page.

The rule these tests exist for: a careers page that cannot be read must never
close a role. Everything else (collapsing, buckets, the JSON) is here so a
change to the scraper cannot quietly change the numbers.
"""
import datetime as dt
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app as chat  # noqa: E402
from talent import db  # noqa: E402
from talent.models import Base, PortfolioRole  # noqa: E402
from talent.portfolio import adapters, classify, extract, seed, store  # noqa: E402
from talent.portfolio.collapse import collapse, fingerprint  # noqa: E402

SECRET = "test-handoff-secret-shared-with-alister-32plus"


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 'p.db'}")
    Base.metadata.create_all(db.engine())
    monkeypatch.setenv("PORTFOLIO_CLASSIFY_LLM", "0")
    with db.session() as s:
        yield s


def company(session, slug="acme", **kw):
    return store.upsert_company(session, slug, name=kw.pop("name", slug.title()),
                                careers_url=kw.pop("careers_url", "https://acme.test/careers"), **kw)


def row(title, location="Prague", **kw):
    return dict({"title": title, "location": location}, **kw)


# --- fingerprint and collapsing ----------------------------------------------


def test_one_requisition_in_four_cities_is_one_role():
    rows = [row("Area Manager", "Brno"), row("Area Manager", "Prague"),
            row("Area Manager", "Ostrava"), row("Area Manager", "Liboc, Prague")]
    out = collapse(rows)
    assert len(out) == 1
    assert out[0]["locations"] == ["Brno", "Prague", "Ostrava", "Liboc, Prague"]
    assert out[0]["location"] == "Brno / Prague / Ostrava / Liboc, Prague"


def test_gender_and_bracket_variants_are_the_same_requisition():
    assert fingerprint("Warehouse Trainer (m/w/d)") == fingerprint("Warehouse Trainer")
    assert fingerprint("Koordinátor*ka klinických studií") == fingerprint("Koordinátor klinických studií")
    assert fingerprint("Data Analytics Engineer (medior/senior)") == fingerprint("Data Analytics Engineer")


def test_seniority_and_different_jobs_stay_apart():
    assert fingerprint("Senior Backend Engineer") != fingerprint("Backend Engineer")
    assert fingerprint("Backend Developer") != fingerprint("Frontend Developer")


def test_two_shops_hiring_the_same_job_stay_two_roles():
    # One requisition reposted per city repeats the title unchanged; a title that
    # names the place is a separate hire and must survive collapsing.
    assert fingerprint("Konzultant prodeje - Poděbradská") != fingerprint("Konzultant prodeje - Jeneč")
    assert fingerprint("Area Manager") == fingerprint("Area manager")


def test_a_meaningful_bracket_survives_but_noise_does_not():
    assert fingerprint("Lead (Inbound)") != fingerprint("Lead (Outbound)")
    assert fingerprint("Warehouse Trainer (m/w/d)") == fingerprint("Warehouse Trainer")
    assert fingerprint("Backend Developer (Junior/Medior)") == fingerprint("Backend Developer")


def test_one_title_in_two_places_stays_two_roles_with_two_fingerprints():
    rows = [row("Account Executive", "Dublin", url="https://acme.test/o/ae-dublin"),
            row("Account Executive", "New York", url="https://acme.test/o/ae-ny")]
    out = collapse(rows)
    assert len(out) == 2
    assert len({r["fingerprint"] for r in out}) == 2
    # The same two postings next week fingerprint the same way.
    assert {r["fingerprint"] for r in collapse(rows)} == {r["fingerprint"] for r in out}


def test_one_posting_repeated_per_city_is_one_role_even_with_one_url():
    rows = [row("Area Manager", "Brno", url="https://acme.test/o/am"),
            row("Area Manager", "Prague", url="https://acme.test/o/am")]
    assert len(collapse(rows)) == 1


def test_aliases_merge_cross_language_copies():
    rows = [row("Warengruppenmanager Frische", "Vienna"), row("Category Manager Fresh", "Prague")]
    aliases = {fingerprint("Warengruppenmanager Frische"): fingerprint("Category Manager Fresh")}
    assert len(collapse(rows, aliases)) == 1


# --- classification ----------------------------------------------------------


@pytest.mark.parametrize("title,expected", [
    ("Senior Backend Engineer", "Engineering & AI"),
    ("ML Research Engineer", "Engineering & AI"),
    ("Data Analytics Engineer", "Data & Analytics"),
    ("Principal Product Manager: Fulfillment Center", "Product & Design"),
    ("Last Mile Operations Manager", "Ops & Logistics"),
    ("Account Executive", "Sales & BD"),
    ("Category Manager Wine & Alcohol", "Marketing & Commercial"),
    ("Senior Legal Counsel", "Finance & Legal"),
    ("Recruiter", "People & Support"),
    ("Lekar*ka - psychiatr", "Clinical"),
])
def test_function_buckets_come_from_the_title(title, expected):
    assert classify.function_by_rules(title) == expected


def test_technical_is_the_three_buckets_only():
    assert classify.is_technical("Engineering & AI")
    assert classify.is_technical("Product & Design")
    assert not classify.is_technical("Ops & Logistics")
    assert not classify.is_technical(None)


@pytest.mark.parametrize("location,cc", [
    ("Opatovice nad Labem", "CZ"), ("Praha 6", "CZ"), ("Warsaw", "PL"),
    ("Garching / Bischofsheim", "DE"), ("Budapest", "HU"), ("Bratislava", "SK"),
    ("New York", "US"), ("Remote", "Remote"), ("Mars", "Other"),
])
def test_country_comes_from_the_location(location, cc):
    assert classify.country_of(location) == cc


def test_rows_without_a_model_fall_back_to_other(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_CLASSIFY_LLM", "0")
    rows = classify.classify_rows([row("Zahradník na půl úvazku")])
    assert rows[0]["fn"] == "Other"
    assert rows[0]["fn_source"] == "rules"
    assert rows[0]["is_technical"] is False


# --- the weekly diff ---------------------------------------------------------


def scrape_into(session, comp, titles, *, status="ok", run_actor="cron"):
    run = store.start_run(session, actor=run_actor, companies_total=1)
    outcome = store.record_company_result(
        session, run, comp, status=status,
        rows=[row(t) for t in titles], adapter="test", source_url=comp.careers_url,
    )
    store.finish_run(session, run)
    return run, outcome


def test_first_run_creates_roles_and_second_run_diffs_them(session):
    comp = company(session)
    scrape_into(session, comp, ["Backend Engineer", "Recruiter"])
    run2, outcome = scrape_into(session, comp, ["Backend Engineer", "Data Analyst"])

    assert outcome == {"status": "ok", "roles": 2, "new": 1, "closed": 1}
    diff = store.changes(session, run2)
    assert [r["title"] for r in diff["new"]] == ["Data Analyst"]
    assert [r["title"] for r in diff["closed"]] == ["Recruiter"]
    assert diff["roles_now"] == 2


def test_a_page_that_could_not_be_read_closes_nothing(session):
    comp = company(session)
    scrape_into(session, comp, ["Backend Engineer", "Recruiter"])

    run2 = store.start_run(session, actor="cron", companies_total=1)
    outcome = store.record_company_result(
        session, run2, comp, status="failed", rows=[], adapter="html",
        error="HTTP 403", source_url=comp.careers_url,
    )
    store.finish_run(session, run2)

    assert outcome["closed"] == 0
    assert outcome["roles"] == 2            # last week's count, reported as-is
    assert len(store.open_roles(session, comp.id)) == 2
    snap = store.snapshot(session)
    entry = next(c for c in snap["companies"] if c["slug"] == "acme")
    assert entry["read_ok"] is False
    assert entry["verified"] is False
    assert "403" in entry["note"]
    assert snap["health"]["unverified"] == ["Acme"]


def test_a_company_with_no_careers_page_is_a_known_zero_not_an_unread_one(session):
    comp = company(session, "silent", name="Silent", careers_url=None)
    run = store.start_run(session, actor="cron", companies_total=1)
    store.record_company_result(session, run, comp, status="no_careers_page", rows=[])
    store.finish_run(session, run)
    entry = next(c for c in store.snapshot(session)["companies"] if c["slug"] == "silent")
    assert entry["read_ok"] is False      # nothing was read, because there is nothing to read
    assert entry["verified"] is True      # ...so the zero is this week's, not last week's
    assert store.snapshot(session)["health"]["unverified"] == []


def test_a_real_zero_closes_every_role(session):
    comp = company(session)
    scrape_into(session, comp, ["Backend Engineer"])
    run2, outcome = scrape_into(session, comp, [])
    assert outcome == {"status": "ok", "roles": 0, "new": 0, "closed": 1}
    assert store.open_roles(session, comp.id) == []
    assert store.changes(session, run2)["went_quiet"] == ["Acme"]


def test_started_hiring_is_reported(session):
    comp = company(session)
    scrape_into(session, comp, [])
    run2 = scrape_into(session, comp, ["Backend Engineer"])[0]
    assert store.changes(session, run2)["started_hiring"] == ["Acme"]


def test_a_role_that_comes_back_is_a_new_row_not_a_resurrection(session):
    comp = company(session)
    scrape_into(session, comp, ["Backend Engineer"])
    scrape_into(session, comp, [])
    run3, outcome = scrape_into(session, comp, ["Backend Engineer"])
    assert outcome["new"] == 1
    rows = session.query(PortfolioRole).filter_by(company_id=comp.id).all()
    assert len(rows) == 2
    assert sum(1 for r in rows if r.closed_at is None) == 1
    assert [r["title"] for r in store.changes(session, run3)["new"]] == ["Backend Engineer"]


def test_snapshot_carries_the_classification(session):
    comp = company(session)
    scrape_into(session, comp, ["Senior Backend Engineer"])
    entry = next(c for c in store.snapshot(session)["companies"] if c["slug"] == "acme")
    role = entry["roles"][0]
    assert role["fn"] == "Engineering & AI"
    assert role["tech"] is True
    assert role["cc"] == "CZ"


# --- the seed and the baseline ------------------------------------------------


def test_seed_loads_the_portfolio_and_the_baseline_once(session):
    out = seed.seed(session)
    assert out["companies_created"] == 44
    assert out["baseline"]["roles"] == 117
    assert out["baseline"]["date"] == "2026-09-14"

    snap = store.snapshot(session)
    assert len(snap["companies"]) == 44
    assert sum(len(c["roles"]) for c in snap["companies"]) == 117
    # The import is not a week of news.
    assert snap["changes"]["baseline"] is True
    assert snap["changes"]["new"] == []
    # ...and its roles were open on the baseline date, not created today, or
    # every one of them would count as new this week.
    first_seen = {r["first_seen"] for c in snap["companies"] for r in c["roles"]}
    assert first_seen == {"2026-09-14"}

    again = seed.seed(session)
    assert again["companies_created"] == 0
    assert again["companies_updated"] == 44
    assert again["baseline"] is None


def test_seed_gives_every_company_an_adapter_that_exists(session):
    seed.seed(session, with_baseline=False)
    for comp in store.companies(session):
        if comp.careers_url:
            assert comp.adapter in adapters.ADAPTERS


def test_the_snapshot_says_how_many_real_scrapes_there_have_been(session):
    """"New this week" is meaningless until two real runs can be compared: against
    the imported baseline, every row the artifact never listed looks new."""
    seed.seed(session)
    assert store.snapshot(session)["health"]["scrapes"] == 0

    comp = store.company_by_slug(session, "aim")
    run = store.start_run(session, actor="cron", companies_total=1)
    store.record_company_result(session, run, comp, status="ok", rows=[row("Head of Growth")])
    store.finish_run(session, run)
    assert store.snapshot(session)["health"]["scrapes"] == 1


def test_the_first_real_run_after_the_baseline_reports_only_what_changed(session):
    seed.seed(session)
    comp = store.company_by_slug(session, "aim")
    before = {r.title for r in store.open_roles(session, comp.id)}
    assert "Senior Backend Engineer" in before

    run = store.start_run(session, actor="cron", companies_total=1)
    store.record_company_result(
        session, run, comp, status="ok", adapter="html",
        rows=[row("Senior Backend Engineer"), row("Head of Growth")],
    )
    store.finish_run(session, run)
    diff = store.changes(session, run)
    assert [r["title"] for r in diff["new"]] == ["Head of Growth"]
    assert {r["title"] for r in diff["closed"]} == before - {"Senior Backend Engineer"}


# --- adapters ----------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://confirmo.recruitee.com/", "recruitee"),
    ("https://career.rohlik.group/group/jobs", "ashby"),
    ("https://jobs.ashbyhq.com/rohlik", "ashby"),
    ("https://careers.smartrecruiters.com/GLAMI1", "smartrecruiters"),
    ("https://jobs.recruitis.io/knihobot", "recruitis"),
    ("https://livendo.jobs.personio.com/?language=cs", "personio"),
    ("https://www.startupjobs.cz/startup/reas-cz", "startupjobs"),
    ("https://www.psyon.cz/nabidka-prace/", "html"),
    (None, "html"),
])
def test_adapter_detection_follows_the_host(url, expected):
    assert adapters.detect(url) == expected


def test_no_careers_url_is_not_a_failure():
    result = adapters.read(None)
    assert result.status == "no_careers_page"
    assert result.rows == []


def test_an_adapter_that_raises_comes_back_as_failed(monkeypatch):
    def boom(url, config):
        raise RuntimeError("network on fire")
    monkeypatch.setitem(adapters.ADAPTERS, "recruitee", boom)
    result = adapters.read("https://x.recruitee.com/", config={"fallback_html": False})
    assert result.status == "failed"
    assert "network on fire" in result.error


def test_a_broken_ats_board_falls_back_to_reading_the_page(monkeypatch):
    def gone(url, config):
        return adapters.Result("failed", adapter="personio", error="no XML feed (HTTP 404)")

    def page(url, config):
        return adapters.Result("ok", [{"title": "Správce nemovitostí", "location": "Brno"}], "html")

    monkeypatch.setitem(adapters.ADAPTERS, "personio", gone)
    monkeypatch.setitem(adapters.ADAPTERS, "html", page)
    result = adapters.read("https://x.jobs.personio.com/?language=cs")
    assert result.status == "ok"
    assert result.adapter == "html"
    assert "personio board unavailable" in result.note


def test_an_open_application_row_is_not_a_role(monkeypatch):
    def page(url, config):
        return adapters.Result("ok", [
            {"title": "No position for you? Reach out anyway!"},
            {"title": "Otevřená přihláška"},
            {"title": "Backend Developer"},
        ], "html")

    monkeypatch.setitem(adapters.ADAPTERS, "html", page)
    result = adapters.read("https://x.test/careers")
    assert [r["title"] for r in result.rows] == ["Backend Developer"]
    assert "open-application" in result.note


def test_ashby_expands_secondary_locations_so_collapsing_keeps_every_city(monkeypatch):
    payload = {"jobs": [{
        "title": "Area Manager", "team": "Operations", "employmentType": "FullTime",
        "location": "Prague", "secondaryLocations": ["Brno", {"location": "Ostrava"}],
        "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/x/1",
    }]}

    class FakeResponse:
        status_code = 200
        def json(self):
            return payload

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr(adapters, "_client", lambda: FakeClient())
    result = adapters.read("https://jobs.ashbyhq.com/x")
    assert result.status == "ok"
    assert [r["location"] for r in result.rows] == ["Prague", "Brno", "Ostrava"]
    assert len(collapse(result.rows)) == 1


def _fake_page(text, status=200):
    class FakeResponse:
        status_code = status
    FakeResponse.text = text

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, *a, **kw):
            return FakeResponse()
    return FakeClient


RECRUITIS_BOARD = """
<div id="jobs-wrapper">
<div class="row job g-mt-30"><div class="col-sm-9">
<h3><a href="/knihobot/497106-team-leader">Team Leader v logistick&eacute;m centru</a></h3>
<p class="row-info"><span class="job-item x">&nbsp;Praha 15,&nbsp;CZ</span>
<span class="job-item x">&nbsp;Logistika</span><span class="job-item x">&nbsp;Pr&aacute;ce na pln&yacute; &uacute;vazek</span></p>
</div></div>
<div class="row job g-mt-30"><div class="col-sm-9">
<h3><a href="/knihobot/460888-no-position">No position for you? Reach out anyway!</a></h3>
<p class="row-info"><span class="job-item x">&nbsp;Praha,&nbsp;CZ</span></p>
</div></div>
</div>
"""

STARTUPJOBS_PROFILE = """
<section id="offers">
<a href="/nabidka/107623/hr-generalista"><div><span>Reas.cz</span><span>HR generalista/ka</span>
<span>40 - 60 000 K&ccaron; / m&ecaron;s&iacute;c</span><span>Hybrid, Onsite</span><span>Praha</span><span>Full-time</span>
<svg><path d="M19.25 20.2515V4.75C19.25 3.64543"/></svg></div></a>
<a href="/nabidka/107623/hr-generalista"><div><span>Reas.cz</span><span>HR generalista/ka</span></div></a>
<a href="/nabidka/107999/backend-developer"><div><span>Reas.cz</span><span>Backend Developer</span>
<span>Onsite</span><span>Brno</span><span>Part-time</span></div></a>
</section>
"""


def test_the_recruitis_board_is_read_without_a_model(monkeypatch):
    monkeypatch.setattr(adapters, "_client", _fake_page(RECRUITIS_BOARD))
    result = adapters.read("https://jobs.recruitis.io/knihobot")
    assert result.status == "ok"
    assert [r["title"] for r in result.rows] == ["Team Leader v logistickém centru"]
    assert result.rows[0]["location"] == "Praha 15, CZ"
    assert result.rows[0]["url"] == "https://jobs.recruitis.io/knihobot/497106-team-leader"
    assert "open-application" in result.note


def test_a_startupjobs_profile_is_read_once_per_ad(monkeypatch):
    monkeypatch.setattr(adapters, "_client", _fake_page(STARTUPJOBS_PROFILE))
    result = adapters.read("https://www.startupjobs.cz/startup/reas-cz")
    assert result.status == "ok"
    assert [r["title"] for r in result.rows] == ["HR generalista/ka", "Backend Developer"]
    assert [r["location"] for r in result.rows] == ["Praha", "Brno"]
    assert result.rows[0]["url"] == "https://www.startupjobs.cz/nabidka/107623/hr-generalista"


def test_a_client_side_page_is_blocked_not_zero(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "<html><body><div id='root'></div></body></html>"

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr(adapters, "_client", lambda: FakeClient())
    result = adapters.read("https://graet.test/careers")
    assert result.status == "blocked"
    assert "client-side" in result.error


def test_the_html_reader_needs_a_model(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(extract.ExtractorUnavailable):
        extract.roles_from_text("x" * 1000, url="https://x.test")


def test_readable_text_drops_scripts_and_markup():
    text = extract.readable_text("<html><head><style>a{}</style></head><body>"
                                 "<script>var x=1</script><h1>Careers</h1><p>Backend&nbsp;Developer</p></body></html>")
    assert "var x" not in text
    assert "Careers" in text
    assert "Backend Developer" in text


# --- the page ----------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 'w.db'}")
    Base.metadata.create_all(db.engine())
    monkeypatch.setenv("TALENT_HANDOFF_SECRET", SECRET)
    monkeypatch.setenv("ALISTER_BASE_URL", "https://alister.example.test")
    monkeypatch.setenv("PORTFOLIO_CLASSIFY_LLM", "0")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with db.session() as s:
        seed.seed(s)
    return TestClient(chat.app)


def signed_in(client):
    import jwt
    now = dt.datetime.now(dt.timezone.utc)
    token = jwt.encode({
        "sub": "1", "email": "kolega@miton.cz", "role": "miton",
        "purpose": "talent-handoff", "aud": "miton-talent", "jti": os.urandom(8).hex(),
        "iat": now, "exp": now + dt.timedelta(seconds=60),
    }, SECRET, algorithm="HS256")
    assert client.get(f"/auth/callback?token={token}", follow_redirects=False).status_code in (302, 303, 307)
    return client


def test_the_dashboard_needs_the_alister_session(client):
    for path in ("/admin/portfolio", "/admin/portfolio.json"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303, 307, 401), path
        if r.status_code != 401:
            assert "alister.example.test" in r.headers["location"]
    assert client.post("/admin/portfolio/scrape", follow_redirects=False).status_code in (302, 303, 307, 401)


def test_the_dashboard_renders_the_snapshot(client):
    signed_in(client)
    r = client.get("/admin/portfolio")
    assert r.status_code == 200
    assert "Portfolio hiring" in r.text
    assert "__DATA__" not in r.text and "__USER__" not in r.text
    assert "Rohlik Group" in r.text
    assert "kolega@miton.cz" in r.text


def test_the_json_is_the_same_snapshot(client):
    signed_in(client)
    data = client.get("/admin/portfolio.json").json()
    assert len(data["companies"]) == 44
    assert sum(len(c["roles"]) for c in data["companies"]) == 117
    assert data["scraped_at"] == "2026-09-14"
    assert data["health"]["ran"] is True


def _service_token(secret=SECRET, purpose="portfolio-read", ttl=120, **over):
    import jwt
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": "alister", "purpose": purpose, "aud": "miton-talent",
              "iat": now, "exp": now + dt.timedelta(seconds=ttl)}
    claims.update(over)
    return jwt.encode(claims, secret, algorithm="HS256")


def test_the_service_snapshot_needs_a_token(client):
    assert client.get("/api/portfolio/snapshot").status_code == 401
    assert client.get("/api/portfolio/snapshot",
                      headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_a_valid_service_token_gets_the_snapshot(client):
    r = client.get("/api/portfolio/snapshot",
                   headers={"Authorization": f"Bearer {_service_token()}"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["companies"]) == 44
    assert sum(len(c["roles"]) for c in data["companies"]) == 117


@pytest.mark.parametrize("token_kwargs,why", [
    ({"secret": "another-secret-that-is-also-32-characters-long"}, "wrong secret"),
    ({"purpose": "talent-handoff"}, "a sign-in token is not a read token"),
    ({"ttl": 3600}, "lifetime beyond the ceiling"),
    ({"aud": "somebody-else"}, "wrong audience"),
    ({"ttl": -60}, "expired"),
])
def test_a_bad_service_token_is_refused(client, token_kwargs, why):
    token = _service_token(**token_kwargs)
    assert client.get("/api/portfolio/snapshot",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401, why


def test_a_service_token_cannot_open_a_session(client):
    token = _service_token()
    r = client.get(f"/auth/callback?token={token}", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "alister.example.test" in r.headers["location"]
    assert client.get("/admin/portfolio", follow_redirects=False).status_code in (302, 303, 307, 401)


def test_the_scrape_button_starts_one_run_at_a_time(client, monkeypatch):
    signed_in(client)
    import threading
    gate = threading.Event()
    calls = []

    def slow_run(**kw):
        calls.append(kw)
        gate.wait(5)
        return {}

    monkeypatch.setattr("talent.portfolio.view.scraper.run", slow_run)
    first = client.post("/admin/portfolio/scrape")
    assert first.status_code == 200 and first.json()["started"] is True
    second = client.post("/admin/portfolio/scrape")
    assert second.status_code == 409
    gate.set()
    assert calls and calls[0]["actor"] == "kolega@miton.cz"
