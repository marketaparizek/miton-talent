"""The fallback reader: a careers page that is not an ATS board.

Roughly half the portfolio publishes roles on its own page ("we are hiring:
Backend Developer, Ostrava"). There is no schema to parse, so the page's
readable text goes to Claude with one instruction: list the roles that are open
right now, invent nothing.

Two rules that keep the counts honest, both learned from the artifact:
  * an open-application form or a "we are always hiring" blurb is not a role;
  * a page that renders client-side gives almost no text, and an empty answer
    from an empty page must be reported as blocked, not as zero. That check
    happens in adapters.html before this module is called.
"""

from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("miton-talent.portfolio.extract")

# Below this many characters of visible text, a page is a shell, not a board.
MIN_TEXT_CHARS = 400
MAX_TEXT_CHARS = 60_000

_SCRIPTS = re.compile(r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")

PROMPT = """You are reading the careers page of {company}, fetched from {url}.

List every job that is OPEN right now, as JSON only:

{{"roles": [{{"title": "...", "location": "...", "team": "...", "employment_type": "...", "url": "..."}}],
  "note": "one short sentence for a recruiter, or empty"}}

Rules:
- Only roles the page presents as currently open. Skip closed, filled, archived
  and "past openings".
- An open-application form, a talent pool, a "we are always hiring" or
  "send us your CV" blurb is NOT a role. Leave it out and say so in note.
- Do not invent a role, a location or a URL. Unknown field: empty string.
- Copy the title as written, in its own language. Do not translate or tidy it.
- One entry per posting as the page lists it. Do not merge or split.
- If the page shows no roles at all, answer {{"roles": [], "note": "..."}} and
  say in note what the page does show (for example "explicitly states no
  current openings", or "links to an external job board").

Page text:
---
{text}
---"""


class ExtractorUnavailable(RuntimeError):
    """No model key, or the model call failed: the page was NOT read."""


def readable_text(raw_html: str) -> str:
    text = _SCRIPTS.sub(" ", raw_html or "")
    text = _TAGS.sub("\n", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    text = _WS.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return _BLANKS.sub("\n\n", text)[:MAX_TEXT_CHARS]


def roles_from_text(text: str, *, url: str, company: str = "") -> tuple[list[dict], str | None]:
    """Ask the model what is open. Returns (rows, note)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ExtractorUnavailable("ANTHROPIC_API_KEY is not set, so plain careers pages cannot be read")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise ExtractorUnavailable(f"anthropic package missing: {exc}") from exc

    prompt = PROMPT.format(company=company or "a Miton portfolio company", url=url, text=text)
    client = anthropic.Anthropic()
    try:
        reply = client.messages.create(
            model=os.environ.get("PORTFOLIO_MODEL") or os.environ.get("MODEL", "claude-sonnet-4-6"),
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        raise ExtractorUnavailable(f"{type(exc).__name__}: {exc}"[:300]) from exc

    answer = "".join(b.text for b in reply.content if getattr(b, "type", "") == "text")
    data = _parse(answer)
    rows = []
    for item in data.get("roles", []):
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        link = (item.get("url") or "").strip()
        rows.append({
            "title": title,
            "location": (item.get("location") or "").strip() or None,
            "team": (item.get("team") or "").strip() or None,
            "employment_type": (item.get("employment_type") or "").strip() or None,
            "url": link if link.startswith("http") else url,
        })
    note = (data.get("note") or "").strip() or None
    return rows, note


def _parse(answer: str) -> dict:
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        raise ExtractorUnavailable("model answer was not JSON")
    try:
        data = json.loads(answer[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ExtractorUnavailable(f"model answer was not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("roles", []), list):
        raise ExtractorUnavailable("model answer had no roles list")
    return data
