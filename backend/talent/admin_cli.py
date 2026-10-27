"""Operations from the shell, for the things that have no button yet.

    cd backend
    .venv/bin/python -m talent.admin_cli erase --email someone@example.com   # GDPR erasure
    .venv/bin/python -m talent.admin_cli erase --uid 3f2a...                  # by candidate uid
    .venv/bin/python -m talent.admin_cli revoke-sessions kolega@miton.cz      # cut back-office sessions
    .venv/bin/python -m talent.admin_cli purge-chats                          # drop abandoned chats now

Reads DATABASE_URL like the app (source the .env first on the server).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from talent import auth, conversations, db, erase


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m talent.admin_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("erase", help="erase one person everywhere this service can reach")
    who = e.add_mutually_exclusive_group(required=True)
    who.add_argument("--uid")
    who.add_argument("--email")
    e.add_argument("--submissions-file", default=os.environ.get("SUBMISSIONS_FILE", "submissions.jsonl"))
    e.add_argument("--yes", action="store_true", help="do it; without this only counts")

    r = sub.add_parser("revoke-sessions", help="end every back-office session of an address")
    r.add_argument("email")

    sub.add_parser("purge-chats", help="delete chats older than CHAT_RETENTION_DAYS that were never submitted")

    args = ap.parse_args(argv)

    if args.cmd == "erase":
        with db.session() as s:
            found = erase.store.find_for_erasure(s, uid=args.uid, email=args.email)
            if not args.yes:
                print(f"{len(found)} candidate row(s) match. Re-run with --yes to erase.")
                return 0
            out = erase.erase(s, uid=args.uid, email=args.email,
                              submissions_file=args.submissions_file, actor="cli")
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    if args.cmd == "revoke-sessions":
        print(f"{auth.revoke_sessions_for(args.email)} session(s) revoked")
        return 0

    if args.cmd == "purge-chats":
        with db.session() as s:
            print(f"{conversations.purge_stale(s)} conversation(s) deleted")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
