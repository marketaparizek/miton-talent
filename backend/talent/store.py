"""Write and read paths over the Miton Talent tables.

Everything the chat backend, the admin, the import and the MCP tools do to the
database goes through here, so the rules (vocab validation, event logging,
merge keys) live in one place. Functions take an open Session and never commit;
the caller owns the transaction (see db.session()).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Iterable, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import vocab
from .models import Candidate, CandidateEvent, Search, SearchCandidate, utcnow

_LI_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)


def linkedin_identifier(url: Optional[str]) -> Optional[str]:
    """'https://www.linkedin.com/in/Jan-Novak-1a2b/' -> 'jan-novak-1a2b'."""
    if not url:
        return None
    m = _LI_RE.search(url.strip())
    if not m:
        return None
    return m.group(1).strip().rstrip("/").lower() or None


def aware(d: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """SQLite hands back naive datetimes, Postgres aware ones. Compare in UTC either way."""
    if d is None or d.tzinfo is not None:
        return d
    return d.replace(tzinfo=dt.timezone.utc)


def normalize_email(email: Optional[str]) -> Optional[str]:
    e = (email or "").strip().lower()
    return e or None


# --- candidates -------------------------------------------------------------

def find_candidate(session: Session, *, uid: str | None = None, linkedin_url: str | None = None,
                   email: str | None = None, legacy_ref: str | None = None) -> Optional[Candidate]:
    """Look a person up by the merge keys, strongest first. Ignores soft-deleted rows."""
    q = select(Candidate).where(Candidate.deleted_at.is_(None))
    if uid:
        row = session.scalar(q.where(Candidate.uid == uid))
        if row:
            return row
    if legacy_ref:
        row = session.scalar(q.where(Candidate.legacy_ref == legacy_ref))
        if row:
            return row
    ident = linkedin_identifier(linkedin_url)
    if ident:
        row = session.scalar(q.where(Candidate.linkedin_identifier == ident))
        if row:
            return row
    em = normalize_email(email)
    if em:
        row = session.scalar(q.where(func.lower(Candidate.email) == em).order_by(Candidate.id))
        if row:
            return row
    return None


def add_event(session: Session, candidate: Candidate, type_: str, *, actor: str | None = None,
              data: dict | None = None, search_id: int | None = None,
              happened_at: dt.datetime | None = None) -> CandidateEvent:
    if type_ not in vocab.EVENT_TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    ev = CandidateEvent(
        candidate=candidate, type=type_, actor=actor, data=data or {},
        search_id=search_id, happened_at=happened_at or utcnow(),
    )
    session.add(ev)
    return ev


def set_stage(session: Session, candidate: Candidate, stage: str, *, actor: str | None = None,
              note: str | None = None) -> Candidate:
    if stage not in vocab.STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if candidate.stage != stage:
        add_event(session, candidate, "stage_changed", actor=actor,
                  data={"from": candidate.stage, "to": stage, **({"note": note} if note else {})})
        candidate.stage = stage
    return candidate


def create_from_submission(session: Session, *, uid: str, profile: dict, contact: dict,
                           summary: str, consent: bool, consent_text: str, cv_name: str,
                           lang: str, messages: Iterable | None) -> tuple[Candidate, bool]:
    """Store one talent chat submission. Idempotent on `uid`: a retry of the same
    submission returns the existing row and creates nothing. Returns (row, created)."""
    existing = session.scalar(select(Candidate).where(Candidate.uid == uid))
    if existing:
        return existing, False

    profile = profile or {}
    contact = contact or {}
    transcript = []
    for m in messages or []:
        role = getattr(m, "role", None) if not isinstance(m, dict) else m.get("role")
        content = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if role in ("user", "assistant") and content:
            transcript.append({"role": role, "content": str(content)[:4000]})
    transcript = transcript[:80]

    name = (contact.get("name") or "").strip()
    email = normalize_email(contact.get("email"))
    li = (contact.get("linkedin") or "").strip() or None

    row = Candidate(
        uid=uid,
        full_name=name or (email or "") or ("New candidate from chat" if lang == "en" else "Nový kandidát z chatu"),
        email=email,
        linkedin_url=li,
        linkedin_identifier=linkedin_identifier(li),
        source="talent_chat",
        lang=(lang or "cs")[:8],
        area=vocab.clean_list(_as_list(profile.get("area")), vocab.AREA),
        level=vocab.clean_list(_as_list(profile.get("level")), vocab.LEVEL),
        work_mode=vocab.clean_list(_as_list(profile.get("workMode")), vocab.WORK_MODE),
        search_status=vocab.clean_list(_as_list(profile.get("status")), vocab.SEARCH_STATUS),
        note=(contact.get("note") or "").strip() or None,
        cv_filename=(cv_name or "").strip() or None,
        summary=(summary or "").strip() or None,
        transcript=transcript or None,
        consent=bool(consent),
        consent_text=(consent_text or "").strip() or None,
        consent_at=utcnow() if consent else None,
        stage="applied",
    )
    session.add(row)
    session.flush()
    add_event(session, row, "submitted", actor="chat", data={"lang": lang, "has_cv": bool(cv_name)})
    return row, True


def apply_score(session: Session, candidate: Candidate, *, score: int, company_tier: str | None,
                education_tier: str | None, fit_areas: list | None, recommendation: str | None,
                reasoning: str | None, breakdown: str | None = None) -> Candidate:
    candidate.score = int(score)
    candidate.company_tier = company_tier if company_tier in vocab.COMPANY_TIERS else None
    candidate.education_tier = education_tier if education_tier in vocab.EDUCATION_TIERS else None
    candidate.fit_areas = vocab.clean_list(fit_areas or [], vocab.FIT_AREAS)
    candidate.recommendation = recommendation if recommendation in vocab.RECOMMENDATIONS else None
    candidate.reasoning = (reasoning or "").strip() or None
    candidate.scored_at = utcnow()
    add_event(session, candidate, "scored", actor="scoring",
              data={"score": candidate.score, "recommendation": candidate.recommendation,
                    **({"breakdown": breakdown} if breakdown else {})})
    return candidate


def list_candidates(session: Session, *, stage: str | None = None, stages: list[str] | None = None,
                    source: str | None = None, q: str | None = None, limit: int = 100,
                    offset: int = 0, include_deleted: bool = False) -> list[Candidate]:
    query = select(Candidate)
    if not include_deleted:
        query = query.where(Candidate.deleted_at.is_(None))
    if stage:
        query = query.where(Candidate.stage == stage)
    if stages:
        query = query.where(Candidate.stage.in_(stages))
    if source:
        query = query.where(Candidate.source == source)
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.where(or_(
            func.lower(Candidate.full_name).like(like),
            func.lower(Candidate.email).like(like),
            func.lower(Candidate.current_company).like(like),
            func.lower(Candidate.summary).like(like),
        ))
    query = query.order_by(Candidate.created_at.desc()).limit(limit).offset(offset)
    return list(session.scalars(query))


def soft_delete(session: Session, candidate: Candidate) -> None:
    candidate.deleted_at = utcnow()


def hard_delete(session: Session, candidate: Candidate) -> None:
    """GDPR erasure: the row and its events and search links go away for good."""
    session.delete(candidate)


# --- searches ---------------------------------------------------------------

def create_search(session: Session, *, company_name: str, role: str, notes: str | None = None,
                  alister_shortlist_id: int | None = None, opened_at: dt.datetime | None = None,
                  legacy_ref: str | None = None) -> Search:
    s = Search(company_name=company_name.strip(), role=role.strip(), notes=notes,
               alister_shortlist_id=alister_shortlist_id, legacy_ref=legacy_ref,
               opened_at=opened_at or utcnow())
    session.add(s)
    session.flush()
    return s


def add_to_search(session: Session, search: Search, candidate: Candidate, *,
                  miton_comment: str | None = None, outcome: str | None = None,
                  position: int | None = None, actor: str | None = None) -> SearchCandidate:
    existing = session.scalar(select(SearchCandidate).where(
        SearchCandidate.search_id == search.id, SearchCandidate.candidate_id == candidate.id))
    if existing:
        if miton_comment and not existing.miton_comment:
            existing.miton_comment = miton_comment
        return existing
    if outcome and outcome not in vocab.OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    if position is None:
        position = (session.scalar(select(func.max(SearchCandidate.position)).where(
            SearchCandidate.search_id == search.id)) or 0) + 1
    link = SearchCandidate(search=search, candidate=candidate, miton_comment=miton_comment,
                           outcome=outcome, position=position)
    session.add(link)
    add_event(session, candidate, "shared_with_founder", actor=actor,
              data={"company_role": f"{search.company_name} / {search.role}"}, search_id=search.id)
    return link


def follow_up_due(session: Session, *, days: int = vocab.FOLLOW_UP_AFTER_DAYS,
                  now: dt.datetime | None = None) -> list[tuple[Candidate, CandidateEvent]]:
    """Candidates with an outreach sent at least `days` ago and no reply, follow-up
    or closing stage since. This is the Notion "To follow up" view."""
    now = now or utcnow()
    cutoff = now - dt.timedelta(days=days)
    sent = list(session.scalars(select(CandidateEvent).where(
        CandidateEvent.type == "outreach_sent", CandidateEvent.happened_at <= cutoff)
        .order_by(CandidateEvent.happened_at)))
    out = []
    for ev in sent:
        cand = ev.candidate
        if cand.deleted_at or cand.stage in ("hired", "rejected", "rejected_after_interview", "not_interested"):
            continue
        later = [e for e in cand.events if aware(e.happened_at) > aware(ev.happened_at)
                 and e.type in ("outreach_replied", "follow_up_sent", "intro_booked")]
        if later:
            continue
        out.append((cand, ev))
    return out


def _as_list(v) -> list:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    return [v]
