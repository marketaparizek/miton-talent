"""GDPR erasure: one person, every copy this service can reach.

What ``erase`` removes by itself:

  candidates          the row, with its events and search links (cascade)
  submissions.jsonl   every line with the person's uid or e-mail, if the file
                      exists (it is only written when the database write failed)

What it cannot reach, and therefore lists in MANUAL_STEPS for a person to do:
the Notion page (while the dual-write runs), the e-mail with the CV in the
recruiter inbox, the copy Resend keeps in its sending log, and the database
backups, which age out on their own after RETENTION_DAYS.

Run it from the admin (POST /admin/candidates/erase, admin role only) or from
the shell: python -m talent.admin_cli erase --email someone@example.com
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from talent import store

log = logging.getLogger("miton-talent.erase")

MANUAL_STEPS = [
    "Notion: delete the person's page in 'Hledáme chytré lidi' (search by e-mail) and empty it from Notion's trash.",
    "Recruiter inbox (MAIL_TO): delete the 'Nový kandidát z webu' e-mail with the CV, then empty the trash.",
    "Resend: delete the sent e-mail from the Resend log (dashboard > Emails), if the account keeps logs.",
    "Backups: nothing to do; database dumps age out after RETENTION_DAYS (30). Do not restore one without re-running this erasure.",
]


def scrub_jsonl(path: str, *, uids: set[str], emails: set[str]) -> int:
    """Rewrite ``path`` without the lines that belong to the person. Returns
    how many lines were dropped. A missing file is nothing to do."""
    if not path or not os.path.exists(path):
        return 0
    kept, dropped = [], 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            email = store.normalize_email(((rec.get("contact") or {}).get("email")))
            if rec.get("uid") in uids or (email and email in emails):
                dropped += 1
            else:
                kept.append(line)
    if dropped:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(kept)
        os.replace(tmp, path)
    return dropped


def erase(session, *, uid: Optional[str] = None, email: Optional[str] = None,
          submissions_file: Optional[str] = None, actor: str = "?") -> dict:
    """Erase the person found by ``uid`` or ``email``. The caller commits."""
    if not uid and not store.normalize_email(email):
        raise ValueError("give a uid or an e-mail")
    rows = store.find_for_erasure(session, uid=uid, email=email)
    uids = {r.uid for r in rows} | ({uid} if uid else set())
    emails = {e for e in [store.normalize_email(email)] + [store.normalize_email(r.email) for r in rows] if e}
    for row in rows:
        store.hard_delete(session, row)
    session.flush()
    lines = scrub_jsonl(submissions_file, uids=uids, emails=emails) if submissions_file else 0
    # The log says that an erasure happened and how much, never who.
    log.info("erasure by %s: %d candidate rows, %d backup lines", actor, len(rows), lines)
    return {"candidates": len(rows), "submission_lines": lines, "manual_steps": MANUAL_STEPS}
