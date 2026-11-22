"""Running costs for the admin: /admin/usage.

  GET  /admin/usage          the page, with the numbers injected (no client fetch)
  GET  /admin/usage.json     the same numbers as JSON
  POST /admin/usage/sync     read Anthropic and OpenAI billing now (JSON body {})
  POST /admin/usage/manual   save one month of a hand-entered invoice (JSON)

Admin role only: a miton-role session gets 403, like GDPR erasure.
Two kinds of numbers: every provider's bill (talent/costs.py, provider_costs)
and Talent's own model calls by feature (talent/usage.py, llm_usage).
The POST routes take JSON only, so a plain cross-site form cannot reach them
(the session cookie is SameSite=Lax as well).
"""

from __future__ import annotations

import datetime as dt
import json
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from talent import auth as tauth
from talent import costs, locks, usage

router = APIRouter(prefix="/admin/usage", tags=["usage"], dependencies=[Depends(tauth.require_admin)])

PAGE = os.path.join(os.path.dirname(__file__), "usage_page.html")
_NO_STORE = {"Cache-Control": "no-store"}
SYNC_LOCK = "cost-sync"


def _data() -> dict:
    return {"talent": usage.summary(), "costs": costs.summary()}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def page():
    with open(PAGE, encoding="utf-8") as fh:
        html = fh.read()
    return HTMLResponse(html.replace("__DATA__", _inline(_data())), headers=_NO_STORE)


@router.get(".json")
def as_json():
    return JSONResponse(_data(), headers=_NO_STORE)


def _json_body(request: Request) -> bool:
    return (request.headers.get("content-type") or "").split(";")[0].strip() == "application/json"


@router.post("/sync")
def sync_now(request: Request):
    if not _json_body(request):
        return JSONResponse({"error": "JSON only"}, status_code=415)
    holder = locks.acquire(SYNC_LOCK, dt.timedelta(minutes=5))
    if holder is None:
        return JSONResponse({"error": "načítání už běží"}, status_code=409)
    try:
        return JSONResponse({"result": costs.sync()})
    finally:
        locks.release(SYNC_LOCK, holder)


@router.post("/manual")
async def save_manual(request: Request, user: dict = Depends(tauth.require_admin)):
    if not _json_body(request):
        return JSONResponse({"error": "JSON only"}, status_code=415)
    try:
        body = await request.json()
        out = costs.set_manual(
            str(body.get("provider", "")), str(body.get("month", "")),
            float(body.get("amount", 0)), str(body.get("currency", "USD")).upper(),
            body.get("note"), user.get("email"),
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(out)


def _inline(data: dict) -> str:
    """JSON safe to drop inside a <script> element."""
    return (json.dumps(data, ensure_ascii=False, default=str)
            .replace("</", "<\\/")
            .replace("<!--", "<\\!--"))
