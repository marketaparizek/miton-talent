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


def test_a_role_that_comes_back_soon_is_a_relisting_on_a_new_row(session):
    comp = company(session)
    scrape_into(session, comp, ["Backend Engineer"])
    scrape_into(session, comp, [])
    run3, outcome = scrape_into(session, comp, ["Backend Engineer"])
    assert outcome["new"] == 0
    rows = session.query(PortfolioRole).filter_by(company_id=comp.id).order_by(PortfolioRole.id).all()
    assert len(rows) == 2
    assert sum(1 for r in rows if r.closed_at is None) == 1
    assert rows[1].replaces_role_id == rows[0].id and rows[1].relist_reason == "repost"
    assert rows[1].first_seen_original_at == rows[0].first_seen_at
    diff = store.changes(session, run3)
    assert diff["new"] == []
    assert [r["title"] for r in diff["relisted"]] == ["Backend Engineer"]


def test_a_role_that_comes_back_after_the_window_is_new(session):
    comp = company(session)
    scrape_into(session, comp, ["Backend Engineer"])
    scrape_into(session, comp, [])
    old = session.query(PortfolioRole).filter_by(company_id=comp.id).one()
    old.closed_at = old.closed_at - dt.timedelta(days=40)
    session.flush()
    run3, outcome = scrape_into(session, comp, ["Backend Engineer"])
    assert outcome["new"] == 1
    assert [r["title"] for r in store.changes(session, run3)["new"]] == ["Backend Engineer"]


# --- identity: board ids, retitles, relisting --------------------------------


def scrape_rows(session, comp, rows):
    run = store.start_run(session, actor="cron", companies_total=1)
    outcome = store.record_company_result(session, run, comp, status="ok", rows=rows,
                                          adapter="test", source_url=comp.careers_url)
    store.finish_run(session, run)
    return run, outcome


def test_a_retitle_under_the_same_board_id_is_the_same_role(session):
    comp = company(session)
    scrape_rows(session, comp, [row("Flow Lead", external_id="ashby:5606545e")])
    run2, outcome = scrape_rows(session, comp, [row("Flow Lead - Romanian Speaking", external_id="ashby:5606545e")])
    assert (outcome["new"], outcome["closed"]) == (0, 0)
    role = session.query(PortfolioRole).filter_by(company_id=comp.id).one()
    assert role.title == "Flow Lead - Romanian Speaking"
    assert [h["title"] for h in role.title_history] == ["Flow Lead"]
    assert role.key_tier == "ats"
    diff = store.changes(session, run2)
    assert diff["new"] == diff["closed"] == diff["relisted"] == []


def test_a_new_board_id_in_the_same_slot_is_relisted_not_hiring(session):
    comp = company(session)
    scrape_rows(session, comp, [row("Area Manager Chrášťany", "Chrášťany", external_id="ashby:775720f0"),
                                row("Accountant", "Prague", external_id="ashby:aaaa")])
    run2, outcome = scrape_rows(session, comp, [
        row("Area Manager ve skladu Chrášťany", "Chrášťany", external_id="ashby:4ee447a9"),
        row("Accountant", "Prague", external_id="ashby:aaaa"),
    ])
    assert (outcome["new"], outcome["closed"]) == (0, 0)
    diff = store.changes(session, run2)
    assert diff["new"] == [] and diff["closed"] == []
    assert diff["relisted"][0]["previous_title"] == "Area Manager Chrášťany"
    assert diff["relisted"][0]["reason"] == "same_slot"
    run = session.get(type(run2), run2.id)
    assert (run.roles_new, run.roles_closed) == (0, 0)


def test_two_closures_and_two_openings_at_one_site_are_not_paired(session):
    comp = company(session)
    scrape_rows(session, comp, [row("Picker", "Chrášťany", external_id="ashby:1"),
                                row("Packer", "Chrášťany", external_id="ashby:2")])
    _, outcome = scrape_rows(session, comp, [row("Forklift Driver", "Chrášťany", external_id="ashby:3"),
                                             row("Warehouse Clerk", "Chrášťany", external_id="ashby:4")])
    assert (outcome["new"], outcome["closed"]) == (2, 2)


def test_same_title_under_a_new_board_id_is_relisted(session):
    comp = company(session)
    scrape_rows(session, comp, [row("Customer Care Specialist", external_id="ashby:1")])
    run2, outcome = scrape_rows(session, comp, [row("Customer Care Specialist", external_id="ashby:2")])
    assert (outcome["new"], outcome["closed"]) == (0, 0)
    assert store.changes(session, run2)["relisted"][0]["reason"] == "same_title"


def test_a_role_from_before_identity_is_adopted_by_title(session):
    comp = company(session)
    scrape_into(session, comp, ["Backend Engineer"])
    legacy = session.query(PortfolioRole).filter_by(company_id=comp.id).one()
    legacy.role_key = None
    legacy.key_tier = None
    session.flush()
    _, outcome = scrape_rows(session, comp, [row("Backend Engineer", external_id="recruitee:42")])
    assert (outcome["new"], outcome["closed"]) == (0, 0)
    role = session.query(PortfolioRole).filter_by(company_id=comp.id).one()
    assert role.role_key == "ats:recruitee:42" and role.id == legacy.id


def test_rows_with_one_board_id_collapse_whatever_the_title():
    out = collapse([row("Developer", "Prague", external_id="x:1", url="https://a/1"),
                    row("Developer", "Brno", external_id="x:1", url="https://a/1"),
                    row("Developer", "Brno", external_id="x:2", url="https://a/2")])
    assert len(out) == 2
    assert len({r["fingerprint"] for r in out}) == 2


def test_two_postings_one_title_one_place_get_distinct_fingerprints():
    out = collapse([row("Picker", "Chrášťany", external_id="x:1"),
                    row("Picker", "Chrášťany", external_id="x:2")])
    assert len({r["fingerprint"] for r in out}) == 2


def test_identity_tiers():
    from talent.portfolio import identity
    rows = collapse([
        row("A", url="https://www.startupjobs.cz/nabidka/12345/a"),
        row("B", url="https://aim.test/hiring"),
        row("C", url="https://aim.test/hiring"),
        row("D", url="https://acme.test/careers/d"),
        row("E", url="https://acme.test/careers"),
        row("F", url="https://career.rohlik.group/cz/jobs/5606545e-1111-2222-3333-444455556666"),
        row("G", url="https://acme.peopleforce.io/careers/v/247739"),
    ])
    identity.assign_keys(rows, careers_url="https://acme.test/careers/")
    keys = {r["title"]: (r["key_tier"], r["role_key"]) for r in rows}
    assert keys["A"] == ("ats", "ats:startupjobs:12345")
    assert keys["B"][0] == keys["C"][0] == "heur"       # one URL for two roles
    assert keys["D"] == ("url", "url:acme.test/careers/d")
    assert keys["E"][0] == "heur"                        # the careers page itself
    assert keys["F"] == ("ats", "ats:ashby:5606545e-1111-2222-3333-444455556666")
    assert keys["G"] == ("ats", "ats:peopleforce:247739")


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


def test_the_snapshot_carries_one_history_row_per_finished_run(session):
    """The hub's stat tiles need "against last week": one row per finished run
    with the open, technical and hiring counts as they stood at its end."""
    seed.seed(session)
    first = store.snapshot(session)["history"]
    assert len(first) == 1 and first[0]["baseline"] is True
    base_open = first[0]["open"]

    comp = store.company_by_slug(session, "aim")
    run = store.start_run(session, actor="cron", companies_total=1)
    store.record_company_result(
        session, run, comp, status="ok", adapter="html",
        rows=[row("Senior Backend Engineer"), row("Head of Growth")],
    )
    store.finish_run(session, run)

    hist = store.snapshot(session)["history"]
    assert [h["run_id"] for h in hist] == [first[0]["run_id"], run.id]
    assert hist[0]["open"] == base_open, "an earlier row does not move when a later run closes roles"
    last = hist[1]
    assert last["baseline"] is False
    assert last["new"] == 1 and last["open"] == len(store.open_roles(session))
    assert last["technical"] == sum(1 for r in store.open_roles(session) if r.is_technical)
    assert last["companies_hiring"] == len({r.company_id for r in store.open_roles(session)})


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


# --- requisitions: postings of one job grouped (Rohlik) -----------------------

ROHLIK_RULES = {
    "sites": ["Chrastany", "Brno FC", "Garching", "Bischofsheim"],
    "same_job": [["(customer care|zakaznick).*(german|nemc)", "customer care specialist german"]],
}


def test_postings_of_one_job_become_one_requisition():
    from talent.portfolio.collapse import group_requisitions
    rows = collapse([
        row("Customer Care Specialist (German)", "Prague", external_id="ashby:1", url="u1"),
        row("Customer Care Specialist (German) - PART-TIME 20hrs/week", "Prague", external_id="ashby:2", url="u2"),
        row("Specialista/ka zákaznické péče s němčinou", "Prague", external_id="ashby:3", url="u3"),
        row("Head of Applied Science", "Prague", external_id="ashby:4", url="u4"),
        row("Head of Applied Science", "München", external_id="ashby:5", url="u5"),
        row("Vedoucí směny", "Chrášťany - Prague", external_id="ashby:6", url="u6"),
        row("Vedoucí směny", "Brno FC", external_id="ashby:7", url="u7"),
        row("Staplerfahrer (m/w/d)", "Bischofsheim bei Frankfurt", external_id="ashby:8", url="u8"),
        row("Staplerfahrer (m/w/d)", "Bischofsheim bei Frankfurt", external_id="ashby:9", url="u9"),
        row("Account Executive", "Group career pages", external_id="ashby:10", url="u10"),
    ])
    grouped = group_requisitions(rows, ROHLIK_RULES)
    assert len(grouped) == 6
    out = {r["title"]: r for r in grouped}
    assert out["Customer Care Specialist (German)"]["postings"] == 3
    assert out["Head of Applied Science"]["locations"] == ["Prague", "München"]
    assert sum(1 for r in grouped if r["title"] == "Vedoucí směny") == 2
    assert out["Staplerfahrer (m/w/d)"]["external_ids"] == ["ashby:8", "ashby:9"]
    assert out["Account Executive"]["locations"] == []          # a channel, not a place


def test_grouping_postings_already_stored_is_not_a_wave_of_closures(session):
    comp = company(session, slug="rohlik")
    postings = [row("Head of Applied Science", "Prague", external_id="ashby:4", url="https://jobs.ashbyhq.com/r/4"),
                row("Head of Applied Science", "München", external_id="ashby:5", url="https://jobs.ashbyhq.com/r/5")]
    scrape_rows(session, comp, postings)
    assert session.query(PortfolioRole).filter_by(closed_at=None).count() == 2
    comp.config = {"requisitions": ROHLIK_RULES}
    session.flush()
    run2, outcome = scrape_rows(session, comp, postings)
    assert (outcome["roles"], outcome["new"], outcome["closed"]) == (1, 0, 0)
    diff = store.changes(session, run2)
    assert diff["new"] == diff["closed"] == diff["relisted"] == []
    # and the week after is quiet too
    _, outcome = scrape_rows(session, comp, postings)
    assert (outcome["new"], outcome["closed"]) == (0, 0)


def test_a_retitled_posting_inside_a_requisition_keeps_its_role(session):
    comp = company(session, slug="rohlik", config={"requisitions": ROHLIK_RULES})
    scrape_rows(session, comp, [row("Flow Lead", "Garching", external_id="ashby:1")])
    _, outcome = scrape_rows(session, comp, [row("Flow Lead - Romanian Speaking", "Garching", external_id="ashby:1")])
    assert (outcome["new"], outcome["closed"]) == (0, 0)
    role = session.query(PortfolioRole).filter_by(closed_at=None).one()
    assert role.key_tier == "req" and [h["title"] for h in role.title_history] == ["Flow Lead"]


# --- how long a role has been open --------------------------------------------


def test_board_dates_parse():
    assert adapters._date("2026-10-23T12:37:47.594+00:00") == "2026-10-23"
    assert adapters._date("2026-10-15 09:38:10 UTC") == "2026-10-15"
    assert adapters._date(1760000000000) == "2025-10-09"
    assert adapters._date(None) is None and adapters._date("soon") is None


def test_age_comes_from_the_board_when_it_is_older_than_our_first_sighting(session):
    comp = company(session)
    scrape_rows(session, comp, [row("Java Developer", external_id="x:1", posted_at="2026-04-16")])
    entry = next(c for c in store.snapshot(session)["companies"] if c["slug"] == "acme")
    r = entry["roles"][0]
    assert (r["open_since"], r["open_since_source"]) == ("2026-04-16", "board")
    assert r["age_days"] >= 190


def test_a_requisition_is_as_old_as_its_oldest_posting(session):
    from talent.portfolio.collapse import group_requisitions
    rows = group_requisitions(collapse([
        row("Head of Applied Science", "Prague", external_id="a:1", posted_at="2026-09-01"),
        row("Head of Applied Science", "München", external_id="a:2", posted_at="2026-06-01"),
    ]), ROHLIK_RULES)
    assert rows[0]["posted_at"] == "2026-06-01"


def test_without_a_board_date_age_runs_from_first_seen_and_baseline_is_a_floor(session):
    comp = company(session)
    scrape_into(session, comp, ["Recruiter"], run_actor="seed")
    scrape_into(session, comp, ["Recruiter", "Designer"])
    roles = {r["title"]: r for r in next(c for c in store.snapshot(session)["companies"]
                                         if c["slug"] == "acme")["roles"]}
    assert roles["Recruiter"]["open_since_source"] == "before"
    assert roles["Designer"]["open_since_source"] == "seen"
    assert roles["Designer"]["age_days"] == 0


def test_a_closed_role_reports_how_long_it_was_open(session):
    comp = company(session)
    scrape_rows(session, comp, [row("Analyst", external_id="x:1", posted_at="2026-09-01")])
    run2, _ = scrape_rows(session, comp, [])
    closed = store.changes(session, run2)["closed"][0]
    assert closed["open_days"] == (dt.datetime.now(dt.timezone.utc).date() - dt.date(2026, 9, 1)).days
