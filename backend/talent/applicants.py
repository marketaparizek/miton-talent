"""The applicant inbox, served to Alister's /miton hub and its MCP.

This replaces the Notion inbox "Hledáme chytré lidi". The people live in this
database; Alister draws the list in its own design and the candidate-triage
skill reads it through Alister's MCP. Neither ever holds a copy.

  GET  /api/applicants                 the inbox: one short row per person
  GET  /api/applicants/{uid}           one person in full: chat, scoring, history
  POST /api/applicants/{uid}/stage     move them on (contacted, rejected, ...)
  POST /api/applicants/{uid}/notes     add a recruiter note

Reads need an ``applicants-read`` token, writes an ``applicants-write`` token
that names the @miton.cz person making the change (talent/auth.py). The CV is
never stored (app.py emails it), so a row only says whether one came.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from talent import auth as tauth
from talent import db, store, vocab
from talent.models import Candidate

log = logging.getLogger("miton-talent.applicants")

router = APIRouter(prefix="/api/applicants", tags=["applicants"])

_NO_STORE = {"Cache-Control": "no-store"}

# People who came to us, as opposed to people we sourced. The inbox is theirs.
INBOUND_SOURCES = ["talent_chat", "typeform", "startupjobs"]

# The triage skill never drafts a rejection inside this window: a same-day no
# looks like nobody read it. Invitations have no delay.
REJECTION_DELAY = dt.timedelta(hours=48)

# What the inbox is for: deciding about people who have not had an answer yet.
INBOX_STAGES = ["applied"]

MAX_LIMIT = 200


def _iso(d: Optional[dt.datetime]) -> Optional[str]:
    d = store.aware(d)
    return d.isoformat() if d else None


def _applied_at(c: Candidate) -> Optional[dt.datetime]:
    return store.aware(c.first_seen_at or c.created_at)


def _row(c: Candidate, now: dt.datetime) -> dict:
    applied = _applied_at(c)
    return {
        "uid": c.uid,
        "name": c.full_name,
        "email": c.email,
        "linkedin_url": c.linkedin_url,
        "source": c.source,
        "lang": c.lang,
        "applied_at": _iso(applied),
        "waiting_days": (now - applied).days if applied else None,
        "rejection_eligible_at": _iso(applied + REJECTION_DELAY) if applied else None,
        "stage": c.stage,
        "area": c.area or [],
        "level": c.level or [],
        "work_mode": c.work_mode or [],
        "search_status": c.search_status or [],
        "current_company": c.current_company,
        "current_position": c.current_position,
        "summary": c.summary,
        "has_cv": bool(c.cv_filename),
        "score": c.score,
        "recommendation": c.recommendation,
        "company_tier": c.company_tier,
        "education_tier": c.education_tier,
        "fit_areas": c.fit_areas or [],
    }


def _detail(c: Candidate, now: dt.datetime) -> dict:
    out = _row(c, now)
    out.update({
        "note": c.note,
        "notes": c.notes,
        "cv_filename": c.cv_filename,
        "reasoning": c.reasoning,
        "scored_at": _iso(c.scored_at),
        "transcript": c.transcript or [],
        "consent": bool(c.consent),
        "consent_version": c.consent_version,
        "consent_at": _iso(c.consent_at),
        "events": [
            {"type": e.type, "at": _iso(e.happened_at), "actor": e.actor, "data": e.data or {}}
            for e in c.events
        ],
    })
    return out


def _get(session, uid: str) -> Candidate:
    c = session.scalar(select(Candidate).where(Candidate.uid == uid, Candidate.deleted_at.is_(None)))
    if c is None:
        raise HTTPException(status_code=404, detail="no such applicant")
    return c


@router.get("")
def list_applicants(
    caller: dict = Depends(tauth.require_applicants_read),
    stage: Optional[str] = Query(None, description="one stage; default is the inbox (applied)"),
    q: Optional[str] = Query(None, max_length=100),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """Inbound applicants, newest first, plus how many sit in each stage."""
    if stage and stage not in vocab.STAGES:
        raise HTTPException(status_code=400, detail=f"unknown stage {stage!r}")
    stages = [stage] if stage else INBOX_STAGES
    now = dt.datetime.now(dt.timezone.utc)
    with db.session() as s:
        base = (Candidate.deleted_at.is_(None), Candidate.source.in_(INBOUND_SOURCES))
        counts = dict(s.execute(
            select(Candidate.stage, func.count()).where(*base).group_by(Candidate.stage)
        ).all())
        where = [*base, Candidate.stage.in_(stages)]
        if q and q.strip():
            like = f"%{q.strip().lower()}%"
            where.append(or_(
                func.lower(Candidate.full_name).like(like),
                func.lower(Candidate.email).like(like),
                func.lower(Candidate.current_company).like(like),
                func.lower(Candidate.summary).like(like),
            ))
        total = s.scalar(select(func.count()).select_from(Candidate).where(*where)) or 0
        page = list(s.scalars(
            select(Candidate).where(*where)
            .order_by(Candidate.first_seen_at.desc(), Candidate.id.desc())
            .limit(limit).offset(offset)
        ))
        body = {
            "stage": stage or "inbox",
            "stages": stages,
            "total": total,
            "counts": {st: int(counts.get(st, 0)) for st in vocab.STAGES},
            "stage_labels": vocab.STAGE_LABELS,
            "items": [_row(r, now) for r in page],
            "generated_at": now.isoformat(),
        }
    log.info("applicants list served to %s (%d rows)", caller.get("service"), len(body["items"]))
    return JSONResponse(body, headers=_NO_STORE)


@router.get("/{uid}")
def get_applicant(uid: str, caller: dict = Depends(tauth.require_applicants_read)):
    now = dt.datetime.now(dt.timezone.utc)
    with db.session() as s:
        body = _detail(_get(s, uid), now)
    return JSONResponse(body, headers=_NO_STORE)


class StageIn(BaseModel):
    stage: str
    note: Optional[str] = Field(None, max_length=2000)
    # What the person was sent, when the change follows a reply: "invitation"
    # or "rejection". Logged as outreach_sent so the history says why.
    reply: Optional[str] = None


@router.post("/{uid}/stage")
def set_applicant_stage(uid: str, body: StageIn, caller: dict = Depends(tauth.require_applicants_write)):
    if body.stage not in vocab.STAGES:
        raise HTTPException(status_code=400, detail=f"unknown stage {body.stage!r}")
    if body.reply not in (None, "invitation", "rejection"):
        raise HTTPException(status_code=400, detail="reply must be invitation or rejection")
    actor = caller["email"]
    now = dt.datetime.now(dt.timezone.utc)
    with db.session() as s:
        c = _get(s, uid)
        before = c.stage
        if body.reply:
            store.add_event(s, c, "outreach_sent", actor=actor,
                            data={"channel": "email", "mode": body.reply})
        store.set_stage(s, c, body.stage, actor=actor, note=(body.note or "").strip() or None)
        s.flush()
        out = {"uid": c.uid, "from": before, "stage": c.stage, "changed": before != c.stage,
               "applicant": _row(c, now)}
    log.info("applicant stage %s -> %s by %s", before, body.stage, actor)
    return JSONResponse(out, headers=_NO_STORE)


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@router.post("/{uid}/notes")
def add_applicant_note(uid: str, body: NoteIn, caller: dict = Depends(tauth.require_applicants_write)):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty note")
    with db.session() as s:
        c = _get(s, uid)
        store.add_event(s, c, "note", actor=caller["email"], data={"text": text})
    return JSONResponse({"uid": uid, "ok": True}, headers=_NO_STORE)
