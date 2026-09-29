"""The Notion import over a tiny synthetic export: merge keys, stages, searches, outreach."""
import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

from talent import db, store  # noqa: E402
from talent.models import Base, Candidate, Search  # noqa: E402


def _load_importer():
    path = os.path.join(HERE, "..", "..", "scripts", "import_notion_export.py")
    spec = importlib.util.spec_from_file_location("import_notion_export", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


@pytest.fixture()
def export(tmp_path):
    inbox = [
        {"id": "p-chat-1", "created_time": "2026-08-10T10:00:00.000Z", "Name": "Jan Novák",
         "E-mail": "jan@example.com", "LinkedIn": "https://www.linkedin.com/in/jan-novak-1/",
         "Source": "Talent chat", "Status": "Unprocessed", "Oblast": ["Software engineering"],
         "Level": ["Mid level"], "Remote?": ["Remote"], "Summary": "Backend dev.", "Score": 71,
         "Company tier": "T2", "Education": "T1", "Fit oblast": ["AI"], "Doporučení": "Potential fit"},
        {"id": "p-old-1", "created_time": "2024-03-01T10:00:00.000Z", "Name": "Old Rejected",
         "E-mail": "old@example.com", "Source": None, "Status": "Rejected"},
        {"id": "p-old-2", "created_time": "2025-01-01T10:00:00.000Z", "Name": "Typeform Era",
         "E-mail": "tf@example.com", "Source": None, "Status": "Contacted"},
    ]
    _write(tmp_path / "hledame_chytre_lidi.jsonl", inbox)
    (tmp_path / "bodies").mkdir()
    (tmp_path / "bodies" / "p-chat-1.json").write_text(json.dumps([
        {"type": "heading_2", "text": "Přepis konverzace"},
        {"type": "paragraph", "text": "Kandidát: Ahoj"},
        {"type": "paragraph", "text": "Miton: Ahoj Jane"},
        {"type": "heading_2", "text": "Evaluace"},
        {"type": "paragraph", "text": "Score 71 · Company T2"},
        {"type": "paragraph", "text": "Silný backend track."},
    ]))
    pool = [
        {"id": "pool-1", "created_time": "2025-06-01T10:00:00.000Z", "Name": "Jan Novak",
         "Contact": None, "Linkedin/CV": "https://linkedin.com/in/JAN-NOVAK-1", "Stage": "Interviewing",
         "Position": ["Backend developer"], "Sourced for": ["Deepscout"], "Interviewed with": ["Michala Gregorová"],
         "Candidate owner": "Miton", "Poznámky": "dobrý", "Notes": "fit bullets", "Alister profil": "https://alisterai.com/profile/42",
         "Placed in newsletter": False},
        {"id": "pool-2", "created_time": "2023-02-01T10:00:00.000Z", "Name": "Eva Malá",
         "Contact": "eva@example.com", "Linkedin/CV": None, "Stage": None, "Status": "Hired",
         "Position": ["CMO"], "Sourced for": ["Bonami"]},
    ]
    _write(tmp_path / "full_databaze_kandidatu.jsonl", pool)
    _write(tmp_path / "searches" / "index.jsonl", [
        {"id": "db-1", "title": "Deepscout - GTM člověk", "path": ["2025", "12/2025"], "rows": 2}])
    _write(tmp_path / "searches" / "db-1.jsonl", [
        {"id": "s-1", "created_time": "2025-12-03T10:00:00.000Z", "Name": "Eva Malá", "Contact": "eva@example.com",
         "Status": "Hired", "Poznámky": "why curated", "Matův koment": "líbí se mi", "Michal": "", "Position": ["CMO"]},
        {"id": "s-2", "created_time": "2025-12-03T10:00:00.000Z", "Name": "Nový Člověk",
         "Linkedin/CV": "https://linkedin.com/in/novy-clovek", "Status": "Rejected"},
    ])
    _write(tmp_path / "outreach_table.jsonl", [
        {"id": "o-1", "created_time": "2026-07-22T18:23:05.000Z", "Name": "Jan Novák",
         "LinkedIn": "https://www.linkedin.com/in/jan-novak-1/", "Channel": "Email", "Mode": "Pitch",
         "Company and role": "Deepscout / GTM", "Sent": "2026-07-23", "Follow-up sent": None,
         "Outreach status": "Replied", "Source": "Sourcing"},
    ])
    return tmp_path


@pytest.fixture()
def session():
    db.reset_for_tests("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(db.engine())
    yield


def test_import_merges_and_maps(export, session):
    mod = _load_importer()
    imp = mod.Importer(export, dry_run=False, skip_rejected=True)
    imp.run()

    with db.session() as s:
        people = {c.full_name: c for c in s.query(Candidate).all()}
        # Jan: inbox row + pool row + outreach row merged on the LinkedIn handle
        jan = store.find_candidate(s, linkedin_url="linkedin.com/in/jan-novak-1")
        assert jan.legacy_ref == "p-chat-1" and jan.source == "talent_chat"
        assert jan.transcript == [{"role": "user", "content": "Ahoj"}, {"role": "assistant", "content": "Ahoj Jane"}]
        assert jan.reasoning == "Score 71 · Company T2\nSilný backend track."
        assert jan.score == 71 and jan.fit_areas == ["AI"] and jan.recommendation == "Potential fit"
        assert jan.stage == "interviewing"                       # pool Stage wins over inbox Unprocessed
        assert jan.positions == ["Backend developer"] and jan.sourced_for == ["Deepscout"]
        assert jan.alister_profile_url == "https://alisterai.com/profile/42"
        assert "fit bullets" in jan.notes and "dobrý" in jan.notes
        types = [e.type for e in jan.events]
        assert types.count("imported") == 1
        assert {"outreach_drafted", "outreach_sent", "outreach_replied"} <= set(types)

        # Eva: pool row + search row merged on e-mail; hired outcome recorded on the search
        eva = store.find_candidate(s, email="eva@example.com")
        assert eva.stage == "hired"
        search = s.query(Search).one()
        assert (search.company_name, search.role) == ("Deepscout", "GTM člověk")
        assert search.opened_at.year == 2025 and search.opened_at.month == 12
        by_name = {l.candidate.full_name: l for l in search.candidates}
        assert by_name["Eva Malá"].outcome == "hired"
        assert by_name["Eva Malá"].miton_comment == "why curated"
        assert by_name["Eva Malá"].founder_comment == "Matův koment: líbí se mi"
        assert by_name["Nový Člověk"].outcome == "rejected"

        # skipped: the rejected 2024 row; kept: the Typeform-era contacted row
        assert "Old Rejected" not in people
        assert people["Typeform Era"].source == "typeform" and people["Typeform Era"].stage == "contacted"
        assert imp.stats["skipped_rejected"] == 1
        assert imp.stats["merged_by_linkedin"] == 2 and imp.stats["merged_by_email"] == 1

    # second run: nothing duplicated
    mod.Importer(export, dry_run=False, skip_rejected=True).run()
    with db.session() as s:
        assert s.query(Candidate).count() == 4
        assert s.query(Search).count() == 1
        jan = store.find_candidate(s, linkedin_url="linkedin.com/in/jan-novak-1")
        assert [e.type for e in jan.events].count("outreach_sent") == 1


def test_dry_run_writes_nothing(export, session):
    mod = _load_importer()
    with pytest.raises(mod._DryRun):
        mod.Importer(export, dry_run=True, skip_rejected=False).run()
    with db.session() as s:
        assert s.query(Candidate).count() == 0
