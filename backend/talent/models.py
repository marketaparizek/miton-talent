"""Miton Talent schema: the recruiting back office, separate from Alister.

Four data tables plus one for sign-in. They follow the rule in docs/miton-layer-architecture.md: this
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

  used_handoff_tokens  ids of the one-minute sign-in tokens Alister has handed
                      over and this service has already accepted, so a token
                      cannot open two sessions (talent/auth.py).

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


class UsedHandoffToken(Base):
    """A handoff token id (``jti``) that has already opened a session.

    Alister mints each token once with a fresh id; the first callback records
    the id here and a second callback with the same token finds the row and
    refuses. Rows are dropped an hour after the token's own expiry, when a
    replay would fail on ``exp`` anyway.
    """

    __tablename__ = "used_handoff_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


# --- Portfolio open roles -----------------------------------------------------
# The weekly scrape of every Miton portfolio company's careers page. Three
# tables and one join row per run:
#
#   portfolio_companies   the portfolio itself: who is in it, where its careers
#                         page is, how to read it. Miton's own list, so it lives
#                         here and not in Alister (docs/miton-layer-architecture.md).
#   portfolio_runs        one row per weekly scrape, with its totals.
#   portfolio_company_runs  what one run saw at one company: status, method,
#                         how many roles, the error if it failed. This is what
#                         keeps a zero honest: "nothing published" and "the page
#                         did not load" must never look the same.
#   portfolio_roles       one row per requisition, kept across runs.
#                         first_seen_at / last_seen_at / closed_at give the
#                         "new this week / closed this week" diff for free, so
#                         no snapshot table is needed.


class PortfolioCompany(Base):
    __tablename__ = "portfolio_companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Miton's portfolio stage ("Early Growth", "Scaling", "Mature"). NULL for a
    # company the public portfolio page does not list (Boski, ACE).
    stage: Mapped[Optional[str]] = mapped_column(String(32))
    not_on_site: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Companies merged into one holding still show separately because miton.cz
    # lists them separately; this says which ones are one hiring plan.
    group_name: Mapped[Optional[str]] = mapped_column(String(64))
    website: Mapped[Optional[str]] = mapped_column(String(500))
    careers_url: Mapped[Optional[str]] = mapped_column(String(500))
    # Which adapter reads the careers page: recruitee, personio, smartrecruiters,
    # ashby, greenhouse, lever, workable, teamtailor, recruitis, html (LLM), or
    # NULL to let the scraper detect it from careers_url.
    adapter: Mapped[Optional[str]] = mapped_column(String(32))
    # Adapter arguments the URL does not carry, plus per-company overrides:
    # {"board": "GLAMI1", "aliases": {"<fingerprint>": "<fingerprint>"}, "skip": true}
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    cats: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    description: Mapped[Optional[str]] = mapped_column(Text)
    # The standing caveat a reader needs before trusting this company's count
    # ("hires through Discord", "page is a JS shell"). Written by hand, kept
    # across runs; the run's own message goes to PortfolioCompanyRun.note.
    note: Mapped[Optional[str]] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    roles: Mapped[list["PortfolioRole"]] = relationship(
        "PortfolioRole", back_populates="company", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<PortfolioCompany {self.slug}>"


class PortfolioRun(Base):
    __tablename__ = "portfolio_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    # running / done / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    actor: Mapped[Optional[str]] = mapped_column(String(255))   # "cron", an e-mail, "seed"
    companies_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    companies_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    roles_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    roles_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    roles_closed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)

    company_runs: Mapped[list["PortfolioCompanyRun"]] = relationship(
        "PortfolioCompanyRun", back_populates="run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<PortfolioRun {self.id} {self.status}>"


class PortfolioCompanyRun(Base):
    __tablename__ = "portfolio_company_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("portfolio_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("portfolio_companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # ok            the page was read and its roles counted (0 is a real zero)
    # no_careers_page  the company has no careers page to read
    # blocked       the page answered, but refused us (403, bot wall, JS shell)
    # failed        the fetch or the parse broke; the count is NOT trustworthy
    # skipped       config says do not scrape this one
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    adapter: Mapped[Optional[str]] = mapped_column(String(32))
    source_url: Mapped[Optional[str]] = mapped_column(String(500))
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    roles_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # before collapsing duplicates
    note: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)

    run: Mapped["PortfolioRun"] = relationship("PortfolioRun", back_populates="company_runs")

    __table_args__ = (
        UniqueConstraint("run_id", "company_id", name="uq_portfolio_company_run"),
    )


class PortfolioRole(Base):
    """One open requisition at one portfolio company, kept across runs.

    ``fingerprint`` is the normalised title (see portfolio/collapse.py): the same
    job posted in four cities, or in Czech and German, is one requisition, so
    every location it was seen in is collected in ``locations``.
    """

    __tablename__ = "portfolio_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("portfolio_companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(255))     # display value
    locations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    team: Mapped[Optional[str]] = mapped_column(String(255))
    employment_type: Mapped[Optional[str]] = mapped_column(String(64))
    url: Mapped[Optional[str]] = mapped_column(String(1000))
    # Function bucket (see portfolio/classify.py FUNCTIONS) and whether it counts
    # as technical; both derived, both stored so the view never re-computes.
    fn: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    fn_source: Mapped[Optional[str]] = mapped_column(String(16))     # rules / llm / seed / manual
    is_technical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cc: Mapped[Optional[str]] = mapped_column(String(8))

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    first_seen_run_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_run_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    # Set when a run read the company's page successfully and the role was gone.
    # A failed fetch never closes a role.
    closed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    closed_run_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    company: Mapped["PortfolioCompany"] = relationship("PortfolioCompany", back_populates="roles")

    __table_args__ = (
        # A role that closes and is posted again later gets a new row (a new
        # first_seen_run_id), so the pair (company, fingerprint) may repeat over
        # time. "At most one OPEN row per pair" cannot be a NULL-tolerant unique
        # index on either database, so store.py enforces it on the write path.
        UniqueConstraint("company_id", "fingerprint", "first_seen_run_id", name="uq_portfolio_role_run"),
        Index("ix_portfolio_roles_company_closed", "company_id", "closed_at"),
    )

    def __repr__(self) -> str:
        return f"<PortfolioRole {self.title!r} company={self.company_id}>"
