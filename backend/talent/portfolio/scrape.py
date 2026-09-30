"""One weekly run.

    from talent.portfolio import scrape
    summary = scrape.run(actor="cron")

Reads every active portfolio company's careers page (concurrently, because the
slow part is the network), then writes the results one company at a time so the
diff is computed inside a single transaction. A company whose page cannot be
read keeps last week's roles; see store.record_company_result.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from talent import db
from talent.models import PortfolioCompany, PortfolioRun
from talent.portfolio import adapters, store

log = logging.getLogger("miton-talent.portfolio.scrape")

MAX_PARALLEL_FETCHES = 6


def run(*, actor: str = "cron", only: Optional[list[str]] = None,
        classify: bool = True, parallel: int = MAX_PARALLEL_FETCHES) -> dict:
    """Scrape the portfolio and return a summary dict (also written to the run row)."""
    with db.session() as session:
        targets = [c for c in store.companies(session) if not only or c.slug in only]
        plan = [
            {
                "id": c.id,
                "slug": c.slug,
                "name": c.name,
                "careers_url": c.careers_url,
                "adapter": c.adapter,
                "config": dict(c.config or {}, company_name=c.name),
            }
            for c in targets
        ]
        run_row = store.start_run(session, actor=actor, companies_total=len(plan))
        run_id = run_row.id
    log.info("portfolio run %s started by %s over %d companies", run_id, actor, len(plan))

    results = _fetch_all(plan, parallel=parallel)

    per_company = []
    for item in plan:
        result = results[item["slug"]]
        # One transaction per company: a break in the middle of the run leaves
        # every company written so far intact, and the run row says how far it got.
        with db.session() as session:
            run_row = session.get(PortfolioRun, run_id)
            company = session.get(PortfolioCompany, item["id"])
            outcome = store.record_company_result(
                session, run_row, company,
                status=result.status,
                rows=result.rows,
                adapter=result.adapter,
                source_url=result.source_url,
                http_status=result.http_status,
                raw_rows=result.raw_rows,
                note=result.note,
                error=result.error,
                duration_ms=result.duration_ms,
                classify_rows=classify,
            )
        outcome["company"] = item["name"]
        per_company.append(outcome)
        log.info("  %-24s %-16s roles=%s new=%s closed=%s %s",
                 item["name"], result.status, outcome["roles"], outcome["new"], outcome["closed"],
                 result.error or "")

    with db.session() as session:
        run_row = session.get(PortfolioRun, run_id)
        failed = sum(1 for r in results.values() if r.status in {"failed", "blocked"})
        store.finish_run(session, run_row, status="done")
        summary = {
            "run_id": run_id,
            "companies": len(plan),
            "companies_failed": failed,
            "roles_total": run_row.roles_total,
            "roles_new": run_row.roles_new,
            "roles_closed": run_row.roles_closed,
            "per_company": per_company,
        }
    log.info("portfolio run %s done: %d roles, +%d new, -%d closed, %d companies unread",
             run_id, summary["roles_total"], summary["roles_new"], summary["roles_closed"], failed)
    return summary


def _fetch_all(plan: list[dict], *, parallel: int) -> dict[str, adapters.Result]:
    def one(item: dict) -> tuple[str, adapters.Result]:
        started = time.monotonic()
        if (item["config"] or {}).get("skip"):
            result = adapters.Result("skipped", adapter="none", note="config: skip")
        else:
            result = adapters.read(item["careers_url"], adapter=item["adapter"], config=item["config"])
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return item["slug"], result

    if parallel <= 1:
        return dict(one(item) for item in plan)
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        return dict(pool.map(one, plan))
