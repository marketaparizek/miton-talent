"""Miton Talent schema: the recruiting back office, separate from Alister.

Four tables. They follow the rule in docs/miton-layer-architecture.md: this
database holds Miton's relationships and decisions, never market facts. A row
may point at an Alister profile by URL or id, as a plain value, never a foreign
key, so the two systems stay independent.

  candidates          one row per person Miton is in contact with about a role
                      (chat applicant, sourced, referral). Replaces the Notion
                      inbox "Hledáme chytré lidi" AND "Full databáze kandidátů".
  candidate_events    what happened to a candidate and when: stage changes,
                      outreach drafted / sent / replied, intro booked, notes.
                      Replaces the Notion "Outreach table" and gives the
                      follow-up queue for free.
  searches            a role at a company that Miton sourced for. Replaces the
                      ~80 per-search Notion databases under "Sdílení searchů".
  search_candidates   who was considered for a search, with the founder's
                      rating and comment and the outcome.

Lists (area, level, ...) are stored as JSON arrays so the same schema runs on
SQLite (local, tests) and Postgres (Railway). Vocabularies live in
talent/vocab.py and are enforced in code, not by the database.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_uid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Stable public id used in URLs and by the chat for idempotent submits.
    uid: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, default=new_uid)

    # identity
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    email: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(500))
    # Normalised LinkedIn handle ("jan-novak-123"), the merge key across sources.
    linkedin_identifier: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    # Alister is referenced by value only. No foreign key, no join, ever.
    alister_profile_url: Mapped[Optional[str]] = mapped_column(String(500))
    alister_profile_id: Mapped[Optional[int]] = mapped_column(Integer)

    # origin
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="talent_chat")
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    lang: Mapped[Optional[str]] = mapped_column(String(8))
    # Where the row came from before this database (Notion page id / url) so an
    # import can be re-run without duplicating.
    legacy_ref: Mapped[Optional[str]] = mapped_column(String(255), index=True)

    # what they told us (chat profile) - JSON arrays of vocab strings
    area: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    level: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    work_mode: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    search_status: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[Optional[str]] = mapped_column(Text)          # the candidate's own message
    cv_filename: Mapped[Optional[str]] = mapped_column(String(255))

    # what we know
    current_company: Mapped[Optional[str]] = mapped_column(String(255))
    current_position: Mapped[Optional[str]] = mapped_column(String(255))
    positions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # role tags (CTO, Backend developer, ...)
    sourced_for: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # companies / searches this person was sourced for
    summary: Mapped[Optional[str]] = mapped_column(Text)       # AI recruiter summary
    transcript: Mapped[Optional[list]] = mapped_column(JSON)   # [{role, content}], chat only
    notes: Mapped[Optional[str]] = mapped_column(Text)         # recruiter notes
    hiring_review: Mapped[Optional[str]] = mapped_column(Text)

    # consent (GDPR)
    consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_text: Mapped[Optional[str]] = mapped_column(Text)
    consent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # evaluation (automatic scoring; see app.py SCORE_*)
    score: Mapped[Optional[int]] = mapped_column(Integer)
    company_tier: Mapped[Optional[str]] = mapped_column(String(8))
    education_tier: Mapped[Optional[str]] = mapped_column(String(8))
    fit_areas: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    recommendation: Mapped[Optional[str]] = mapped_column(String(64))
    reasoning: Mapped[Optional[str]] = mapped_column(Text)
    scored_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # pipeline
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="applied", index=True)
    owner: Mapped[Optional[str]] = mapped_column(String(32))          # "Miton" / "Miton C"
    assigned_to: Mapped[Optional[str]] = mapped_column(String(255))   # e-mail of the recruiter
    interviewed_with: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    newsletter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # housekeeping
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["CandidateEvent"]] = relationship(
        "CandidateEvent", back_populates="candidate", cascade="all, delete-orphan",
        order_by="CandidateEvent.happened_at",
    )
    search_links: Mapped[list["SearchCandidate"]] = relationship(
        "SearchCandidate", back_populates="candidate", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_candidates_stage_created", "stage", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Candidate id={self.id} uid={self.uid} stage={self.stage}>"


class CandidateEvent(Base):
    __tablename__ = "candidate_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # See talent/vocab.py EVENT_TYPES: stage_changed, outreach_drafted, outreach_sent,
    # outreach_replied, follow_up_sent, intro_booked, note, imported, scored, ...
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    happened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    actor: Mapped[Optional[str]] = mapped_column(String(255))   # who did it: e-mail, "chat", "import", "skill:outreach"
    # Free-form details: {"from": "applied", "to": "contacted"}, {"channel": "email",
    # "mode": "pitch", "company_role": "..."}, {"text": "..."}.
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    search_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("searches.id", ondelete="SET NULL"), index=True
    )

    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="events")

    def __repr__(self) -> str:
        return f"<CandidateEvent {self.type} candidate={self.candidate_id}>"


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, default=new_uid)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")  # open / closed
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    closed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Alister shortlist this came from, by value only.
    alister_shortlist_id: Mapped[Optional[int]] = mapped_column(Integer)
    # Founder-facing page: /s/<share_token>. NULL = not shared. Rotate to revoke.
    share_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    share_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    legacy_ref: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    candidates: Mapped[list["SearchCandidate"]] = relationship(
        "SearchCandidate", back_populates="search", cascade="all, delete-orphan",
        order_by="SearchCandidate.position",
    )

    def __repr__(self) -> str:
        return f"<Search id={self.id} {self.company_name} / {self.role}>"


class SearchCandidate(Base):
    __tablename__ = "search_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    search_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("searches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[Optional[int]] = mapped_column(Integer)
    founder_rating: Mapped[Optional[str]] = mapped_column(String(16))   # liked / disliked / maybe
    founder_comment: Mapped[Optional[str]] = mapped_column(Text)
    miton_comment: Mapped[Optional[str]] = mapped_column(Text)          # "why curated" / fit bullets
    outcome: Mapped[Optional[str]] = mapped_column(String(32))          # see vocab OUTCOMES
    added_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    search: Mapped["Search"] = relationship("Search", back_populates="candidates")
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="search_links")

    __table_args__ = (
        UniqueConstraint("search_id", "candidate_id", name="uq_search_candidate"),
    )
