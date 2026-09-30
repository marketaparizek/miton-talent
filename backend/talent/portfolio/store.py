"""Every write and read for the portfolio open roles.

The write path has one rule that everything else follows from: **only a run
that actually read a company's page may close that company's roles.** A 403, a
timeout or a parse break leaves last week's roles standing, so a broken fetch
can never be mistaken for a company that stopped hiring.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from talent.models import (
    PortfolioCompany,
    PortfolioCompanyRun,
    PortfolioRole,
    PortfolioRun,
    utcnow,
)
from talent.portfolio import classify
from talent.portfolio.collapse import apply_aliases, collapse, fingerprint

log = logging.getLogger("miton-talent.portfolio.store")

# A company whose page was read and showed roles counts as "hiring".
READ_STATUSES = {"ok"}


# --- companies ---------------------------------------------------------------


def companies(session: Session, *, active_only: bool = True) -> list[PortfolioCompany]:
    stmt = select(PortfolioCompany).order_by(PortfolioCompany.name)
    if active_only:
        stmt = stmt.where(PortfolioCompany.active.is_(True))
    return list(session.scalars(stmt))


def company_by_slug(session: Session, slug: str) -> Optional[PortfolioCompany]:
    return session.scalars(select(PortfolioCompany).where(PortfolioCompany.slug == slug)).first()


def upsert_company(session: Session, slug: str, **fields) -> PortfolioCompany:
    company = company_by_slug(session, slug)
    if company is None:
        company = PortfolioCompany(slug=slug, name=fields.get("name") or slug)
        session.add(company)
    for key, value in fields.items():
        if hasattr(company, key) and value is not None:
            setattr(company, key, value)
    session.flush()
    return company


# --- runs --------------------------------------------------------------------


def start_run(session: Session, *, actor: str = "cron", companies_total: int = 0) -> PortfolioRun:
    run = PortfolioRun(actor=actor, companies_total=companies_total, status="running")
    session.add(run)
    session.flush()
    return run


def finish_run(session: Session, run: PortfolioRun, *, status: str = "done", error: str | None = None) -> PortfolioRun:
    totals = run_totals(session, run.id)
    run.roles_total = totals["roles_total"]
    run.roles_new = totals["roles_new"]
    run.roles_closed = totals["roles_closed"]
    run.companies_failed = totals["companies_failed"]
    run.finished_at = utcnow()
    run.status = status
    run.error = error
    session.flush()
    return run


def run_totals(session: Session, run_id: int) -> dict:
    roles_total = session.scalar(
        select(func.count(PortfolioRole.id)).where(PortfolioRole.closed_at.is_(None))
    ) or 0
    roles_new = session.scalar(
        select(func.count(PortfolioRole.id)).where(PortfolioRole.first_seen_run_id == run_id)
    ) or 0
    roles_closed = session.scalar(
        select(func.count(PortfolioRole.id)).where(PortfolioRole.closed_run_id == run_id)
    ) or 0
    companies_failed = session.scalar(
        select(func.count(PortfolioCompanyRun.id)).where(
            PortfolioCompanyRun.run_id == run_id,
            PortfolioCompanyRun.status.in_(("failed", "blocked")),
        )
    ) or 0
    return {
        "roles_total": roles_total,
        "roles_new": roles_new,
        "roles_closed": roles_closed,
        "companies_failed": companies_failed,
    }


def last_run(session: Session, *, status: str | None = "done") -> Optional[PortfolioRun]:
    stmt = select(PortfolioRun).order_by(PortfolioRun.started_at.desc(), PortfolioRun.id.desc())
    if status:
        stmt = stmt.where(PortfolioRun.status == status)
    return session.scalars(stmt.limit(1)).first()


def previous_run(session: Session, run: PortfolioRun) -> Optional[PortfolioRun]:
    return session.scalars(
        select(PortfolioRun)
        .where(PortfolioRun.id < run.id, PortfolioRun.status == "done")
        .order_by(PortfolioRun.id.desc())
        .limit(1)
    ).first()


# --- roles -------------------------------------------------------------------


def open_roles(session: Session, company_id: int | None = None) -> list[PortfolioRole]:
    stmt = select(PortfolioRole).where(PortfolioRole.closed_at.is_(None))
    if company_id is not None:
        stmt = stmt.where(PortfolioRole.company_id == company_id)
    return list(session.scalars(stmt.order_by(PortfolioRole.title)))


def _open_by_fingerprint(session: Session, company_id: int) -> dict[str, PortfolioRole]:
    out: dict[str, PortfolioRole] = {}
    for role in open_roles(session, company_id):
        # Should not happen (the write path keeps one open row per fingerprint),
        # but if it ever did, the older row is closed below rather than ignored.
        out.setdefault(role.fingerprint, role)
    return out


def record_company_result(
    session: Session,
    run: PortfolioRun,
    company: PortfolioCompany,
    *,
    status: str,
    rows: Iterable[dict] | None = None,
    adapter: str | None = None,
    source_url: str | None = None,
    http_status: int | None = None,
    raw_rows: int = 0,
    note: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    classify_rows: bool = True,
) -> dict:
    """Write one company's outcome into this run, and diff its roles.

    Returns {"status", "roles": n, "new": n, "closed": n}.
    """
    rows = list(rows or [])
    aliases = (company.config or {}).get("aliases") or {}

    if status in READ_STATUSES:
        rows = collapse(rows, aliases)
        if classify_rows:
            classify.classify_rows(rows, company=company.name)
        new_count, closed_count = _apply_rows(session, run, company, rows)
        roles_found = len(rows)
    else:
        # Page not read: nothing is created, nothing is closed, nothing is touched.
        new_count = closed_count = 0
        roles_found = len(_open_by_fingerprint(session, company.id))

    session.add(PortfolioCompanyRun(
        run_id=run.id,
        company_id=company.id,
        status=status,
        adapter=adapter,
        source_url=source_url,
        http_status=http_status,
        roles_found=roles_found,
        raw_rows=raw_rows or len(rows),
        note=note,
        error=error,
        duration_ms=duration_ms,
    ))
    session.flush()
    return {"status": status, "roles": roles_found, "new": new_count, "closed": closed_count}


def _apply_rows(session: Session, run: PortfolioRun, company: PortfolioCompany,
                rows: list[dict]) -> tuple[int, int]:
    existing = _open_by_fingerprint(session, company.id)
    seen: set[str] = set()
    new_count = 0
    now = utcnow()

    for row in rows:
        fp = row.get("fingerprint") or apply_aliases(
            fingerprint(row.get("title", "")), (company.config or {}).get("aliases")
        )
        if fp in seen:          # two rows collapsed onto one fingerprint
            continue
        seen.add(fp)
        role = existing.get(fp)
        if role is None:
            role = PortfolioRole(
                company_id=company.id,
                fingerprint=fp,
                first_seen_at=now,
                first_seen_run_id=run.id,
            )
            session.add(role)
            new_count += 1
        role.title = row.get("title") or role.title
        role.location = row.get("location")
        role.locations = row.get("locations") or ([row["location"]] if row.get("location") else [])
        role.team = row.get("team")
        role.employment_type = row.get("employment_type")
        role.url = row.get("url")
        if row.get("fn"):
            role.fn = row["fn"]
            role.fn_source = row.get("fn_source") or "rules"
            role.is_technical = bool(row.get("is_technical"))
        role.cc = row.get("cc") or role.cc
        role.last_seen_at = now
        role.last_seen_run_id = run.id
        role.raw = {k: v for k, v in row.items() if k not in {"fingerprint", "locations"}}

    closed_count = 0
    for fp, role in existing.items():
        if fp not in seen:
            role.closed_at = now
            role.closed_run_id = run.id
            closed_count += 1
    session.flush()
    return new_count, closed_count


# --- reads for the page ------------------------------------------------------


def _company_run_map(session: Session, run_id: int | None) -> dict[int, PortfolioCompanyRun]:
    if run_id is None:
        return {}
    rows = session.scalars(
        select(PortfolioCompanyRun).where(PortfolioCompanyRun.run_id == run_id)
    )
    return {r.company_id: r for r in rows}


def role_dict(role: PortfolioRole) -> dict:
    return {
        "title": role.title,
        "location": role.location or "—",
        "locations": role.locations or [],
        "team": role.team,
        "type": role.employment_type,
        "url": role.url,
        "fn": role.fn or "Other",
        "fn_source": role.fn_source,
        "cc": role.cc or "Other",
        "tech": bool(role.is_technical),
        "first_seen": role.first_seen_at.date().isoformat() if role.first_seen_at else None,
        "new": role.first_seen_run_id is not None and role.last_seen_run_id == role.first_seen_run_id,
    }


def snapshot(session: Session) -> dict:
    """Everything the dashboard needs, in one dict, shaped like the artifact's DATA."""
    run = last_run(session)
    cruns = _company_run_map(session, run.id if run else None)
    roles_by_company: dict[int, list[PortfolioRole]] = {}
    for role in open_roles(session):
        roles_by_company.setdefault(role.company_id, []).append(role)

    out_companies = []
    for company in companies(session):
        crun = cruns.get(company.id)
        roles = sorted(roles_by_company.get(company.id, []), key=lambda r: (r.fn or "", r.title))
        out_companies.append({
            "slug": company.slug,
            "name": company.name,
            "stage": company.stage or "Not on miton.cz",
            "not_on_site": company.not_on_site,
            "group_name": company.group_name,
            "website": company.website,
            "careers_url": company.careers_url,
            "cats": company.cats or [],
            "desc": company.description,
            "note": _company_note(company, crun),
            "status": crun.status if crun else ("no_careers_page" if not company.careers_url else "unknown"),
            "adapter": crun.adapter if crun else company.adapter,
            "read_ok": bool(crun and crun.status in READ_STATUSES),
            "checked_at": crun.run.started_at.isoformat() if crun and crun.run else None,
            "roles": [role_dict(r) for r in roles],
        })

    return {
        "scraped_at": run.started_at.date().isoformat() if run else None,
        "run_id": run.id if run else None,
        "run_status": run.status if run else None,
        "source": "https://www.miton.cz/portfolio/",
        "companies": out_companies,
        "changes": changes(session, run) if run else None,
        "health": health(session, run),
    }


def _company_note(company: PortfolioCompany, crun: PortfolioCompanyRun | None) -> str | None:
    """What a reader needs before trusting this company's number: the standing
    caveat, and whatever went wrong in the last run."""
    parts = []
    if crun and crun.status not in READ_STATUSES:
        label = {
            "failed": "Last check failed",
            "blocked": "Last check was refused by the site",
            "no_careers_page": "No careers page known",
            "skipped": "Not scraped by configuration",
        }.get(crun.status, crun.status)
        detail = crun.error or crun.note
        parts.append(f"{label}: {detail}" if detail else label)
    elif crun and crun.note:
        parts.append(crun.note)
    if company.note:
        parts.append(company.note)
    return " · ".join(parts) or None


def changes(session: Session, run: PortfolioRun) -> dict:
    """New and closed since the run before this one, plus who started and stopped."""
    prev = previous_run(session, run)
    new_roles = list(session.scalars(
        select(PortfolioRole).where(PortfolioRole.first_seen_run_id == run.id)
        .order_by(PortfolioRole.company_id, PortfolioRole.title)
    ))
    closed_roles = list(session.scalars(
        select(PortfolioRole).where(PortfolioRole.closed_run_id == run.id)
        .order_by(PortfolioRole.company_id, PortfolioRole.title)
    ))
    names = {c.id: c.name for c in companies(session, active_only=False)}

    def out(role: PortfolioRole) -> dict:
        return {
            "title": role.title,
            "company": names.get(role.company_id, "?"),
            "location": role.location or "—",
            "fn": role.fn or "Other",
            "tech": bool(role.is_technical),
            "url": role.url,
        }

    started, quiet = [], []
    if prev:
        before = _counts_at_run(session, prev.id)
        after = {c["company"]: c["n"] for c in _counts_now(session)}
        for name in sorted(set(before) | set(after)):
            was, now = before.get(name, 0), after.get(name, 0)
            if was == 0 and now > 0:
                started.append(name)
            elif was > 0 and now == 0:
                quiet.append(name)

    baseline = (run.actor or "") == "seed"
    if baseline:
        # The imported snapshot is "what was already open", not a week's news.
        new_roles = []

    return {
        "baseline": baseline,
        "since_run_id": prev.id if prev else None,
        "since": prev.started_at.date().isoformat() if prev else None,
        "roles_before": prev.roles_total if prev else None,
        "roles_now": session.scalar(
            select(func.count(PortfolioRole.id)).where(PortfolioRole.closed_at.is_(None))
        ) or 0,
        "new": [out(r) for r in new_roles],
        "closed": [out(r) for r in closed_roles],
        "started_hiring": started,
        "went_quiet": quiet,
    }


def _counts_at_run(session: Session, run_id: int) -> dict[str, int]:
    """How many roles each company had open at the end of that run."""
    rows = session.execute(
        select(PortfolioCompany.name, func.count(PortfolioRole.id))
        .join(PortfolioRole, PortfolioRole.company_id == PortfolioCompany.id)
        .where(
            PortfolioRole.first_seen_run_id <= run_id,
            (PortfolioRole.closed_run_id.is_(None)) | (PortfolioRole.closed_run_id > run_id),
        )
        .group_by(PortfolioCompany.name)
    ).all()
    return {name: n for name, n in rows}


def _counts_now(session: Session) -> list[dict]:
    rows = session.execute(
        select(PortfolioCompany.name, func.count(PortfolioRole.id))
        .join(PortfolioRole, PortfolioRole.company_id == PortfolioCompany.id)
        .where(PortfolioRole.closed_at.is_(None))
        .group_by(PortfolioCompany.name)
    ).all()
    return [{"company": name, "n": n} for name, n in rows]


def health(session: Session, run: PortfolioRun | None) -> dict:
    """What the last run could and could not read. The page shows this next to
    the zeros, because a zero the scraper never verified is not a zero."""
    if run is None:
        return {"ran": False}
    cruns = list(session.scalars(select(PortfolioCompanyRun).where(PortfolioCompanyRun.run_id == run.id)))
    names = {c.id: c.name for c in companies(session, active_only=False)}
    by_status: dict[str, list[str]] = {}
    for crun in cruns:
        by_status.setdefault(crun.status, []).append(names.get(crun.company_id, "?"))
    return {
        "ran": True,
        "run_id": run.id,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "status": run.status,
        "actor": run.actor,
        "companies": len(cruns),
        "by_status": {k: sorted(v) for k, v in sorted(by_status.items())},
        "unverified": sorted(
            name for status, names_ in by_status.items() if status in {"failed", "blocked"} for name in names_
        ),
        "llm_read": sorted(
            names.get(c.company_id, "?") for c in cruns if c.adapter == "html" and c.status == "ok"
        ),
    }


def stale_days(session: Session) -> Optional[int]:
    run = last_run(session)
    if run is None:
        return None
    return (utcnow() - run.started_at.replace(tzinfo=run.started_at.tzinfo or dt.timezone.utc)).days
