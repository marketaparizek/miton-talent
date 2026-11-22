"""Token spend for the admin: /admin/usage.

  GET /admin/usage        the page, with the numbers injected (no client fetch)
  GET /admin/usage.json   the same numbers as JSON

Admin role only: a miton-role session gets 403, like GDPR erasure.
The numbers come from llm_usage (talent/usage.py).
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from talent import auth as tauth
from talent import usage

router = APIRouter(prefix="/admin/usage", tags=["usage"], dependencies=[Depends(tauth.require_admin)])

PAGE = os.path.join(os.path.dirname(__file__), "usage_page.html")
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def page():
    with open(PAGE, encoding="utf-8") as fh:
        html = fh.read()
    return HTMLResponse(html.replace("__DATA__", _inline(usage.summary())), headers=_NO_STORE)


@router.get(".json")
def as_json():
    return JSONResponse(usage.summary(), headers=_NO_STORE)


def _inline(data: dict) -> str:
    """JSON safe to drop inside a <script> element."""
    return (json.dumps(data, ensure_ascii=False, default=str)
            .replace("</", "<\\/")
            .replace("<!--", "<\\!--"))
