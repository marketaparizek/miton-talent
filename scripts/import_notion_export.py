#!/usr/bin/env python3
"""Load the Notion export (scripts/export_notion.py) into the Miton Talent database.

Re-runnable: every imported row remembers its Notion page id in `legacy_ref`, so
running the import twice updates instead of duplicating. People are merged
across the inbox, the candidate pool and the per-search tables by, in order:
Notion page id, LinkedIn handle, e-mail.

  DATABASE_URL=... python scripts/import_notion_export.py --export ~/miton-notion-export
  ... --dry-run          count and report, write nothing
  ... --duplicates out.csv   write the probable cross-source matches for review
  ... --skip-rejected    leave out inbox rows with Status "Rejected" (1 257 rows
                         of old Typeform/StartupJobs history), recommended

Mapping summary (see docs/notion-replacement-analysis.md, section 5):
  inbox Status     Unprocessed -> applied, Contacted -> contacted,
                   Placed in database -> sourced, Rejected -> rejected
  inbox Source     Talent chat / StartupJobs / Typeform / Sourcing / Referral;
                   empty -> typeform (rows before 2026-07) else startupjobs
  pool Stage       verbatim (lower-cased, spaces -> underscores)
  search Status    -> search_candidates.outcome (same vocabulary)
  outreach rows    -> candidate_events outreach_drafted / outreach_sent (+ follow_up_sent)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "backend"))

from talent import db, store, vocab  # noqa: E402
from talent.models import Candidate, Search, utcnow  # noqa: E402

INBOX_STATUS_TO_STAGE = {
    "Unprocessed": "applied", "Contacted": "contacted",
    "Placed in database": "sourced", "Rejected": "rejected",
}
SOURCE_MAP = {"Talent chat": "talent_chat", "StartupJobs": "startupjobs", "Typeform": "typeform",
              "Sourcing": "sourcing", "Referral": "referral"}
STANDARD_SEARCH_COLUMNS = {
    "Name", "Contact", "Current company", "Current position", "Interviewed with", "Linkedin/CV",
    "Owner kandidáta", "Placed in newsletter", "Position", "Poznámky", "Sourced for", "Start date",
    "Status", "Tags", "TM - comments", "Candidate owner", "Alister profil", "Notes", "Hiring review",
    "Shared with founder", "Stage", "Search", "Parent item", "Sub-item",
}


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _url(v) -> str | None:
    """Notion url / files / rich_text values as one string (first file's url for files)."""
    if v is None:
        return None
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("url") or v.get("name")
    v = (str(v) if v is not None else "").strip()
    return v or None


def _same_person(a: str | None, b: str | None) -> bool:
    """Loose name agreement for e-mail merges: empty name, or a shared surname token."""
    ta = {t for t in re.split(r"[\s,]+", (a or "").lower()) if len(t) > 2}
    tb = {t for t in re.split(r"[\s,]+", (b or "").lower()) if len(t) > 2}
    return not ta or not tb or bool(ta & tb)


def slug(v: str | None) -> str | None:
    if not v:
        return None
    return re.sub(r"[^a-z0-9]+", "_", v.strip().lower()).strip("_")


def read_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def body_text(export: Path, page_id: str) -> tuple[list[dict], str | None]:
    """Rebuild the chat transcript and the evaluation reasoning from page blocks."""
    p = export / "bodies" / f"{page_id}.json"
    if not p.exists():
        return [], None
    blocks = json.loads(p.read_text(encoding="utf-8"))
    transcript, reasoning, section = [], [], None
    for b in blocks:
        t = (b.get("text") or "").strip()
        if b.get("type", "").startswith("heading"):
            section = t.lower()
            continue
        if not t:
            continue
        if section and ("přepis" in section or "transcript" in section):
            m = re.match(r"^(Kandidát|Candidate|Miton):\s*(.*)$", t, re.S)
            if m:
                role = "user" if m.group(1) in ("Kandidát", "Candidate") else "assistant"
                transcript.append({"role": role, "content": m.group(2).strip()})
        elif section and ("evaluace" in section or "evaluation" in section):
            reasoning.append(t)
    return transcript, ("\n".join(reasoning) or None)


class Importer:
    def __init__(self, export: Path, dry_run: bool, skip_rejected: bool):
        self.export, self.dry_run, self.skip_rejected = export, dry_run, skip_rejected
        self.stats = {"inbox": 0, "pool": 0, "searches": 0, "search_rows": 0, "outreach": 0,
                      "linked_searches": 0, "linked_rows": 0,
                      "merged_by_linkedin": 0, "merged_by_email": 0, "skipped_rejected": 0}
        self.duplicates: list[dict] = []

    # --- helpers ------------------------------------------------------------
    def _get_or_create(self, s, *, legacy_ref: str, name: str, email: str | None, linkedin: str | None,
                       source: str, created: dt.datetime | None, origin: str) -> tuple[Candidate, bool]:
        row = store.find_candidate(s, legacy_ref=legacy_ref)
        if row:
            return row, False
        row = store.find_candidate(s, linkedin_url=linkedin, email=email)
        if row:
            key = "linkedin" if store.linkedin_identifier(linkedin) and row.linkedin_identifier == store.linkedin_identifier(linkedin) else "email"
            if key == "email" and not _same_person(name, row.full_name):
                # shared / generic mailbox: do not merge, but ask a human
                self.duplicates.append({"origin": origin, "notion": legacy_ref, "name": name,
                                        "matched_id": row.id, "matched_name": row.full_name,
                                        "key": "email_conflict"})
                row = None
        if row:
            self.stats[f"merged_by_{key}"] += 1
            if key == "email":
                self.duplicates.append({"origin": origin, "notion": legacy_ref, "name": name,
                                        "matched_id": row.id, "matched_name": row.full_name, "key": key})
            if not row.legacy_ref:
                row.legacy_ref = legacy_ref
            return row, False
        row = Candidate(full_name=name or (email or "") or "(bez jména)", email=store.normalize_email(email),
                        linkedin_url=linkedin or None, linkedin_identifier=store.linkedin_identifier(linkedin),
                        source=source, first_seen_at=created or utcnow(), legacy_ref=legacy_ref,
                        created_at=created or utcnow())
        s.add(row)
        s.flush()
        store.add_event(s, row, "imported", actor="import", data={"notion": legacy_ref, "origin": origin},
                        happened_at=created or utcnow())
        return row, True

    # --- inbox: Hledáme chytré lidi ----------------------------------------
    def inbox(self, s):
        for r in read_jsonl(self.export / "hledame_chytre_lidi.jsonl"):
            status = r.get("Status") or "Unprocessed"
            if self.skip_rejected and status == "Rejected":
                self.stats["skipped_rejected"] += 1
                continue
            created = parse_ts(r.get("created_time"))
            src = SOURCE_MAP.get(r.get("Source") or "")
            if not src:
                src = "typeform" if (created and created < dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)) else "startupjobs"
            name = (r.get("Name") or r.get("Jméno") or "").strip()
            row, created_now = self._get_or_create(
                s, legacy_ref=r["id"], name=name, email=_url(r.get("E-mail")), linkedin=_url(r.get("LinkedIn")),
                source=src, created=created, origin="inbox")
            row.note = row.note or (r.get("Application") or None)
            row.summary = row.summary or (r.get("Summary") or None)
            row.area = row.area or vocab.clean_list(r.get("Oblast") or [], vocab.AREA)
            row.level = row.level or [v for v in (r.get("Level") or []) if isinstance(v, str)]
            row.work_mode = row.work_mode or vocab.clean_list(r.get("Remote?") or [], vocab.WORK_MODE)
            row.search_status = row.search_status or vocab.clean_list(r.get("Aktivita hledání") or [], vocab.SEARCH_STATUS)
            row.cv_filename = row.cv_filename or ((r.get("CV") or [{}])[0].get("name") if r.get("CV") else None)
            if r.get("Score") is not None and row.score is None:
                row.score = int(r["Score"])
                row.company_tier = r.get("Company tier") if r.get("Company tier") in vocab.COMPANY_TIERS else None
                row.education_tier = r.get("Education") if r.get("Education") in vocab.EDUCATION_TIERS else None
                row.fit_areas = vocab.clean_list(r.get("Fit oblast") or [], vocab.FIT_AREAS)
                row.recommendation = r.get("Doporučení") if r.get("Doporučení") in vocab.RECOMMENDATIONS else None
            transcript, reasoning = body_text(self.export, r["id"])
            if transcript and not row.transcript:
                row.transcript = transcript
            if reasoning and not row.reasoning:
                row.reasoning = reasoning
            if src == "talent_chat":
                row.consent = True   # the chat enforces consent server-side
            stage = INBOX_STATUS_TO_STAGE.get(status, "applied")
            if created_now or row.stage == "applied":
                row.stage = stage
            if r.get("Call/schůzka"):
                store.add_event(s, row, "call", actor="import", data={"when": r["Call/schůzka"]},
                                happened_at=parse_ts(r["Call/schůzka"]) or utcnow())
            self.stats["inbox"] += 1

    # --- pool: Full databáze kandidátů --------------------------------------
    def pool(self, s):
        for r in read_jsonl(self.export / "full_databaze_kandidatu.jsonl"):
            created = parse_ts(r.get("created_time"))
            row, created_now = self._get_or_create(
                s, legacy_ref=r["id"], name=(r.get("Name") or "").strip(), email=_url(r.get("Contact")),
                linkedin=_url(r.get("Linkedin/CV")), source="sourcing", created=created, origin="pool")
            row.current_company = row.current_company or r.get("Current company") or None
            row.current_position = row.current_position or r.get("Current position") or None
            row.positions = sorted(set(row.positions or []) | set(r.get("Position") or []))
            row.sourced_for = sorted(set(row.sourced_for or []) | set(r.get("Sourced for") or []))
            row.interviewed_with = sorted(set(row.interviewed_with or []) | set(r.get("Interviewed with") or []))
            row.owner = row.owner or r.get("Candidate owner") or None
            row.hiring_review = row.hiring_review or r.get("Hiring review") or None
            row.newsletter = row.newsletter or (r.get("Placed in newsletter") is True)
            row.alister_profile_url = row.alister_profile_url or r.get("Alister profil") or None
            notes = "\n".join(x for x in [r.get("Poznámky"), r.get("Notes")] if x)
            if notes and (not row.notes or notes not in row.notes):
                row.notes = (row.notes + "\n" + notes) if row.notes else notes
            stage = slug(r.get("Stage"))
            if stage in vocab.STAGES and (created_now or row.stage in ("applied", "sourced")):
                row.stage = stage
            elif r.get("Status") in ("Hired", "Hired with Miton lead"):
                row.stage = "hired"
            elif created_now:
                row.stage = "sourced"
            self.stats["pool"] += 1

    # --- per-search tables under Sdílení searchů ---------------------------
    def searches(self, s):
        seen_linked: dict[tuple, Search] = {}
        for idx in read_jsonl(self.export / "searches" / "index.jsonl"):
            if idx.get("linked"):
                self._linked_search(s, idx, seen_linked)
                continue
            title = (idx.get("title") or "").strip() or idx["id"]
            company, _, role = [x.strip() for x in title.partition("-")] if "-" in title else (title, "", "")
            if not role and "—" in title:
                company, _, role = [x.strip() for x in title.partition("—")]
            path = idx.get("path") or []
            opened = None
            for p in reversed(path):
                m = re.match(r"^\s*(\d{1,2})/(\d{4})", p or "")
                if m:
                    opened = dt.datetime(int(m.group(2)), int(m.group(1)), 1, tzinfo=dt.timezone.utc)
                    break
            search = s.query(Search).filter(Search.legacy_ref == idx["id"]).one_or_none()
            if not search:
                search = store.create_search(s, company_name=company or title, role=role or title,
                                             opened_at=opened, legacy_ref=idx["id"])
                search.status = "closed"
            self.stats["searches"] += 1
            for r in read_jsonl(self.export / "searches" / f"{idx['id']}.jsonl"):
                created = parse_ts(r.get("created_time"))
                row, _ = self._get_or_create(
                    s, legacy_ref=r["id"], name=(r.get("Name") or "").strip(), email=_url(r.get("Contact")),
                    linkedin=_url(r.get("Linkedin/CV")), source="sourcing", created=created, origin=f"search:{title}")
                row.current_company = row.current_company or r.get("Current company") or None
                row.current_position = row.current_position or r.get("Current position") or None
                row.positions = sorted(set(row.positions or []) | set(r.get("Position") or []))
                row.sourced_for = sorted(set(row.sourced_for or []) | set(r.get("Sourced for") or []) | ({company} if company else set()))
                row.interviewed_with = sorted(set(row.interviewed_with or []) | set(r.get("Interviewed with") or []))
                founder_bits = [f"{k}: {v}" for k, v in r.items()
                                if k not in STANDARD_SEARCH_COLUMNS and not k.startswith("_")
                                and k not in ("id", "url", "created_time", "last_edited_time", "archived")
                                and isinstance(v, str) and v.strip()]
                outcome = slug(r.get("Status"))
                link = store.add_to_search(
                    s, search, row, miton_comment=(r.get("Poznámky") or r.get("TM - comments") or None),
                    outcome=outcome if outcome in vocab.OUTCOMES else None, actor="import")
                if outcome in ("hired", "hired_with_miton_lead"):
                    row.stage = "hired"
                if founder_bits and not link.founder_comment:
                    link.founder_comment = "\n".join(founder_bits)
                if link.added_at and created:
                    link.added_at = created
                self.stats["search_rows"] += 1

    # --- linked views of the pool (PangeAI-GTM, Whisper- FE, Firefish - ...) ---
    # These Notion "databases" were filtered views of "Full databáze kandidátů"
    # (Sourced for = company, Position = role). The rows live in the pool export, so
    # the search is rebuilt from there: members = pool rows sourced for that company
    # whose Position matches the role in the title (or all of the company's rows when
    # the role cannot be matched). Approximate by construction; the search note says so.
    ROLE_TO_POSITIONS = {
        "gtm": ["Business development manager", "Sales manager", "Head of Growth", "CRO", "CSO",
                "Expansion manager", "Partnership manager", "Business development", "Head of sales"],
        "product manager": ["Product manager", "Growth Product Manager", "CPO"],
        "growth product manager": ["Growth Product Manager"],
        "growth product marketing manager": ["Growth Product Marketing Manager"],
        "ux/product design": ["UX/product designer", "Product designer"],
        "product designer": ["Product designer", "UX/product designer"],
        "ai": ["AI/ML developer", "AI/MI team lead", "AI researcher", "AI/data scientist", "Data scientist", "AI analyst"],
        "ai data scientist": ["AI/data scientist", "Data scientist", "AI/ML developer"],
        "data scientist": ["Data scientist", "AI/data scientist"],
        "devops": ["DevOps"],
        "fe": ["Frontend developer", "React developer", "Fullstack developer"],
        "be": ["Backend developer", "Python developer", "Node.js developer", "Java developer", "PHP developer", "Fullstack developer"],
        "backend developer/cto": ["Backend developer", "CTO", "Fullstack developer"],
        "python developer": ["Python developer", "Backend developer"],
        "fullstack developer": ["Fullstack developer", "Frontend developer", "Backend developer", "React developer"],
        "tech lead/full-stack developer": ["Fullstack developer", "CTO", "Backend developer"],
        "sales manager": ["Sales manager", "Business development manager", "Head of sales"],
        "ceo potential": ["CEO potential", "CEO", "co-founder potential", "Founders material", "CEO potential with finance focus"],
        "ceo": ["CEO", "CEO potential", "co-founder potential"],
        "co-founder": ["co-founder potential", "Founders material", "CEO potential"],
        "performance marketing": ["Performance consultant (PPCRTB)", "Head of Performance", "Marketing manager"],
        "p&o business partner": ["HR manager", "Recruiter"],
        "ai trends & market analyst": ["AI analyst", "Research analyst", "Investment analyst", "AI researcher"],
        "market analyst": ["Research analyst", "AI analyst", "Investment analyst"],
        "investiční": ["Investment analyst", "Investment manager", "Financial analyst"],
    }
    TITLE_COMPANY_ALIASES = {"pangeai": "PangeAI", "whisper": "Whisper", "firefish": "Firefish",
                             "coinmate": "Coinmate", "deepscout": "Deepscout", "aim": "Aim", "psyon": "Psyon",
                             "knihobot": "Knihobot", "glami": "GLAMI", "deinsy": "Deinsy", "behavera": "Behavera",
                             "miton": "Miton", "miton c": "Miton C"}

    def _linked_search(self, s, idx: dict, seen: dict):
        raw = (idx.get("title") or "").strip()
        if not raw or raw.lower() == "untitled":
            return
        # "PangeAI-GTM", "Whisper- FE", "Firefish - Growth Product Manager", "CEO-Psyon"
        parts = [p.strip() for p in re.split(r"\s*[-—]\s*", raw, maxsplit=1)]
        company, role = (parts + [""])[:2]
        if company.lower() in ("ceo", "cto") and role:         # "CEO-Psyon" is role-company
            company, role = role, company
        company_key = self.TITLE_COMPANY_ALIASES.get(company.lower().rstrip(" -"), company)
        role_clean = re.sub(r"[“”\"]", "", role).strip()
        path = idx.get("path") or []
        opened = None
        for p in reversed(path):
            m = re.match(r"^\s*(\d{1,2})/(\d{4})", p or "")
            if m:
                opened = dt.datetime(int(m.group(2)), int(m.group(1)), 1, tzinfo=dt.timezone.utc)
                break
        key = (company_key.lower(), role_clean.lower(), opened.strftime("%Y-%m") if opened else "")
        if key in seen:
            return                                              # same linked view listed twice
        search = s.query(Search).filter(Search.legacy_ref == idx["id"]).one_or_none()
        if not search:
            search = store.create_search(s, company_name=company_key, role=role_clean or raw,
                                         opened_at=opened, legacy_ref=idx["id"])
            search.status = "closed"
            search.notes = ("Rebuilt from a Notion linked view of Full databáze kandidátů "
                            "(filter: Sourced for + Position). Membership is approximate.")
        seen[key] = search
        self.stats["linked_searches"] += 1

        wanted = None
        for k, positions in self.ROLE_TO_POSITIONS.items():
            if k and k in role_clean.lower():
                wanted = set(positions)
                break
        company_rows = [c for c in s.query(Candidate).filter(Candidate.deleted_at.is_(None)).all()
                        if company_key in (c.sourced_for or [])]
        members = [c for c in company_rows if wanted is None or wanted & set(c.positions or [])]
        if not members:
            members = company_rows
        for c in members:
            outcome = None
            if c.stage == "hired":
                outcome = "hired"
            store.add_to_search(s, search, c, outcome=outcome, actor="import")
            self.stats["linked_rows"] += 1

    # --- outreach table -----------------------------------------------------
    def outreach(self, s):
        for r in read_jsonl(self.export / "outreach_table.jsonl"):
            created = parse_ts(r.get("created_time"))
            row, _ = self._get_or_create(
                s, legacy_ref=r["id"], name=(r.get("Name") or "").strip(), email=_url(r.get("E-mail")),
                linkedin=_url(r.get("LinkedIn")), source=SOURCE_MAP.get(r.get("Source") or "", "sourcing"),
                created=created, origin="outreach")
            channel = (r.get("Channel") or "").lower() or None
            mode = "referral_ask" if (r.get("Mode") or "") == "Referral ask" else "pitch"
            data = {"channel": channel, "mode": mode, "company_role": r.get("Company and role"),
                    "note": r.get("Note"), "notion": r["id"]}
            already = {(e.type, (e.data or {}).get("notion")) for e in row.events}
            if ("outreach_drafted", r["id"]) not in already:
                store.add_event(s, row, "outreach_drafted", actor="import", data=data, happened_at=created or utcnow())
            sent = parse_ts(r.get("Sent"))
            if sent and ("outreach_sent", r["id"]) not in already:
                store.add_event(s, row, "outreach_sent", actor="import", data=data, happened_at=sent)
            fu = parse_ts(r.get("Follow-up sent"))
            if fu and ("follow_up_sent", r["id"]) not in already:
                store.add_event(s, row, "follow_up_sent", actor="import", data=data, happened_at=fu)
            status = r.get("Outreach status")
            if status == "Replied" and ("outreach_replied", r["id"]) not in already:
                store.add_event(s, row, "outreach_replied", actor="import", data=data, happened_at=fu or sent or utcnow())
            if status == "Intro booked" and ("intro_booked", r["id"]) not in already:
                store.add_event(s, row, "intro_booked", actor="import", data=data, happened_at=fu or sent or utcnow())
            self.stats["outreach"] += 1

    def run(self):
        with db.session() as s:
            self.inbox(s)
            self.pool(s)
            self.searches(s)
            self.outreach(s)
            if self.dry_run:
                s.rollback()
                # rollback happens again harmlessly on exit; nothing is committed
                raise _DryRun()


class _DryRun(Exception):
    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-rejected", action="store_true")
    ap.add_argument("--duplicates", help="CSV path for the probable cross-source matches")
    args = ap.parse_args()
    imp = Importer(Path(args.export).expanduser(), args.dry_run, args.skip_rejected)
    try:
        imp.run()
        committed = True
    except _DryRun:
        committed = False
    if args.duplicates:
        with open(args.duplicates, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["origin", "notion", "name", "matched_id", "matched_name", "key"])
            w.writeheader()
            w.writerows(imp.duplicates)
    print(json.dumps({"committed": committed, **imp.stats, "duplicates": len(imp.duplicates)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
