"""Reading a careers page.

Every adapter answers the same question - "what is open at this company right
now" - and returns the same shape:

    Result(status, rows, adapter, source_url, http_status, raw_rows, note, error)

    status "ok"               the board was read; len(rows) is the truth, 0 included
           "no_careers_page"  there is nothing to read
           "blocked"          the host answered but refused us, or served a shell
           "failed"           fetch or parse broke; the count means nothing

A row is {title, location, team, employment_type, url}. Classification and
collapsing happen later (classify.py, collapse.py), so an adapter only reads.

Most portfolio companies sit on an ATS with a public JSON board, which is exact
and cheap. The rest are ordinary pages, and those go to extract.py, where the
model reads the HTML. Detection is by hostname, and a company's ``adapter``
column overrides it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Callable, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

log = logging.getLogger("miton-talent.portfolio.adapters")

USER_AGENT = "MitonTalentBot/1.0 (+https://talent.miton.cz; portfolio hiring monitor)"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


@dataclass
class Result:
    status: str
    rows: list[dict] = field(default_factory=list)
    adapter: str = ""
    source_url: Optional[str] = None
    http_status: Optional[int] = None
    raw_rows: int = 0
    note: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None


_TAGS_ONLY = re.compile(r"<[^>]+>")

# A board row that is an invitation, not a job. The artifact's rule: an open
# application, a talent pool or a "we are always hiring" blurb is not a role.
_OPEN_APPLICATION = re.compile(
    r"(open|spontaneous|general|speculative)\s+application|reach out anyway|no position|"
    r"talent (pool|community|network)|always (hiring|looking)|"
    r"otevren[aá] (pozice|prihlaska|přihláška)|nenasel|nena[sš]li jste|"
    r"volna pozice pro tebe|napi[sš]te n[aá]m|po[sš]lete n[aá]m sv[eé]|"
    r"nabidni se sam|zaujala te|jin[aá] pozice",
    re.IGNORECASE,
)


def looks_like_open_application(title: str) -> bool:
    from talent.portfolio.collapse import strip_diacritics
    return bool(_OPEN_APPLICATION.search(strip_diacritics(title or "")))


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "cs,en;q=0.9"},
    )


def _row(title, location=None, team=None, employment_type=None, url=None) -> dict:
    return {
        "title": (title or "").strip(),
        "location": (location or "").strip() or None,
        "team": (team or "").strip() or None,
        "employment_type": (employment_type or "").strip() or None,
        "url": (url or "").strip() or None,
    }


def _slug_from_path(url: str, *, index: int = 0) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[index] if len(parts) > index else ""


def _subdomain(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.split(".")[0]


# --- ATS adapters -------------------------------------------------------------


def recruitee(url: str, config: dict) -> Result:
    """<company>.recruitee.com - public offers API, published offers only."""
    sub = config.get("board") or _subdomain(url)
    api = f"https://{sub}.recruitee.com/api/offers/"
    with _client() as c:
        r = c.get(api)
    if r.status_code >= 400:
        return Result("failed", adapter="recruitee", source_url=api, http_status=r.status_code,
                      error=f"HTTP {r.status_code}")
    offers = r.json().get("offers", [])
    rows = [
        _row(o.get("title"),
             o.get("city") or o.get("location") or ("Remote" if o.get("remote") else None),
             o.get("department"),
             o.get("employment_type_code"),
             o.get("careers_url") or o.get("careers_apply_url"))
        for o in offers
        if str(o.get("status", "published")).lower() == "published"
    ]
    return Result("ok", rows, "recruitee", api, r.status_code, raw_rows=len(offers))


def ashby(url: str, config: dict) -> Result:
    """jobs.ashbyhq.com/<board>, and every careers page that proxies one
    (career.rohlik.group). Listed jobs only, which is what a visitor can apply to."""
    host = (urlparse(url).hostname or "")
    if "ashbyhq.com" in host:
        board = config.get("board") or _slug_from_path(url)
    else:
        # A company serving an Ashby board from its own domain
        # (career.rohlik.group): the board name is the company, not "career".
        labels = [l for l in host.split(".") if l not in {"www", "career", "careers", "jobs"}]
        board = config.get("board") or (labels[0] if labels else "")
    api = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    with _client() as c:
        r = c.get(api)
    if r.status_code >= 400:
        return Result("failed", adapter="ashby", source_url=api, http_status=r.status_code,
                      error=f"HTTP {r.status_code}")
    jobs = r.json().get("jobs", [])
    rows = []
    for j in jobs:
        if j.get("isListed") is False:
            continue
        locations = [j.get("location")] + list(j.get("secondaryLocations") or [])
        for loc in locations or [None]:
            name = loc.get("location") if isinstance(loc, dict) else loc
            rows.append(_row(j.get("title"), name, j.get("team") or j.get("department"),
                             j.get("employmentType"), j.get("jobUrl")))
    return Result("ok", rows, "ashby", api, r.status_code, raw_rows=len(jobs))


def smartrecruiters(url: str, config: dict) -> Result:
    """careers.smartrecruiters.com/<ID> - the postings API, paged."""
    board = config.get("board") or _slug_from_path(url)
    api = f"https://api.smartrecruiters.com/v1/companies/{board}/postings"
    rows, offset, total, status = [], 0, None, None
    with _client() as c:
        while True:
            r = c.get(api, params={"limit": 100, "offset": offset})
            status = r.status_code
            if r.status_code >= 400:
                return Result("failed", adapter="smartrecruiters", source_url=api,
                              http_status=r.status_code, error=f"HTTP {r.status_code}")
            data = r.json()
            content = data.get("content", [])
            for p in content:
                loc = p.get("location") or {}
                where = loc.get("city") or loc.get("fullLocation")
                if loc.get("remote"):
                    where = f"{where} / Remote" if where else "Remote"
                rows.append(_row(p.get("name"), where,
                                 (p.get("department") or {}).get("label") or (p.get("function") or {}).get("label"),
                                 (p.get("typeOfEmployment") or {}).get("label"),
                                 f"https://jobs.smartrecruiters.com/{board}/{p.get('id')}"))
            total = data.get("totalFound", len(rows))
            offset += len(content)
            if not content or offset >= (total or 0):
                break
    return Result("ok", rows, "smartrecruiters", api, status, raw_rows=len(rows))


def greenhouse(url: str, config: dict) -> Result:
    board = config.get("board") or _slug_from_path(url, index=1) or _slug_from_path(url)
    api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    with _client() as c:
        r = c.get(api)
    if r.status_code >= 400:
        return Result("failed", adapter="greenhouse", source_url=api, http_status=r.status_code,
                      error=f"HTTP {r.status_code}")
    jobs = r.json().get("jobs", [])
    rows = [_row(j.get("title"), (j.get("location") or {}).get("name"),
                 ", ".join(d.get("name", "") for d in (j.get("departments") or [])) or None,
                 None, j.get("absolute_url")) for j in jobs]
    return Result("ok", rows, "greenhouse", api, r.status_code, raw_rows=len(jobs))


def lever(url: str, config: dict) -> Result:
    board = config.get("board") or _slug_from_path(url)
    api = f"https://api.lever.co/v0/postings/{board}"
    with _client() as c:
        r = c.get(api, params={"mode": "json"})
    if r.status_code >= 400:
        return Result("failed", adapter="lever", source_url=api, http_status=r.status_code,
                      error=f"HTTP {r.status_code}")
    jobs = r.json()
    rows = [_row(j.get("text"), (j.get("categories") or {}).get("location"),
                 (j.get("categories") or {}).get("team"),
                 (j.get("categories") or {}).get("commitment"), j.get("hostedUrl")) for j in jobs]
    return Result("ok", rows, "lever", api, r.status_code, raw_rows=len(jobs))


def workable(url: str, config: dict) -> Result:
    board = config.get("board") or _subdomain(url) or _slug_from_path(url)
    api = f"https://apply.workable.com/api/v1/widget/accounts/{board}"
    with _client() as c:
        r = c.get(api, params={"details": "true"})
    if r.status_code >= 400:
        return Result("failed", adapter="workable", source_url=api, http_status=r.status_code,
                      error=f"HTTP {r.status_code}")
    jobs = r.json().get("jobs", [])
    rows = [_row(j.get("title"), j.get("location") or j.get("city"), j.get("department"),
                 j.get("type"), j.get("url") or j.get("application_url")) for j in jobs]
    return Result("ok", rows, "workable", api, r.status_code, raw_rows=len(jobs))


def personio(url: str, config: dict) -> Result:
    """<company>.jobs.personio.com - the XML feed, when the company publishes one."""
    sub = config.get("board") or _subdomain(url)
    api = f"https://{sub}.jobs.personio.com/xml"
    with _client() as c:
        r = c.get(api)
    if r.status_code >= 400 or not r.text.lstrip().startswith("<"):
        return Result("failed", adapter="personio", source_url=api, http_status=r.status_code,
                      error=f"no XML feed (HTTP {r.status_code})")
    try:
        root = ElementTree.fromstring(r.text)
    except ElementTree.ParseError as exc:
        return Result("failed", adapter="personio", source_url=api, http_status=r.status_code,
                      error=f"XML parse: {exc}")
    rows = []
    positions = root.findall(".//position")
    for p in positions:
        def text(tag):
            el = p.find(tag)
            return (el.text or "").strip() if el is not None and el.text else None
        rows.append(_row(text("name"), text("office"), text("department"),
                         text("employmentType"),
                         f"https://{sub}.jobs.personio.com/job/{text('id')}" if text("id") else None))
    return Result("ok", rows, "personio", api, r.status_code, raw_rows=len(positions))


def recruitis(url: str, config: dict) -> Result:
    """jobs.recruitis.io/<company> - a server-rendered board.

    Every posting is one ``<h3><a href="/<company>/<id>-<slug>">Title</a></h3>``
    followed by a row of chips: place, category, contract. That is enough to read
    exactly, with no model and no JavaScript.
    """
    with _client() as c:
        r = c.get(url)
    if r.status_code >= 400:
        return Result("failed", adapter="recruitis", source_url=url, http_status=r.status_code,
                      error=f"HTTP {r.status_code}")
    company = _slug_from_path(url)
    page = re.sub(r"\s+", " ", r.text)
    blocks = re.split(r'<div class="row job', page)[1:]
    rows = []
    for block in blocks:
        link = re.search(rf'href="(/{re.escape(company)}/(\d+)-[^"]*)"', block)
        title = re.search(r"<h3><a[^>]*>(.*?)</a></h3>", block)
        if not link or not title:
            continue
        chips = [unescape(_TAGS_ONLY.sub("", chip)).replace("\xa0", " ").strip()
                 for chip in re.findall(r'<span class="job-item[^"]*">(.*?)</span>', block)]
        place = chips[0] if chips else None
        team = chips[1] if len(chips) > 1 else None
        contract = chips[2] if len(chips) > 2 else None
        rows.append(_row(unescape(_TAGS_ONLY.sub("", title.group(1))), place, team, contract,
                         f"https://jobs.recruitis.io{link.group(1)}"))
    if not rows and "job" not in page:
        return Result("blocked", adapter="recruitis", source_url=url, http_status=r.status_code,
                      error="board markup not recognised")
    return Result("ok", rows, "recruitis", url, r.status_code, raw_rows=len(blocks))


def startupjobs(url: str, config: dict) -> Result:
    """startupjobs.cz/startup/<company> - the profile page lists its own ads.

    Each ad is an anchor to /nabidka/<id>/<slug> wrapping a card whose text runs
    "company | title | salary | work mode | city | contract". The page renders
    the same card twice (a mobile and a desktop copy), so ads are deduplicated
    by their id.
    """
    with _client() as c:
        r = c.get(url)
    if r.status_code >= 400:
        return Result("failed", adapter="startupjobs", source_url=url, http_status=r.status_code,
                      error=f"HTTP {r.status_code}")
    page = re.sub(r"\s+", " ", r.text)
    cards = list(re.finditer(r'href="(/nabidka/(\d+)/[^"]*)"', page))
    rows, seen = [], set()
    for n, match in enumerate(cards):
        ad_id = match.group(2)
        if ad_id in seen:
            continue
        end = cards[n + 1].start() if n + 1 < len(cards) else match.end() + 2500
        body = page[match.end():end]
        # The match ends inside the opening <a ...> tag, so skip the rest of that
        # tag: its attributes are not card text.
        cut = body.find(">")
        body = body[cut + 1:] if 0 <= cut < 400 else body
        chunks = [unescape(x).strip() for x in _TAGS_ONLY.sub("|", body).split("|")]
        # Drop SVG path data ("M19.25 20.25...") and anything too long to be a
        # card field; what remains is the card's own text, in its own order.
        chunks = [x for x in chunks
                  if x and len(x) < 120 and re.search(r"\w", x)
                  and not re.match(r"^[MmLlCcZzHhVv][\d\.\-\s,]", x)]
        if len(chunks) < 2:
            continue
        seen.add(ad_id)
        contract = next((x for x in reversed(chunks)
                         if re.fullmatch(r"(?i)(full|part)-?time|brigáda|brigada|kontrakt|internship|stáž|staz|ičo|IČO", x)), None)
        place = None
        if contract and contract in chunks:
            before = chunks[:chunks.index(contract)]
            place = next((x for x in reversed(before)
                          if not re.search(r"(?i)hybrid|onsite|on-site|remote|kč|czk|eur|/ měsíc|month", x)), None)
        rows.append(_row(chunks[1], place, None, contract, "https://www.startupjobs.cz" + match.group(1)))
    return Result("ok", rows, "startupjobs", url, r.status_code, raw_rows=len(cards))


def html(url: str, config: dict) -> Result:
    """An ordinary careers page: fetch it and let the model read it (extract.py)."""
    from talent.portfolio import extract

    with _client() as c:
        r = c.get(url)
    if r.status_code in (401, 403, 405, 429):
        return Result("blocked", adapter="html", source_url=url, http_status=r.status_code,
                      error=f"HTTP {r.status_code}")
    if r.status_code >= 400:
        return Result("failed", adapter="html", source_url=url, http_status=r.status_code,
                      error=f"HTTP {r.status_code}")
    text = extract.readable_text(r.text)
    if len(text) < extract.MIN_TEXT_CHARS:
        return Result("blocked", adapter="html", source_url=url, http_status=r.status_code,
                      error="page rendered client-side: no readable text to judge")
    try:
        rows, note = extract.roles_from_text(text, url=url, company=config.get("company_name", ""))
    except extract.ExtractorUnavailable as exc:
        return Result("failed", adapter="html", source_url=url, http_status=r.status_code, error=str(exc))
    return Result("ok", rows, "html", url, r.status_code, raw_rows=len(rows), note=note)


ADAPTERS: dict[str, Callable[[str, dict], Result]] = {
    "recruitee": recruitee,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "greenhouse": greenhouse,
    "lever": lever,
    "workable": workable,
    "personio": personio,
    "recruitis": recruitis,
    "startupjobs": startupjobs,
    "html": html,
}

# Hostname fragment -> adapter. Checked in order.
_BY_HOST: list[tuple[str, str]] = [
    ("recruitee.com", "recruitee"),
    ("ashbyhq.com", "ashby"),
    ("career.rohlik.group", "ashby"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("workable.com", "workable"),
    ("jobs.personio.com", "personio"),
    ("jobs.personio.de", "personio"),
    ("recruitis.io", "recruitis"),
    ("startupjobs.cz", "startupjobs"),
]


def detect(url: str | None) -> str:
    """Which adapter reads this URL. Anything unknown is an ordinary page."""
    host = (urlparse(url or "").hostname or "").lower()
    for fragment, name in _BY_HOST:
        if fragment in host:
            return name
    return "html"


def read(url: str | None, *, adapter: str | None = None, config: dict | None = None) -> Result:
    """Read one careers page, never raising: a break comes back as status failed."""
    config = dict(config or {})
    if not url:
        return Result("no_careers_page", adapter="none",
                      note="no careers page known for this company")
    name = adapter or config.get("adapter") or detect(url)
    fn = ADAPTERS.get(name)
    if fn is None:
        return Result("failed", adapter=name, source_url=url, error=f"unknown adapter {name!r}")
    try:
        result = fn(url, config)
    except Exception as exc:
        log.warning("adapter %s failed on %s: %s", name, url, exc)
        result = Result("failed", adapter=name, source_url=url, error=f"{type(exc).__name__}: {exc}"[:500])

    # An ATS that answers 404 usually means the company left that ATS, or never
    # published a feed there (Personio without the XML export). The page itself
    # is still there, so read it rather than reporting a hole.
    if result.status == "failed" and name != "html" and config.get("fallback_html", True):
        note = f"{name} board unavailable ({result.error}); read the page instead"
        fallback = read(url, adapter="html", config=dict(config, fallback_html=False))
        fallback.note = " · ".join(x for x in (note, fallback.note) if x)
        if fallback.status == "ok" or result.status == "failed":
            return fallback
    # A JSON board that answers with an empty list is a real zero; keep it.
    # An "open application" row is not a role, on any board.
    dropped = [r for r in result.rows if r.get("title") and looks_like_open_application(r["title"])]
    result.rows = [r for r in result.rows
                   if r.get("title") and not looks_like_open_application(r["title"])]
    if dropped:
        skipped = f"{len(dropped)} open-application row(s) not counted: " + \
                  ", ".join(r["title"] for r in dropped[:3])
        result.note = " · ".join(x for x in (result.note, skipped) if x)
    return result
