"""The portfolio itself, and the 14 Sep 2026 baseline.

``seed_companies.json`` is the last hand-built snapshot of the claude.ai
artifact this feature replaces: 44 companies with their careers page, category,
description and caveat, plus the 117 roles that were open that day, already
classified by function and country.

Seeding does two things:

  1. upserts the companies (re-runnable: it never deletes and never overwrites a
     value that is now set in the database with a NULL from the file);
  2. if the database has no run yet, writes the baseline as run #1, dated
     14 Sep 2026, actor "seed". That way the first real scrape has something to
     diff against and "new this week" is right from the first Monday.

Adding a company later is a one-line edit of the JSON plus `--seed`, or an
``upsert_company`` call from the admin.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from talent.models import PortfolioCompany, PortfolioRole, PortfolioRun
from talent.portfolio import adapters, store
from talent.portfolio.collapse import collapse

log = logging.getLogger("miton-talent.portfolio.seed")

SEED_FILE = os.path.join(os.path.dirname(__file__), "seed_companies.json")


def load(path: str | None = None) -> dict:
    with open(path or SEED_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def seed(session: Session, *, path: str | None = None, with_baseline: bool = True) -> dict:
    data = load(path)
    created = updated = 0
    for entry in data["companies"]:
        existing = store.company_by_slug(session, entry["slug"])
        store.upsert_company(
            session,
            entry["slug"],
            name=entry["name"],
            stage=entry.get("stage"),
            not_on_site=entry.get("not_on_site", False),
            group_name=entry.get("group_name"),
            website=entry.get("website"),
            careers_url=entry.get("careers_url"),
            adapter=entry.get("adapter") or (adapters.detect(entry["careers_url"]) if entry.get("careers_url") else None),
            cats=entry.get("cats") or [],
            description=entry.get("desc"),
            note=entry.get("note"),
            config=entry.get("config") or {},
        )
        if existing is None:
            created += 1
        else:
            updated += 1

    out = {"companies_created": created, "companies_updated": updated, "baseline": None}
    if with_baseline:
        out["baseline"] = _baseline(session, data)
    return out


def _baseline(session: Session, data: dict) -> dict | None:
    """Write the artifact's snapshot as the first run, once."""
    if session.scalar(select(func.count(PortfolioRun.id))):
        return None

    day = dt.datetime.fromisoformat(data["baseline_date"]).replace(tzinfo=dt.timezone.utc)
    run = PortfolioRun(
        started_at=day, finished_at=day, status="done", actor="seed",
        companies_total=len(data["companies"]),
    )
    session.add(run)
    session.flush()

    roles = 0
    for entry in data["companies"]:
        company = store.company_by_slug(session, entry["slug"])
        rows = collapse([
            {
                "title": r["title"], "location": r.get("location"), "team": r.get("team"),
                "employment_type": r.get("employment_type"), "url": r.get("url"),
                "fn": r.get("fn"), "cc": r.get("cc"),
            }
            for r in entry.get("roles", [])
        ], (entry.get("config") or {}).get("aliases"))
        for row in rows:
            row["fn_source"] = "seed"
            row["is_technical"] = row.get("fn") in {"Engineering & AI", "Data & Analytics", "Product & Design"}
        status = entry.get("status") or "ok"
        outcome = store.record_company_result(
            session, run, company,
            status=status if status in {"ok", "blocked", "no_careers_page"} else "ok",
            rows=rows,
            adapter=company.adapter,
            source_url=company.careers_url,
            note=None,
            classify_rows=False,
        )
        roles += outcome["roles"]
    # first_seen on the baseline is "was already open then", not "new", so the
    # weekly diff must not report 117 new roles on the first page load.
    session.execute(
        PortfolioRole.__table__.update()
        .where(PortfolioRole.first_seen_run_id == run.id)
        .values(last_seen_run_id=run.id)
    )
    store.finish_run(session, run, status="done")
    run.roles_new = 0
    session.flush()
    log.info("portfolio baseline written: run %s, %d roles from %s", run.id, roles, data["baseline_date"])
    return {"run_id": run.id, "date": data["baseline_date"], "roles": roles}
