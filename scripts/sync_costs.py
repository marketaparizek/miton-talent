#!/usr/bin/env python
"""Read what Anthropic and OpenAI billed into provider_costs (talent/costs.py).

    # every morning from cron (deploy/setup_server.sh installs it)
    backend/.venv/bin/python scripts/sync_costs.py

    # once after the keys go in: everything since the start of the year
    backend/.venv/bin/python scripts/sync_costs.py --since 2026-01-01

Needs ANTHROPIC_ADMIN_KEY and/or OPENAI_ADMIN_KEY in .env; a provider without
its key is skipped. Exit code 1 when a provider with a key failed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from talent import costs  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync provider bills into provider_costs")
    ap.add_argument("--days", type=int, default=35, help="how many days back to re-read (default 35)")
    ap.add_argument("--since", help="YYYY-MM-DD: re-read everything from this day instead")
    ap.add_argument("--only", choices=sorted(costs.FETCHERS), help="one provider")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    since = dt.date.fromisoformat(args.since) if args.since else None
    result = costs.sync(args.days, since=since, providers=(args.only,) if args.only else None)
    print(json.dumps(result, ensure_ascii=False))
    failed = [k for k, v in result.items() if not v["ok"] and v.get("error") != "chybí klíč"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
