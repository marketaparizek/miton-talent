"""Company tier for intake scoring: a curated catalog first, the model's reading second.

Same method as Alister's LinkedIn quality score (github-sourcing src/identity/linkedin_quality.py):
the tier of a known company comes from a list, not from the model, so the same company always gets
the same tier; the best company carries most of the weight (top-3 at 0.80/0.15/0.05); old positions
fade after a grace period. Two Miton differences: an unknown company is T4, not a neutral T3, and a
corporate without startup DNA (automotive, telco, bank, FMCG) is capped at T3 even when the model
rates it higher.

The tier written to Notion is the best company in the history (the scorecard rule). The fading only
lowers the company part of the numeric Score.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

CATALOG_PATH = Path(__file__).with_name("company_tiers.csv")

TIERS = ["T1", "T2", "T3", "T4"]
TIER_RANK = {t: i for i, t in enumerate(TIERS)}  # lower = better
CORPORATE_CAP = "T3"
UNKNOWN_TIER = "T4"

# Decay of old positions, same constants as Alister (LINKEDIN_AGE_DECAY_*):
# factor = max(floor, 1 - rate * max(0, years_since_end - grace)).
DECAY_GRACE_YEARS = 2.0
DECAY_RATE = 0.06
DECAY_FLOOR = 0.15
# Top-1/2/3 weights over the decayed company values (Alister LINKEDIN_HISTORY_TOP*_WEIGHT).
TOP_WEIGHTS = (0.80, 0.15, 0.05)

_LEGAL = {
    "a", "s", "as", "sro", "spol", "r", "o", "ro", "inc", "ltd", "limited", "llc", "plc", "gmbh", "ag",
    "sa", "se", "nv", "bv", "ab", "oy", "corp", "corporation", "co", "company", "group", "holding",
    "holdings", "the", "czech", "republic", "cr", "cz", "ceska", "republika", "czechia", "international",
    "services", "global", "europe", "emea",
}
_KEEP_SHORT = {"ab"}  # "AB InBev": keep "ab" when it leads the name


def normalize(name: str) -> str:
    """Lower-case, strip diacritics, brackets, web suffixes and legal/country words."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\.(cz|com|io|ai|gg|sk|eu)\b", " ", s)
    s = s.replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", s)
    out = [w for i, w in enumerate(words) if w not in _LEGAL or (i == 0 and w in _KEEP_SHORT)]
    return " ".join(out) or " ".join(words)


@lru_cache(maxsize=1)
def catalog() -> dict:
    """normalized name -> {"tier", "reason", "name"}. First listing wins on a clash."""
    text = CATALOG_PATH.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    out: dict = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines))):
        tier = (row.get("tier") or "").strip()
        if tier not in TIERS:
            continue
        names = [row.get("name") or ""] + (row.get("aliases") or "").split("|")
        for n in names:
            key = normalize(n)
            if key and key not in out:
                out[key] = {"tier": tier, "reason": (row.get("reason") or "").strip(), "name": row["name"].strip()}
    return out


def lookup(name: str) -> Optional[dict]:
    return catalog().get(normalize(name))


def decay(end_year, now_year: int) -> float:
    """Weight multiplier for a position that ended in end_year (None = current)."""
    try:
        age = max(0.0, float(now_year) - float(end_year)) if end_year not in (None, "") else 0.0
    except (TypeError, ValueError):
        age = 0.0
    return max(DECAY_FLOOR, 1.0 - DECAY_RATE * max(0.0, age - DECAY_GRACE_YEARS))


def _top_weighted(values: list) -> float:
    ranked = sorted(values, reverse=True)[:3]
    if not ranked:
        return 0.0
    w = TOP_WEIGHTS[: len(ranked)]
    return sum(v * x for v, x in zip(ranked, w)) / sum(w)


def rate_one(company: dict) -> dict:
    """Tier for one company entry from the model: catalog wins, else the model's tier, capped for corporates."""
    name = str(company.get("name") or "").strip()
    hit = lookup(name)
    if hit:
        return {"name": name, "tier": hit["tier"], "source": "katalog", "reason": hit["reason"]}
    tier = company.get("tier") if company.get("tier") in TIERS else UNKNOWN_TIER
    source = "odhad"
    if company.get("corporate") and TIER_RANK[tier] < TIER_RANK[CORPORATE_CAP]:
        tier, source = CORPORATE_CAP, "odhad, korporát"
    return {"name": name, "tier": tier, "source": source, "reason": ""}


def rate_history(companies: Iterable[dict], tier_points: dict, now_year: int) -> Optional[dict]:
    """Rate a whole company history.

    Returns {"tier": best tier, "points": 0..1 company value for the Score, "rows": per-company detail},
    or None when the history is empty (the caller then falls back to the model's overall tier).
    """
    rows = []
    for c in companies or []:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        r = rate_one(c)
        r["end_year"] = c.get("end_year")
        r["decay"] = round(decay(c.get("end_year"), now_year), 2)
        r["value"] = tier_points.get(r["tier"], 0.0) * r["decay"]
        rows.append(r)
    if not rows:
        return None
    best = min(rows, key=lambda r: TIER_RANK[r["tier"]])["tier"]
    return {"tier": best, "points": _top_weighted([r["value"] for r in rows]), "rows": rows}


def describe(rows: list) -> str:
    """One line for the recruiter: 'Rohlik T1 (katalog) · Acme T4 (odhad)'."""
    parts = []
    for r in rows:
        when = "" if r.get("end_year") in (None, "") else f", do {r['end_year']}"
        parts.append(f"{r['name']} {r['tier']} ({r['source']}{when})")
    return " · ".join(parts)
