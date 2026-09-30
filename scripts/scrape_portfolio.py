#!/usr/bin/env python
"""The weekly portfolio scrape, as a command.

    # every Monday from cron (deploy/setup_server.sh installs it)
    backend/.venv/bin/python scripts/scrape_portfolio.py

    # useful while working on it
    scripts/scrape_portfolio.py --seed                  # fill the portfolio + the baseline, no scrape
    scripts/scrape_portfolio.py --only rohlik-group,aim # one or two companies
    scripts/scrape_portfolio.py --dry-run --only aim    # read the pages, write nothing
    scripts/scrape_portfolio.py --no-llm                # ATS boards only, no model calls

Exit code 0 when the run finished, 1 when it could not run at all. Companies
whose page could not be read do not fail the run: they keep last week's roles
and are listed in the summary and on the page.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from talent import db                                    # noqa: E402
from talent.portfolio import adapters, scrape, seed, store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape open roles across the Miton portfolio")
    ap.add_argument("--seed", action="store_true",
                    help="upsert the portfolio list (and the baseline, once), then stop unless --run is given")
    ap.add_argument("--run", action="store_true", help="with --seed: scrape as well")
    ap.add_argument("--only", help="comma-separated company slugs")
    ap.add_argument("--actor", default="cron", help="who is running this (goes on the run row)")
    ap.add_argument("--dry-run", action="store_true", help="read every page, write nothing")
    ap.add_argument("--no-llm", action="store_true", help="skip the model: ATS boards only")
    ap.add_argument("--parallel", type=int, default=scrape.MAX_PARALLEL_FETCHES)
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.no_llm:
        os.environ["PORTFOLIO_CLASSIFY_LLM"] = "0"
        os.environ.pop("ANTHROPIC_API_KEY", None)

    only = [s.strip() for s in args.only.split(",")] if args.only else None

    if args.seed:
        with db.session() as session:
            print(json.dumps(seed.seed(session), ensure_ascii=False))
        # Seeding runs on every deploy; scraping does not, because it walks 40+
        # sites and calls the model. Ask for it explicitly.
        if not (args.run or args.dry_run):
            return 0

    if args.dry_run:
        with db.session() as session:
            targets = [c for c in store.companies(session) if not only or c.slug in only]
            plan = [(c.name, c.careers_url, c.adapter, dict(c.config or {}, company_name=c.name)) for c in targets]
        for name, url, adapter, config in plan:
            result = adapters.read(url, adapter=adapter, config=config)
            print(f"{name:26} {result.status:16} {len(result.rows):3} rows  {result.adapter:15} {result.error or ''}")
            for row in result.rows:
                print(f"    - {row['title']}  [{row.get('location') or '-'}]")
        return 0

    summary = scrape.run(actor=args.actor, only=only, parallel=args.parallel)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print(f"run #{summary['run_id']}: {summary['roles_total']} open roles, "
              f"+{summary['roles_new']} new, -{summary['roles_closed']} closed, "
              f"{summary['companies_failed']} of {summary['companies']} companies unread")
    return 0


if __name__ == "__main__":
    sys.exit(main())
