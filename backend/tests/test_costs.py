"""Running costs: provider bills from the cost APIs and invoices typed in by hand.

No test talks to Anthropic or OpenAI: httpx gets a MockTransport.
"""
import datetime as dt
import json
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app as chat  # noqa: E402
from talent import costs, db  # noqa: E402
from talent.models import Base, ProviderCost  # noqa: E402
from tests.test_usage import SECRET, sign_in  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 'c.db'}")
    Base.metadata.create_all(db.engine())
    for k in ("ANTHROPIC_ADMIN_KEY", "OPENAI_ADMIN_KEY", "COST_EUR_USD"):
        monkeypatch.delenv(k, raising=False)


def rows(provider=None):
    with db.session() as s:
        q = s.query(ProviderCost)
        if provider:
            q = q.filter(ProviderCost.provider == provider)
        return sorted(q.all(), key=lambda r: (r.day, r.scope))


# --- Anthropic ----------------------------------------------------------------------


def anthropic_transport(seen):
    def handler(req: httpx.Request):
        seen.append(req)
        assert req.headers["x-api-key"] == "sk-ant-admin-test"
        if req.url.path.endswith("/workspaces"):
            return httpx.Response(200, json={"data": [{"id": "wrkspc_1", "name": "Alister"}],
                                             "has_more": False, "last_id": "wrkspc_1"})
        assert req.headers["anthropic-beta"] == "analytics-usage-cost-2026-08-24"
        assert req.url.params["time_bucket_width"] == "1d"
        page = req.url.params.get("page")
        if not page:
            return httpx.Response(200, json={"data": [
                {"starting_at": "2026-11-01T00:00:00Z", "ending_at": "2026-11-02T00:00:00Z",
                 "group": {"workspace_id": "wrkspc_1"}, "cost": {"currency": "USD", "amount": "1234.500000"}},
            ], "has_more": True, "next_page": "p2"})
        return httpx.Response(200, json={"data": [
            {"starting_at": "2026-11-02T00:00:00Z", "ending_at": "2026-11-03T00:00:00Z",
             "group": {"workspace_id": None}, "cost": {"currency": "USD", "amount": "50.000000"}},
        ], "has_more": False, "next_page": None})
    return httpx.MockTransport(handler)


def test_anthropic_amounts_are_cents_and_pages_are_followed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin-test")
    seen = []
    out = costs.fetch_anthropic(dt.date(2026, 11, 1), dt.date(2026, 11, 3),
                                http=httpx.Client(transport=anthropic_transport(seen)))
    assert out == [
        {"day": dt.date(2026, 11, 1), "scope": "Alister", "usd": pytest.approx(12.345)},
        {"day": dt.date(2026, 11, 2), "scope": "Default", "usd": pytest.approx(0.5)},
    ]


def test_anthropic_long_ranges_are_split_into_31_day_requests(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin-test")
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path.endswith("/workspaces"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(200, json={"data": [], "has_more": False})
    costs.fetch_anthropic(dt.date(2026, 1, 1), dt.date(2026, 3, 15),
                          http=httpx.Client(transport=httpx.MockTransport(handler)))
    spans = [(r.url.params["starting_at"], r.url.params["ending_at"]) for r in seen if "analytics" in r.url.path]
    assert spans == [("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
                     ("2026-02-01T00:00:00Z", "2026-03-04T00:00:00Z"),
                     ("2026-03-04T00:00:00Z", "2026-03-15T00:00:00Z")]


# --- OpenAI -----------------------------------------------------------------------------


def openai_transport():
    def handler(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer sk-admin-test"
        if req.url.path.endswith("/projects"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "proj_1", "name": "talent-sourcing"}],
                                             "has_more": False})
        assert req.url.params["group_by"] == "project_id"
        day = int(dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc).timestamp())
        return httpx.Response(200, json={"object": "page", "data": [
            {"object": "bucket", "start_time": day, "end_time": day + 86400, "results": [
                {"object": "organization.costs.result", "amount": {"value": 4.2, "currency": "usd"}, "project_id": "proj_1"},
                {"object": "organization.costs.result", "amount": {"value": 0.8, "currency": "usd"}, "project_id": "proj_1"},
                {"object": "organization.costs.result", "amount": {"value": 1.0, "currency": "usd"}, "project_id": None},
            ]}], "has_more": False, "next_page": None})
    return httpx.MockTransport(handler)


def test_openai_costs_per_project_are_stored_and_summed(fresh_db, monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-test")
    out = costs.fetch_openai(dt.date(2026, 11, 1), dt.date(2026, 11, 5), http=httpx.Client(transport=openai_transport()))
    costs.store_api_rows("openai", dt.date(2026, 11, 1), dt.date(2026, 11, 5), out)
    got = [(r.day, r.scope, r.amount_micro) for r in rows("openai")]
    assert got == [(dt.date(2026, 11, 3), "bez projektu", 1_000_000), (dt.date(2026, 11, 3), "talent-sourcing", 5_000_000)]


def test_a_resync_replaces_the_days_it_read_and_keeps_older_ones(fresh_db):
    d = dt.date
    costs.store_api_rows("openai", d(2026, 10, 1), d(2026, 10, 3),
                         [{"day": d(2026, 10, 1), "scope": "p", "usd": 1.0}, {"day": d(2026, 10, 2), "scope": "p", "usd": 2.0}])
    costs.store_api_rows("openai", d(2026, 10, 2), d(2026, 10, 3), [{"day": d(2026, 10, 2), "scope": "p", "usd": 2.5}])
    assert [(r.day.day, r.amount_micro) for r in rows("openai")] == [(1, 1_000_000), (2, 2_500_000)]


def test_sync_skips_a_provider_without_a_key_and_survives_one_failing(fresh_db, monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-test")

    def broken(start, end):
        raise costs.SyncError("OpenAI 500: down")
    monkeypatch.setitem(costs.FETCHERS, "openai", broken)
    out = costs.sync(today=dt.date(2026, 11, 22))
    assert out["anthropic"] == {"ok": False, "error": "chybí klíč"}
    assert out["openai"]["ok"] is False and "500" in out["openai"]["error"]


# --- by hand + summary ------------------------------------------------------------------


def test_manual_month_upserts_and_zero_deletes(fresh_db):
    costs.set_manual("hetzner", "2026-10", 40, "EUR", "fa 123", "m@miton.cz")
    costs.set_manual("hetzner", "2026-10", 42.5, "EUR", None, "m@miton.cz")
    (r,) = rows("hetzner")
    assert (r.day, r.amount_micro, r.currency, r.note) == (dt.date(2026, 10, 1), 42_500_000, "EUR", None)
    costs.set_manual("hetzner", "2026-10", 0, "EUR", None, None)
    assert rows("hetzner") == []
    with pytest.raises(ValueError):
        costs.set_manual("aws", "2026-10", 1, "USD", None, None)
    with pytest.raises(ValueError):
        costs.set_manual("hetzner", "2026/10", 1, "USD", None, None)


def test_summary_months_totals_and_projection(fresh_db, monkeypatch):
    monkeypatch.setenv("COST_EUR_USD", "1.2")
    d = dt.date
    costs.store_api_rows("openai", d(2026, 10, 1), d(2026, 11, 23), [
        {"day": d(2026, 10, 5), "scope": "talent-sourcing", "usd": 60.0},
        {"day": d(2026, 11, 3), "scope": "talent-sourcing", "usd": 22.0},
    ])
    costs.set_manual("hetzner", "2026-10", 50, "EUR", None, None)   # 60 $ at 1.2
    costs.set_manual("brightdata", "2026-10", 120, "USD", None, None)
    costs.set_manual("brightdata", "2026-11", 100, "USD", None, None)

    out = costs.summary(today=d(2026, 11, 22))
    by = {p["provider"]: p for p in out["providers"]}
    assert out["this_month"] == "2026-11-01" and out["months"][-1] == "2026-11-01" and len(out["months"]) == 6
    assert by["openai"]["months"]["2026-10-01"] == 60.0
    assert by["openai"]["projected_usd"] == pytest.approx(round(22 / 22 * 30, 2))
    assert by["hetzner"]["months"]["2026-10-01"] == pytest.approx(60.0)
    assert by["hetzner"]["projection_basis"] == "last_month" and by["hetzner"]["projected_usd"] == pytest.approx(60.0)
    assert by["brightdata"]["projection_basis"] == "manual" and by["brightdata"]["projected_usd"] == 100.0
    assert out["totals"]["2026-10-01"] == pytest.approx(240.0)
    assert by["openai"]["scopes_this_month"] == [{"scope": "talent-sourcing", "usd": 22.0}]
    assert "other" not in by
    assert by["anthropic"]["configured"] is False
    assert by["anthropic"]["kind"] == "manual" and by["anthropic"]["topup"] is True

    # the chart's year: older than the table, same numbers where they overlap
    costs.set_manual("hetzner", "2026-02", 10, "EUR", None, None)
    out = costs.summary(today=d(2026, 11, 22))
    by = {p["provider"]: p for p in out["providers"]}
    assert len(out["history_months"]) == 12 and out["history_months"][0] == "2025-12-01"
    assert by["hetzner"]["history"]["2026-02-01"] == pytest.approx(12.0)
    assert "2026-02-01" not in by["hetzner"]["months"]
    assert by["openai"]["history"]["2026-10-01"] == by["openai"]["months"]["2026-10-01"]


def test_a_provider_without_admin_key_is_entered_by_hand_as_topups(fresh_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-x")
    assert "anthropic" in costs.manual_providers() and "openai" not in costs.manual_providers()
    costs.set_manual("anthropic", "2026-10", 48.4, "USD", "2x credit grant", None)
    with pytest.raises(ValueError):
        costs.set_manual("openai", "2026-10", 1, "USD", None, None)
    out = costs.summary(today=dt.date(2026, 11, 22))
    a = {p["provider"]: p for p in out["providers"]}["anthropic"]
    assert a["months"]["2026-10-01"] == pytest.approx(48.4)
    # a top-up is not a monthly bill: nothing carried into November
    assert a["projected_usd"] == 0.0 and a["projection_basis"] == "none"
    assert {"id": "anthropic", "label": "Anthropic"} in out["manual_providers"]


# --- routes ----------------------------------------------------------------------------------


@pytest.fixture()
def http(fresh_db, monkeypatch):
    monkeypatch.setenv("TALENT_HANDOFF_SECRET", SECRET)
    monkeypatch.setenv("ALISTER_BASE_URL", "https://alister.example.test")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    return TestClient(chat.app)


def test_manual_entry_needs_admin_and_json(http):
    payload = {"provider": "brightdata", "month": "2026-10", "amount": 120, "currency": "usd"}
    sign_in(http, "miton")
    assert http.post("/admin/usage/manual", json=payload).status_code == 403
    sign_in(http, "admin")
    assert http.post("/admin/usage/manual", data=payload).status_code == 415
    assert http.post("/admin/usage/manual", json=dict(payload, provider="aws")).status_code == 400
    assert http.post("/admin/usage/manual", json=payload).status_code == 200
    (r,) = rows("brightdata")
    assert r.updated_by == "marketa.parizek@miton.cz" and r.currency == "USD"
    data = http.get("/admin/usage.json").json()
    assert data["costs"]["manual"][0]["usd"] == 120.0 and "talent" in data


def test_sync_button_reports_missing_keys(http):
    sign_in(http, "admin")
    r = http.post("/admin/usage/sync", json={})
    assert r.status_code == 200
    assert r.json()["result"]["openai"] == {"ok": False, "error": "chybí klíč"}


def test_an_estimate_drives_the_projection_but_is_not_spend(fresh_db):
    d = dt.date
    costs.set_manual("brightdata", "2026-10", 220, "USD", None, None)
    costs.set_manual("brightdata", "2026-11", 100, "USD", "jen tech", None, estimate=True)
    out = costs.summary(today=d(2026, 11, 22))
    bd = {p["provider"]: p for p in out["providers"]}["brightdata"]
    assert bd["months"]["2026-11-01"] == 0 and out["totals"]["2026-11-01"] == 0
    assert (bd["projection_basis"], bd["projected_usd"], bd["estimate_this_month"]) == ("estimate", 100.0, 100.0)
    assert [m["estimate"] for m in out["manual"] if m["month"] == "2026-11"] == [True]
    # the invoice wins once it is in
    costs.set_manual("brightdata", "2026-11", 104.2, "USD", None, None)
    bd = {p["provider"]: p for p in costs.summary(today=d(2026, 11, 22))["providers"]}["brightdata"]
    assert (bd["projection_basis"], bd["projected_usd"]) == ("manual", 104.2)


def test_estimate_flag_goes_through_the_route(http):
    sign_in(http, "admin")
    r = http.post("/admin/usage/manual", json={"provider": "brightdata", "month": "2026-11", "amount": 100,
                                              "currency": "USD", "estimate": True})
    assert r.status_code == 200
    (row,) = rows("brightdata")
    assert row.source == "estimate"
