"""Which role is which, from one week to the next.

A title is a poor identity. Rohlik renames a dozen postings in a normal week
("Flow Lead" becomes "Flow Lead - Romanian Speaking"), and matching on the title
turns every rename into one false closure and one false opening. Most boards
give each posting a stable id, so the id is the identity wherever there is one.

Three tiers, best first, and the tier is stored with the role so a week's diff
can say how sure it is:

    ats    ``ats:<namespace>:<id>``  an id from an ATS API, or one embedded in
                                     the posting URL (/nabidka/<id>, /v/<id> ...)
    url    ``url:<normalised url>``  the posting URL, only when it is specific
                                     to this one role on this run
    heur   ``heur:<fingerprint>``    the normalised title (collapse.fingerprint)

``alias`` is a fourth, manual kind: rows a company's config merges on purpose
are keyed by the merged fingerprint, because no single id stands for them.
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

# Ids that a posting URL carries, by host fragment. The first group is the id.
# Order matters only where two patterns could match the same URL.
_URL_IDS: list[tuple[str, str, re.Pattern]] = [
    ("startupjobs", "startupjobs.cz", re.compile(r"/nabidka/(\d+)")),
    ("recruitis", "recruitis.io", re.compile(r"/[^/]+/(\d+)-")),
    ("peopleforce", "peopleforce.io", re.compile(r"/v/(\d+)")),
    ("smartrecruiters", "smartrecruiters.com", re.compile(r"/[^/]+/(\d{6,})")),
    ("personio", "personio.", re.compile(r"/job/(\d+)")),
    ("jobscz", "jobs.cz", re.compile(r"/rpd/(\d+)")),
    ("greenhouse", "greenhouse.io", re.compile(r"/jobs/(\d+)")),
    ("lever", "lever.co", re.compile(r"/([0-9a-f]{8}-[0-9a-f-]{27})")),
    ("ashby", "ashbyhq.com", re.compile(r"/([0-9a-f]{8}-[0-9a-f-]{27})")),
    # A careers site serving an Ashby board from its own domain:
    # career.rohlik.group/<board>/jobs/<uuid>
    ("ashby", "", re.compile(r"/jobs/([0-9a-f]{8}-[0-9a-f-]{27})")),
    ("recruitee", "recruitee.com", re.compile(r"/o/([^/?#]+)")),
]


def id_from_url(url: str | None) -> str | None:
    """``<namespace>:<id>`` when the URL names one posting, else None."""
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    for namespace, fragment, pattern in _URL_IDS:
        if fragment and fragment not in host:
            continue
        match = pattern.search(parsed.path)
        if match:
            return f"{namespace}:{match.group(1).lower()}"
    return None


def normalise_url(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def assign_keys(rows: list[dict], *, careers_url: str | None = None) -> list[dict]:
    """Give each collapsed row a ``role_key`` and a ``key_tier``.

    A URL is an identity only when no other row on this run shares it and it is
    not the careers page itself: Aim points four roles at one Typeform, and a
    page the model read without links hands back its own address for every row.
    """
    page = normalise_url(careers_url)
    url_counts = Counter(normalise_url(r.get("url")) for r in rows)
    for row in rows:
        if row.get("aliased"):
            row["role_key"], row["key_tier"] = f"alias:{row['fingerprint']}", "alias"
            continue
        ext = row.get("external_id") or id_from_url(row.get("url"))
        if ext:
            row["external_id"] = ext
            row["role_key"], row["key_tier"] = f"ats:{ext}", "ats"
            continue
        norm = normalise_url(row.get("url"))
        if norm and norm != page and url_counts[norm] == 1:
            row["role_key"], row["key_tier"] = f"url:{norm}", "url"
            continue
        row["role_key"], row["key_tier"] = f"heur:{row['fingerprint']}", "heur"
    return rows
