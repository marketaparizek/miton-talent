"""Company tier catalog + the intake score built on it (no model calls)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app as chat  # noqa: E402
from talent import company_tiers as ct  # noqa: E402


@pytest.mark.parametrize("name,tier", [
    ("Škoda Auto", "T3"), ("ŠKODA AUTO a.s.", "T3"), ("Skoda Auto DigiLab s.r.o.", "T3"),
    ("O2 Czech Republic", "T3"), ("O2 Czech Republic, a.s.", "T3"), ("Telefónica O2 Czech Republic", "T3"),
    ("AB InBev", "T3"), ("Anheuser-Busch InBev", "T3"),
    ("Komerční banka", "T3"), ("Komercni banka, a.s.", "T3"),
    ("Rohlik", "T1"), ("Rohlik Group", "T1"), ("Rohlik.cz", "T1"),
    ("Productboard", "T1"), ("productboard s.r.o.", "T1"),
    ("Google", "T1"), ("Google Inc.", "T1"),
    ("Stripe", "T1"), ("Revolut", "T1"), ("Rossum", "T1"), ("Mews", "T1"),
    ("Seznam.cz, a.s.", "T2"), ("Česká spořitelna, a.s.", "T3"),
])
def test_catalog_tiers(name, tier):
    assert ct.lookup(name)["tier"] == tier


def test_unknown_company_is_t4_unless_model_knows_better():
    assert ct.rate_one({"name": "Acme Widgets s.r.o.", "tier": "T4", "corporate": False})["tier"] == "T4"
    assert ct.rate_one({"name": "Acme Widgets s.r.o."})["tier"] == "T4"


def test_corporate_cap_beats_model_tier():
    r = ct.rate_one({"name": "Some Big Brewery", "tier": "T1", "corporate": True})
    assert r["tier"] == "T3" and "korporát" in r["source"]


def test_catalog_beats_model_tier():
    assert ct.rate_one({"name": "Škoda Auto", "tier": "T1", "corporate": False})["tier"] == "T3"
    assert ct.rate_one({"name": "Rohlik", "tier": "T4", "corporate": False})["tier"] == "T1"


def test_decay():
    assert ct.decay(None, 2026) == 1.0
    assert ct.decay(2025, 2026) == 1.0          # inside the 2-year grace
    assert ct.decay(2016, 2026) == pytest.approx(1 - 0.06 * 8)
    assert ct.decay(1980, 2026) == ct.DECAY_FLOOR


def test_best_company_sets_tier_decay_lowers_points():
    hist = [{"name": "O2", "end_year": None, "tier": "T1", "corporate": True},
            {"name": "Google", "end_year": 2014, "tier": "T1", "corporate": False}]
    r = ct.rate_history(hist, chat.COMPANY_TIER_POINTS, 2026)
    assert r["tier"] == "T1"                    # best company in the history
    assert r["points"] < chat.COMPANY_TIER_POINTS["T1"]  # but Google was 12 years ago


def test_corporate_only_cv_scores_t3_at_most():
    verdict = {"company_tier": "T1", "education": "T1", "ownership": 2, "impact": 2, "depth": 2,
               "companies": [{"name": "ŠKODA AUTO a.s.", "end_year": None, "tier": "T1", "corporate": True},
                             {"name": "AB InBev", "end_year": 2022, "tier": "T1", "corporate": True}]}
    v = chat._resolve_company_tier(verdict, now_year=2026)
    assert v["company_tier"] == "T3"
    props = chat._score_props(v)
    assert props["Company tier"]["select"]["name"] == "T3"
    # Old rule gave T1 -> 45 + 20 + 23 = 88 "Potential fit"; now 16 + 20 + 23 = 59.
    assert chat._compute_score(v) == 59
    assert props["Doporučení"]["select"]["name"] == "K rozhodnutí"


def test_no_history_keeps_model_tier():
    v = chat._resolve_company_tier({"company_tier": "T2", "companies": []}, now_year=2026)
    assert v["company_tier"] == "T2" and "_company_points" not in v
