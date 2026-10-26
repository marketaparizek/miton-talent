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
from sqlalchemy.orm import Session, aliased

from talent.models import (
    PortfolioCompany,
    PortfolioCompanyRun,
    PortfolioRole,
    PortfolioRun,
    utcnow,
)
from talent.portfolio import classify, identity
from talent.portfolio.collapse import collapse, group_requisitions

log = logging.getLogger("miton-talent.portfolio.store")

# A run read this company's page and may close its roles.
READ_STATUSES = {"ok"}
# The count can be trusted: either the page was read, or there is no page to
# read and the zero is a known zero. Everything else is last week's number.
VERIFIED_STATUSES = {"ok", "no_careers_page"}


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


def _opened_in(run_id: int):
    """Roles that opened in this run as news: a relisted posting is not."""
    return (PortfolioRole.first_seen_run_id == run_id, PortfolioRole.replaces_role_id.is_(None))


def _closed_in(run_id: int):
    """Roles that closed in this run as news: one a new posting replaced in the
    same run is not."""
    replacer = aliased(PortfolioRole)
    replaced_now = select(replacer.replaces_role_id).where(
        replacer.first_seen_run_id == run_id, replacer.replaces_role_id.is_not(None)
    )
    return (PortfolioRole.closed_run_id == run_id, PortfolioRole.id.not_in(replaced_now),
            PortfolioRole.merged_into_role_id.is_(None))


def _relisted_in(run_id: int):
    return (PortfolioRole.first_seen_run_id == run_id, PortfolioRole.replaces_role_id.is_not(None))


def run_totals(session: Session, run_id: int) -> dict:
    roles_total = session.scalar(
        select(func.count(PortfolioRole.id)).where(PortfolioRole.closed_at.is_(None))
    ) or 0
    roles_new = session.scalar(select(func.count(PortfolioRole.id)).where(*_opened_in(run_id))) or 0
    roles_closed = session.scalar(select(func.count(PortfolioRole.id)).where(*_closed_in(run_id))) or 0
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
        if (company.config or {}).get("requisitions"):
            rows = group_requisitions(rows, company.config["requisitions"])
        if classify_rows:
            classify.classify_rows(rows, company=company.name)
        new_count, closed_count = _apply_rows(session, run, company, rows)
        roles_found = len(rows)
    else:
        # Page not read: nothing is created, nothing is closed, nothing is touched.
        new_count = closed_count = 0
        roles_found = len(open_roles(session, company.id))

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
    """Match this run's rows to the company's open roles, then open and close.

    Matching goes by ``role_key`` (identity.py), so a posting that keeps its id
    and changes its title stays one role and only gains a title_history entry.
    A role written before identity existed has no key yet and is adopted by its
    fingerprint, once, which is what keeps the first run after a deploy quiet.

    What is left unmatched on both sides is checked for relisting before it is
    scored as an opening and a closure: see ``_pair_relisted``.
    """
    identity.assign_keys(rows, careers_url=company.careers_url)
    existing = open_roles(session, company.id)
    by_key = {r.role_key: r for r in existing if r.role_key}
    matched: dict[int, dict] = {}       # role id -> row
    merged: dict[int, int] = {}         # role id -> the role id it folded into
    unmatched_rows: list[dict] = []
    seen_keys: set[str] = set()

    for row in rows:
        if row["role_key"] in seen_keys:
            continue
        seen_keys.add(row["role_key"])
        role = by_key.get(row["role_key"])
        if role is not None and role.id not in matched:
            matched[role.id] = row
        else:
            unmatched_rows.append(row)

    # By posting id: a requisition whose title or grouping changed, or a role
    # stored before identity existed, is still the posting it was.
    ids_by_role = {r.id: identity.ids_of(r) for r in existing}
    rest = []
    for row in unmatched_rows:
        ids = set(row.get("external_ids") or [])
        hit = next((r for r in existing if r.id not in matched and ids & ids_by_role[r.id]), None)
        if hit is not None:
            matched[hit.id] = row
        else:
            rest.append(row)
    unmatched_rows = rest

    # Several stored roles can be postings of what is now one requisition (the
    # first run that groups Rohlik's board). They fold into the matched role;
    # that is a change in counting, not a closure.
    row_owner = {id(row): rid for rid, row in matched.items()}
    for role in existing:
        if role.id in matched:
            continue
        for row in rows:
            owner = row_owner.get(id(row))
            if owner is not None and ids_by_role[role.id] & set(row.get("external_ids") or []):
                merged[role.id] = owner
                break

    # Adoption by fingerprint: only for a role whose identity is weaker than a
    # board id (no key yet, a URL, or the title). Two different board ids under
    # one title are two postings; whether they are one job is _pair_relisted's
    # question, and it never merges them silently.
    leftovers = [r for r in existing if r.id not in matched and r.id not in merged
                 and r.key_tier not in ("ats", "req")]
    by_fp = {}
    for role in leftovers:
        by_fp.setdefault(role.fingerprint, role)
    still_unmatched = []
    for row in unmatched_rows:
        role = by_fp.pop(row["fingerprint"], None)
        if role is not None and role.id not in matched:
            matched[role.id] = row
        else:
            still_unmatched.append(row)

    now = utcnow()
    roles_by_id = {r.id: r for r in existing}
    for role_id, row in matched.items():
        _write_role(roles_by_id[role_id], row, run, now)

    to_close = [r for r in existing if r.id not in matched]
    for role in to_close:
        if role.id in merged:
            role.merged_into_role_id = merged[role.id]
    new_roles = []
    for row in still_unmatched:
        role = PortfolioRole(
            company_id=company.id,
            fingerprint=row["fingerprint"],
            first_seen_at=now,
            first_seen_run_id=run.id,
            title_history=[],
        )
        session.add(role)
        _write_role(role, row, run, now)
        new_roles.append(role)
    for role in to_close:
        role.closed_at = now
        role.closed_run_id = run.id
    session.flush()

    _pair_relisted(session, company, new_roles, [r for r in to_close if r.id not in merged])
    session.flush()
    replaced = {r.replaces_role_id for r in new_roles if r.replaces_role_id}
    return (sum(1 for r in new_roles if not r.replaces_role_id),
            sum(1 for r in to_close if r.id not in replaced and r.id not in merged))


def _write_role(role: PortfolioRole, row: dict, run: PortfolioRun, now: dt.datetime) -> None:
    title = row.get("title") or role.title
    if role.title and title and role.title != title:
        role.title_history = list(role.title_history or []) + [
            {"title": role.title, "until": now.date().isoformat()}
        ]
    role.title = title
    role.role_key = row["role_key"]
    role.key_tier = row["key_tier"]
    role.external_ids = list(row.get("external_ids") or [])
    if row.get("posted_at"):
        posted = dt.date.fromisoformat(row["posted_at"])
        # A requisition is as old as its oldest posting, so this only moves back.
        role.posted_at = min(role.posted_at, posted) if role.posted_at else posted
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
    role.raw = {k: v for k, v in row.items()
                if k not in {"fingerprint", "locations", "role_key", "key_tier", "aliased", "posted_at",
                             "external_ids", "requisition"}}


# A posting that closes and comes back inside this window is the same job
# advertised again, not a new opening.
REPOST_WINDOW = dt.timedelta(days=28)


def _location_key(role: PortfolioRole) -> str:
    return " / ".join(sorted(l.strip().lower() for l in (role.locations or []) if l)) \
        or (role.location or "").strip().lower()


def _pair_relisted(session: Session, company: PortfolioCompany,
                   new_roles: list[PortfolioRole], closed: list[PortfolioRole]) -> None:
    """Mark postings that replace another one instead of scoring open + close.

    Three shapes, all seen at Rohlik or Knihobot:

    * ``same_title``  a posting closed and one with the same title opened in
                      the same run (new id, same job).
    * ``same_slot``   the title changed too, but exactly one posting closed and
                      exactly one opened at the same place in the same function
                      ("Area Manager Chrášťany" -> "Area Manager ve skladu
                      Chrášťany"). Only a one-to-one pair counts: two warehouse
                      roles closing and two opening at one site stay as they are.
    * ``repost``      a role with the same title (or the same board id) closed
                      at this company within REPOST_WINDOW.

    The new role keeps its own row and id; it gets ``replaces_role_id`` and the
    original first-seen date, and the diff lists the pair as relisted. Nothing is
    merged, so a wrong pairing is visible and reversible.
    """
    def link(new: PortfolioRole, old: PortfolioRole, reason: str) -> None:
        new.replaces_role_id = old.id
        new.relist_reason = reason
        new.first_seen_original_at = old.first_seen_original_at or old.first_seen_at

    open_new = list(new_roles)
    open_closed = list(closed)

    def base_fp(fp: str) -> str:
        return (fp or "").split("#", 1)[0]

    # 1. same title, same run
    for new in list(open_new):
        hits = [c for c in open_closed if base_fp(c.fingerprint) == base_fp(new.fingerprint)]
        if len(hits) == 1:
            link(new, hits[0], "same_title")
            open_new.remove(new)
            open_closed.remove(hits[0])

    # 2. one-to-one at the same place and function, same run
    def slot(role: PortfolioRole) -> tuple:
        return (_location_key(role), role.fn or "")
    new_slots: dict[tuple, list] = {}
    closed_slots: dict[tuple, list] = {}
    for r in open_new:
        new_slots.setdefault(slot(r), []).append(r)
    for r in open_closed:
        closed_slots.setdefault(slot(r), []).append(r)
    for key, news in new_slots.items():
        olds = closed_slots.get(key, [])
        if key[0] and len(news) == 1 and len(olds) == 1:
            link(news[0], olds[0], "same_slot")
            open_new.remove(news[0])

    # 3. back within the window
    if not open_new:
        return
    since = utcnow() - REPOST_WINDOW
    recent = list(session.scalars(
        select(PortfolioRole).where(
            PortfolioRole.company_id == company.id,
            PortfolioRole.closed_at.is_not(None),
            PortfolioRole.closed_at >= since,
        ).order_by(PortfolioRole.closed_at.desc())
    ))
    closed_now = {r.id for r in closed}
    for new in open_new:
        for old in recent:
            if old.id in closed_now:
                continue
            same_id = bool(set(new.external_ids or []) & identity.ids_of(old))
            if same_id or base_fp(old.fingerprint) == base_fp(new.fingerprint):
                link(new, old, "repost")
                break


# --- reads for the page ------------------------------------------------------


def _company_run_map(session: Session, run_id: int | None) -> dict[int, PortfolioCompanyRun]:
    if run_id is None:
        return {}
    rows = session.scalars(
        select(PortfolioCompanyRun).where(PortfolioCompanyRun.run_id == run_id)
    )
    return {r.company_id: r for r in rows}


def open_since(role: PortfolioRole, *, baseline_run_id: int | None = None) -> tuple[dt.date | None, str]:
    """When this role opened, and how we know.

    ``board``    the employer's own board dated the posting (posted_at)
    ``seen``     the first run that saw it (or the job it relisted)
    ``before``   it was already open in the 14 Sep 2026 baseline, so the date
                 is a floor: "open since at least"
    The board date wins whenever it is older than what we saw, because it also
    covers the time before this scraper existed.
    """
    seen = role.first_seen_original_at or role.first_seen_at
    seen_day = seen.date() if seen else None
    if role.posted_at and (seen_day is None or role.posted_at <= seen_day):
        return role.posted_at, "board"
    if baseline_run_id is not None and role.first_seen_run_id == baseline_run_id \
            and not role.first_seen_original_at:
        return seen_day, "before"
    return seen_day, "seen"


def _days(since: dt.date | None, until: dt.date | None = None) -> int | None:
    if since is None:
        return None
    return ((until or utcnow().date()) - since).days


def role_dict(role: PortfolioRole, *, baseline_run_id: int | None = None) -> dict:
    since, how = open_since(role, baseline_run_id=baseline_run_id)
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
        # A relisted posting is as old as the job it replaced.
        "first_seen": (role.first_seen_original_at or role.first_seen_at).date().isoformat()
        if role.first_seen_at else None,
        "new": role.first_seen_run_id is not None and role.last_seen_run_id == role.first_seen_run_id
        and not role.replaces_role_id,
        # How long the role has been open. "before" means at least this long.
        "open_since": since.isoformat() if since else None,
        "open_since_source": how,
        "age_days": _days(since),
    }


def snapshot(session: Session) -> dict:
    """Everything the dashboard needs, in one dict, shaped like the artifact's DATA."""
    run = last_run(session)
    cruns = _company_run_map(session, run.id if run else None)
    baseline_id = _baseline_run_id(session)
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
            # "the number is this week's": a company with no careers page has
            # nothing to read and is not a failed fetch.
            "verified": bool(crun and crun.status in VERIFIED_STATUSES),
            "checked_at": crun.run.started_at.isoformat() if crun and crun.run else None,
            "roles": [role_dict(r, baseline_run_id=baseline_id) for r in roles],
        })

    return {
        "scraped_at": run.started_at.date().isoformat() if run else None,
        "run_id": run.id if run else None,
        "run_status": run.status if run else None,
        "source": "https://www.miton.cz/portfolio/",
        "companies": out_companies,
        "changes": changes(session, run) if run else None,
        "health": health(session, run),
        "history": history(session),
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


def _baseline_run_id(session: Session) -> int | None:
    return session.scalar(select(func.min(PortfolioRun.id)).where(PortfolioRun.actor == "seed"))


def changes(session: Session, run: PortfolioRun) -> dict:
    """New and closed since the run before this one, plus who started and stopped."""
    prev = previous_run(session, run)
    new_roles = list(session.scalars(
        select(PortfolioRole).where(*_opened_in(run.id))
        .order_by(PortfolioRole.company_id, PortfolioRole.title)
    ))
    closed_roles = list(session.scalars(
        select(PortfolioRole).where(*_closed_in(run.id))
        .order_by(PortfolioRole.company_id, PortfolioRole.title)
    ))
    relisted_roles = list(session.scalars(
        select(PortfolioRole).where(*_relisted_in(run.id))
        .order_by(PortfolioRole.company_id, PortfolioRole.title)
    ))
    previous_titles = {
        r.id: r.title for r in session.scalars(
            select(PortfolioRole).where(PortfolioRole.id.in_(
                [r.replaces_role_id for r in relisted_roles] or [-1]
            ))
        )
    }
    names = {c.id: c.name for c in companies(session, active_only=False)}
    baseline_id = _baseline_run_id(session)

    def out(role: PortfolioRole) -> dict:
        since, how = open_since(role, baseline_run_id=baseline_id)
        until = role.closed_at.date() if role.closed_at else None
        return {
            # For a closed role: how long it was open (time to close).
            "open_days": _days(since, until),
            "open_since_source": how,
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
        relisted_roles = []

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
        # Same job under a new posting (new id, a retitle, or back within four
        # weeks). Not hiring news, so it is in neither list above.
        "relisted": [
            dict(out(r), previous_title=previous_titles.get(r.replaces_role_id), reason=r.relist_reason)
            for r in relisted_roles
        ],
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
            PortfolioRole.merged_into_role_id.is_(None),
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
    # How many times the scraper has actually read the portfolio. The imported
    # baseline is not one of them, and "new this week" only means anything once
    # two real runs can be compared: against the hand-built baseline, every row
    # the artifact never listed looks new.
    scrapes = session.scalar(
        select(func.count(PortfolioRun.id)).where(
            PortfolioRun.status == "done", PortfolioRun.actor != "seed"
        )
    ) or 0
    return {
        "ran": True,
        "scrapes": scrapes,
        "run_id": run.id,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "status": run.status,
        "actor": run.actor,
        "companies": len(cruns),
        "by_status": {k: sorted(v) for k, v in sorted(by_status.items())},
        "unverified": sorted(
            name for status, names_ in by_status.items()
            if status not in VERIFIED_STATUSES for name in names_
        ),
        "llm_read": sorted(
            names.get(c.company_id, "?") for c in cruns if c.adapter == "html" and c.status == "ok"
        ),
    }


def history(session: Session) -> list[dict]:
    """One row per finished run, oldest first: what the portfolio looked like
    at the end of it. The hub draws a delta and a sparkline from this, so a
    number can carry "against last week" instead of standing alone.

    Counts are reconstructed from the roles' first-seen and closed run ids
    rather than read off ``roles_total``, so the technical count and the
    number of companies hiring come out of the same arithmetic as the total.
    The imported baseline is a run like any other here: it is the first point.
    """
    runs = list(session.scalars(
        select(PortfolioRun).where(PortfolioRun.status == "done").order_by(PortfolioRun.id)
    ))
    out = []
    for run in runs:
        open_at = (
            PortfolioRole.first_seen_run_id <= run.id,
            (PortfolioRole.closed_run_id.is_(None)) | (PortfolioRole.closed_run_id > run.id),
            # A posting later folded into a requisition was a duplicate all
            # along; leaving it out keeps past weeks on today's way of counting,
            # so the switch to positions is not a drop in the trend.
            PortfolioRole.merged_into_role_id.is_(None),
        )
        total = session.scalar(select(func.count(PortfolioRole.id)).where(*open_at)) or 0
        technical = session.scalar(
            select(func.count(PortfolioRole.id)).where(*open_at, PortfolioRole.is_technical.is_(True))
        ) or 0
        hiring = session.scalar(
            select(func.count(func.distinct(PortfolioRole.company_id))).where(*open_at)
        ) or 0
        out.append({
            "run_id": run.id,
            "date": run.started_at.date().isoformat(),
            "baseline": (run.actor or "") == "seed",
            "open": total,
            "technical": technical,
            "companies_hiring": hiring,
            "new": run.roles_new,
            "closed": run.roles_closed,
            "companies_failed": run.companies_failed,
        })
    return out


def stale_days(session: Session) -> Optional[int]:
    run = last_run(session)
    if run is None:
        return None
    return (utcnow() - run.started_at.replace(tzinfo=run.started_at.tzinfo or dt.timezone.utc)).days
