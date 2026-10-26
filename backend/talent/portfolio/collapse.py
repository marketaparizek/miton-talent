"""One requisition, one row.

A careers board repeats the same job once per city and once per language. The
artifact this replaces collapsed those by hand ("Rohlik's 57 is a requisition
count; its raw board shows about 80 rows"). This module does it in code.

The fingerprint is the title with everything that varies between copies of the
same requisition removed: diacritics, case, gender markers ((m/w/d), /ka, (m/ž)),
seniority brackets kept (a junior and a senior role are two requisitions),
punctuation, and a trailing city or language marker. Two rows with the same
fingerprint at the same company are one role; their locations are merged.

Cross-language copies of one requisition ("Warengruppenmanager" vs the Czech
title) do NOT collapse here, because nothing in the strings says they are the
same job. For those, a company's config carries an explicit
``{"aliases": {"<from fingerprint>": "<to fingerprint>"}}`` map.
"""

from __future__ import annotations

import re
import unicodedata

# Gender and duplicate markers that differ between copies of one requisition.
_NOISE = [
    r"\(m/w/d\)", r"\(m/f/d\)", r"\(w/m/d\)", r"\(m/w/x\)", r"\(m/ž\)", r"\(m/z\)",
    r"\(f/m\)", r"\(m/f\)", r"\(all genders\)", r"\(any gender\)",
    r"\bm/w/d\b", r"\bm/f/d\b",
]
# "Developer/ka", "koordinátor*ka", "Lekar*ka", "obchodník/ice"
_GENDER_SUFFIX = re.compile(r"(?<=\w)[\*/](?:ka|ku|ky|ice|in|ová|ova|a)\b")
_BRACKETS = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
# A bracket is dropped only when everything inside it is noise. "(medior/senior)"
# and "(m/w/d)" go; "Lead (Inbound)" keeps its bracket, because Inbound is the
# only thing that tells it apart from "Lead (Outbound)".
_BRACKET_NOISE = re.compile(
    r"^[\s/|,&+-]*(?:(?:m|f|w|d|x|z|ž|any|all|genders?|junior|jr|medior|mid|senior|sr|lead|"
    r"intern|internship|staz|stáž|trainee|graduate|part|full|time|hpp|dpp|ico|remote|hybrid|"
    r"on-?site|cz|sk|en|de|hu|pl|prague|praha|brno|ostrava|remote-first)[\s/|,&+-]*)+$",
    re.IGNORECASE,
)
_NONWORD = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")

# Words that carry no meaning for identity and appear inconsistently.
_STOP = {
    "the", "a", "an", "and", "or", "for", "pro", "na", "do", "v", "ve", "se", "s",
    "fulltime", "full", "time", "parttime", "part", "hpp", "dpp",
    "hledame", "hledam", "wanted", "job", "jobs", "position", "role", "vacancy",
    "m", "f", "d", "w", "x", "ka", "ky",
}


def strip_diacritics(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalise_title(title: str) -> str:
    """Human-readable normalisation, used for display and for the fingerprint."""
    t = (title or "").strip()
    t = t.replace("–", "-").replace("—", "-").replace(" ", " ")
    return _WS.sub(" ", t)


def fingerprint(title: str) -> str:
    """The requisition key. Stable, lossy, comparable inside one company."""
    t = normalise_title(title).lower()
    t = strip_diacritics(t)
    for pattern in _NOISE:
        t = re.sub(pattern, " ", t, flags=re.IGNORECASE)
    t = _GENDER_SUFFIX.sub("", t)
    t = _BRACKETS.sub(lambda m: " " if _BRACKET_NOISE.match(m.group(1)) else f" {m.group(1)} ", t)
    # What follows a dash is kept. "Konzultant prodeje - Jeneč" and the same
    # title with "- Poděbradská" are two shops hiring two people, not one
    # requisition posted twice, and only the title says so. A board that repeats
    # one requisition per city repeats the title unchanged, and those collapse on
    # the title alone.
    words = [w for w in _NONWORD.sub(" ", t).split() if w and w not in _STOP]
    return " ".join(words)[:255] or normalise_title(title).lower()[:255]


def apply_aliases(fp: str, aliases: dict | None) -> str:
    """Follow a company's manual merge map, at most a few hops, never in a loop."""
    seen = set()
    while aliases and fp in aliases and fp not in seen:
        seen.add(fp)
        fp = aliases[fp]
    return fp


def collapse(rows: list[dict], aliases: dict | None = None) -> list[dict]:
    """Merge rows that are the same requisition.

    Two rows with the same board id (``external_id``) are one posting, whatever
    their titles. Without an id, two rows are one requisition when the title
    normalises to the same fingerprint AND they come from the same posting URL. That second half is
    what keeps an honest count: a board that repeats one requisition per city
    repeats the same URL (and an adapter that expands secondary locations emits
    the same URL too), while two shops hiring the same job under the same title
    have two postings and stay two roles. An explicit alias merges across URLs,
    because that is exactly what an alias is for.

    The first row's title, team, type and url survive (the board's own order is
    the company's order) and every location is merged into ``locations``.
    """
    out: dict[str, dict] = {}
    for row in rows:
        title = normalise_title(row.get("title") or "")
        if not title:
            continue
        raw_fp = fingerprint(title)
        fp = apply_aliases(raw_fp, aliases)
        # A fingerprint that either follows an alias or is one's destination is
        # merged across postings; nothing else is.
        alias_members = set(aliases or {}) | set((aliases or {}).values())
        aliased = fp in alias_members or raw_fp in alias_members
        if aliased:
            key = fp
        elif row.get("external_id"):
            key = f"id\x00{row['external_id']}"
        else:
            key = f"{fp}\x00{row.get('url') or ''}"
        loc = (row.get("location") or "").strip()
        if is_channel(loc):
            loc = ""
        existing = out.get(key)
        if existing is None:
            merged = dict(row)
            merged["title"] = title
            merged["fingerprint"] = fp
            merged["locations"] = [loc] if loc else []
            merged["aliased"] = aliased
            merged["external_ids"] = [row["external_id"]] if row.get("external_id") else []
            out[key] = merged
            continue
        if loc and loc not in existing["locations"]:
            existing["locations"].append(loc)
        if row.get("external_id") and row["external_id"] not in existing["external_ids"]:
            existing["external_ids"].append(row["external_id"])
        for field_name in ("team", "employment_type", "url", "fn", "cc"):
            if not existing.get(field_name) and row.get(field_name):
                existing[field_name] = row[field_name]
        # The requisition is as old as its oldest posting.
        if row.get("posted_at") and (not existing.get("posted_at") or row["posted_at"] < existing["posted_at"]):
            existing["posted_at"] = row["posted_at"]
    for row in out.values():
        row["location"] = " / ".join(row["locations"]) if row["locations"] else None

    # Two postings that survived as separate roles under one title (an Account
    # Executive in Dublin and one in New York) need two different fingerprints,
    # because the fingerprint is what identifies a role from week to week. The
    # place tells them apart and is as stable as the posting itself.
    rows_out = list(out.values())
    by_fp: dict[str, list[dict]] = {}
    for row in rows_out:
        by_fp.setdefault(row["fingerprint"], []).append(row)
    for fp, group in by_fp.items():
        if len(group) < 2:
            continue
        used: set[str] = set()
        for row in group:
            mark = row["locations"][0] if row["locations"] else (row.get("url") or "")
            mark = _NONWORD.sub("-", strip_diacritics(str(mark)).lower()).strip("-")
            candidate = f"{fp}#{mark}"[:255]
            # Two postings under one title in one place (two shifts at one
            # warehouse) still need two fingerprints; the board id, or failing
            # that the order on the board, tells them apart.
            if candidate in used:
                extra = _NONWORD.sub("-", str(row.get("external_id") or row.get("url") or len(used))).strip("-")
                candidate = f"{fp}#{mark}#{extra}"[:255]
            used.add(candidate)
            row["fingerprint"] = candidate
    return rows_out


# Ashby lets a company list "where else this posting is advertised" as extra
# locations. They are distribution channels, not places, and must not split or
# label a role.
_CHANNELS = re.compile(r"^(group career pages?|jobs\.cz|ejobs\.ro|profesia\.sk|linkedin|startupjobs(\.cz)?)$",
                       re.IGNORECASE)


def is_channel(location: str | None) -> bool:
    return bool(_CHANNELS.match((location or "").strip()))


def _plain(text: str) -> str:
    return strip_diacritics(text or "").lower()


def group_requisitions(rows: list[dict], rules: dict) -> list[dict]:
    """Postings to requisitions, for a company whose board lists one job many times.

    Rohlik posts one requisition once per country board, once per language and
    once per contract (a full-time and a 20-hour twin), so its board count is a
    posting count. With ``config.requisitions`` set, postings collapse further:

    * ``same_job``: ``[[regex, label], ...]`` matched against the plain title.
      Titles that match one pattern are one job whatever their language
      ("Customer Care Specialist (German)", "... s němčinou", the part-time
      twin). Anything that matches no pattern is the job its fingerprint names.
    * ``sites``: names of places that hire separately (warehouses). Two postings
      of one job at two sites are two requisitions; at one site, or at no site
      (an office role posted on the Prague, Munich and Vienna boards), one.

    Each requisition keeps every posting's id in ``external_ids`` and the number
    of postings in ``postings``, so the count says how it was reached.
    """
    patterns = [(re.compile(p, re.IGNORECASE), label) for p, label in (rules.get("same_job") or [])]
    sites = [(s, _plain(s)) for s in (rules.get("sites") or [])]

    def job_of(row: dict) -> str:
        title = _plain(row.get("title"))
        for pattern, label in patterns:
            if pattern.search(title):
                return label
        return row["fingerprint"].split("#", 1)[0]

    def sites_of(row: dict) -> tuple:
        places = _plain(" | ".join(row.get("locations") or [row.get("location") or ""]))
        return tuple(sorted({name for name, plain in sites if plain in places}))

    out: dict[tuple, dict] = {}
    for row in rows:
        key = (job_of(row), sites_of(row))
        ids = list(row.get("external_ids") or ([row["external_id"]] if row.get("external_id") else []))
        existing = out.get(key)
        if existing is None:
            merged = dict(row)
            merged["external_ids"] = ids
            merged["postings"] = 1
            merged["requisition"] = True
            merged["fingerprint"] = key[0] + (("#" + "+".join(_NONWORD.sub("-", _plain(s)).strip("-")
                                                              for s in key[1])) if key[1] else "")
            merged["locations"] = list(row.get("locations") or [])
            out[key] = merged
            continue
        existing["postings"] += 1
        for i in ids:
            if i not in existing["external_ids"]:
                existing["external_ids"].append(i)
        for loc in row.get("locations") or []:
            if loc not in existing["locations"]:
                existing["locations"].append(loc)
        for field_name in ("team", "employment_type", "url", "fn", "cc"):
            if not existing.get(field_name) and row.get(field_name):
                existing[field_name] = row[field_name]
        # The requisition is as old as its oldest posting.
        if row.get("posted_at") and (not existing.get("posted_at") or row["posted_at"] < existing["posted_at"]):
            existing["posted_at"] = row["posted_at"]
    for row in out.values():
        row["location"] = " / ".join(row["locations"]) if row["locations"] else None
        row["fingerprint"] = row["fingerprint"][:255]
    return list(out.values())
