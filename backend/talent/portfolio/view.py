"""The portfolio hiring dashboard: /admin/portfolio.

Three routes, all behind the Alister handoff session like the rest of the back
office (talent/auth.py):

  GET  /admin/portfolio        the page (the artifact's layout, live data)
  GET  /admin/portfolio.json   the same snapshot as JSON, for the Monday digest
  POST /admin/portfolio/scrape run the scrape now, in a background thread

The page is one static file with the snapshot injected, so there is no template
engine and no client-side fetch: what the browser gets is what the database said.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from talent import auth as tauth
from talent import db
from talent.models import PortfolioRun
from talent.portfolio import scrape as scraper
from talent.portfolio import store

log = logging.getLogger("miton-talent.portfolio.view")

router = APIRouter(prefix="/admin/portfolio", tags=["portfolio"], dependencies=[Depends(tauth.require_user)])

# The one route another service may call. Alister's /miton hub draws the
# portfolio in its own design and asks for the data here, with a short-lived
# token signed with the secret the two services already share. Read only, no
# session, no identity: see talent/auth.py verify_service_token.
service_router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@service_router.get("/snapshot")
def service_snapshot(caller: dict = Depends(tauth.require_service)):
    log.info("portfolio snapshot served to service %s", caller.get("service"))
    return JSONResponse(_snapshot(), headers=_NO_STORE)

PAGE = os.path.join(os.path.dirname(__file__), "page.html")
_NO_STORE = {"Cache-Control": "no-store"}

# One scrape at a time per process. A second click while a run is going gets a
# plain "already running" instead of two runs writing the same week.
_lock = threading.Lock()
_running = False


def _snapshot() -> dict:
    with db.session() as session:
        return store.snapshot(session)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def page(user: dict = Depends(tauth.require_user)):
    data = _snapshot()
    with open(PAGE, encoding="utf-8") as fh:
        html = fh.read()
    html = html.replace("__DATA__", _inline(data)).replace(
        "__USER__", _inline({"email": user.get("email"), "role": user.get("role")})
    )
    return HTMLResponse(html, headers=_NO_STORE)


@router.get(".json")
def snapshot_json():
    return JSONResponse(_snapshot(), headers=_NO_STORE)


@router.post("/scrape")
def start_scrape(request: Request, user: dict = Depends(tauth.require_user)):
    """Kick off a run in the background and answer immediately.

    A full run walks 40+ careers pages and asks the model about the ones that
    are not an ATS board, so it takes minutes: far too long for a request.
    """
    global _running
    with _lock:
        if _running:
            return JSONResponse({"started": False, "error": "a scrape is already running"}, status_code=409)
        _running = True

    actor = user.get("email") or "admin"

    with db.session() as session:
        before = session.scalars(
            select(PortfolioRun.id).order_by(PortfolioRun.id.desc()).limit(1)
        ).first()

    def work():
        global _running
        try:
            scraper.run(actor=actor)
        except Exception:
            log.exception("portfolio scrape started by %s failed", actor)
        finally:
            with _lock:
                _running = False

    threading.Thread(target=work, name="portfolio-scrape", daemon=True).start()
    return JSONResponse({"started": True, "run_id": (before or 0) + 1, "actor": actor})


def _inline(data: dict) -> str:
    """JSON safe to drop inside a <script> element."""
    return (json.dumps(data, ensure_ascii=False, default=str)
            .replace("</", "<\\/")
            .replace("<!--", "<\\!--"))
