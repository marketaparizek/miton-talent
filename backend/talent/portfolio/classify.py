"""Which function is this role, is it technical, and in which country.

Rules first, because they are free, reproducible and cover most titles. The
model is asked only about what the rules cannot place, and only in one batched
call per run (see ask_model). Every role records which of the two decided it
(``fn_source``), so a wrong bucket can be found and fixed.

The buckets and the definition of "technical" are the artifact's:
technical = Engineering & AI, Data & Analytics, Product & Design.
"""

from __future__ import annotations

import json
import logging
import os
import re

from talent.portfolio.collapse import strip_diacritics

log = logging.getLogger("miton-talent.portfolio.classify")

FUNCTIONS = [
    "Engineering & AI",
    "Data & Analytics",
    "Product & Design",
    "Ops & Logistics",
    "Sales & BD",
    "Marketing & Commercial",
    "Finance & Legal",
    "People & Support",
    "Clinical",
    "Other",
]
TECHNICAL = {"Engineering & AI", "Data & Analytics", "Product & Design"}

# Ordered: the first bucket whose pattern matches wins, so the narrow buckets
# (data, product) come before the broad ones (engineering).
_RULES: list[tuple[str, str]] = [
    ("Clinical", r"\b(psychiatr|psycholog|lekar|doktor|klinick|clinical|therapist|terapeut|nurse|sestra|pacient)"),
    ("Data & Analytics", r"\b(data (scientist|engineer|analyst|analytics)|analytics engineer|bi |business intelligence|datov|analytik|analyst)"),
    ("Product & Design", r"\b(product (manager|owner|designer|lead|director)|produktov|ux|ui|graphic|designer|design lead|brand designer)"),
    ("Engineering & AI", r"\b(engineer|developer|programator|vyvojar|devops|sre|qa|tester|architect|cto|machine learning|ml |ai |research scientist|quant|infrastructure|security|backend|frontend|fullstack|full.stack|mobile|ios|android|robot|it specialist|sysadmin|administrator systemu|technical (lead|domain))"),
    ("Ops & Logistics", r"\b(operations|operacn|logistic|supply chain|warehouse|sklad|fulfillment|crossdock|last mile|area manager|shift|smenov|courier|kuryr|dispatcher|procurement|technik|driver|ridic|expedice|provoz)"),
    ("Sales & BD", r"\b(sales|account (manager|executive)|business development|obchodn|partnership|customer success|expansion|key account|revenue|bdr|sdr)"),
    ("Marketing & Commercial", r"\b(marketing|brand|content|social|seo|ppc|performance|copywriter|communication|pr |growth|category manager|kategorie|buyer|nakupc|einkauf|warengruppen|merchandis|e-shop|eshop|commercial|campaign)"),
    ("Finance & Legal", r"\b(finance|financn|accountant|ucetn|controller|controlling|tax|dane|legal|counsel|pravn|compliance|risk|audit|payroll|fp&a|treasury|invoic)"),
    ("People & Support", r"\b(hr |hr$|human resources|people|recruit|talent|nabor|personalist|office manager|assistant|asistent|support|customer (care|service|support)|zakaznick|helpdesk|training|trainer|skolitel|community|administrat)"),
]
_COMPILED = [(fn, re.compile(pattern, re.IGNORECASE)) for fn, pattern in _RULES]

# Location text -> country. Checked in order; the first hit wins.
_COUNTRY_RULES: list[tuple[str, str]] = [
    ("CZ", r"\b(prague|praha|prag|brno|ostrava|plzen|pilsen|budejovice|liberec|olomouc|hradec|pardubice|zlin|jihlava|opatovice|chrastany|liboc|hostivar|jenec|karlin|holesovice|podebrad|czech|cesk|cr)"),
    ("SK", r"\b(bratislava|kosice|zilina|nitra|slovak|slovensk)"),
    ("PL", r"\b(warsaw|warszawa|marki|krakow|cracow|wroclaw|poznan|gdansk|poland|polsk)"),
    ("DE", r"\b(berlin|munich|munchen|hamburg|frankfurt|cologne|koln|garching|bischofsheim|schonefeld|germany|deutschland|german)"),
    ("AT", r"\b(vienna|wien|graz|linz|salzburg|austria|osterreich)"),
    ("HU", r"\b(budapest|debrecen|hungary|magyar)"),
    ("RO", r"\b(bucharest|bucuresti|cluj|romania)"),
    ("IE", r"\b(dublin|ireland)"),
    ("AE", r"\b(dubai|abu dhabi|uae|emirates)"),
    ("GB", r"\b(london|manchester|united kingdom|uk)"),
    ("US", r"\b(new york|nyc|san francisco|bay area|boston|austin|usa|united states|us\b)"),
    ("Remote", r"\b(remote|anywhere|distributed|home office)"),
    ("Europe", r"\b(europe|eu\b|emea)"),
]
_COMPILED_COUNTRY = [(cc, re.compile(pattern, re.IGNORECASE)) for cc, pattern in _COUNTRY_RULES]

COUNTRY_NAMES = {
    "CZ": "Czechia", "SK": "Slovakia", "PL": "Poland", "DE": "Germany", "AT": "Austria",
    "HU": "Hungary", "RO": "Romania", "IE": "Ireland", "AE": "UAE", "GB": "United Kingdom",
    "US": "United States", "Remote": "Remote", "Europe": "Europe, remote", "Other": "Other",
}


def is_technical(fn: str | None) -> bool:
    return fn in TECHNICAL


def country_of(location: str | None, *, fallback: str | None = None) -> str:
    text = strip_diacritics(location or "").lower()
    for cc, pattern in _COMPILED_COUNTRY:
        if pattern.search(text):
            return cc
    return fallback or "Other"


def function_by_rules(title: str, team: str | None = None) -> str | None:
    """The bucket the keywords give, or None when nothing matches."""
    haystack = strip_diacritics(f"{title or ''} {team or ''}").lower()
    # A padded copy lets the patterns use \b on the first and last word too.
    haystack = f" {haystack} "
    for fn, pattern in _COMPILED:
        if pattern.search(haystack):
            return fn
    return None


def classify_rows(rows: list[dict], *, company: str = "", use_model: bool | None = None) -> list[dict]:
    """Fill fn / fn_source / is_technical / cc on every row, in place.

    Rows that already carry an ``fn`` (the seed, a manual fix) are left alone.
    """
    unknown: list[dict] = []
    for row in rows:
        if not row.get("cc"):
            row["cc"] = country_of(row.get("location"))
        if row.get("fn"):
            row.setdefault("fn_source", "seed")
        else:
            fn = function_by_rules(row.get("title", ""), row.get("team"))
            if fn:
                row["fn"], row["fn_source"] = fn, "rules"
            else:
                unknown.append(row)
        row["is_technical"] = is_technical(row.get("fn"))

    if unknown and (use_model if use_model is not None else model_enabled()):
        try:
            answers = ask_model(unknown, company=company)
        except Exception:
            log.exception("function classification by model failed; leaving %d rows as Other", len(unknown))
            answers = {}
        for row in unknown:
            fn = answers.get(row.get("title", ""))
            if fn in FUNCTIONS:
                row["fn"], row["fn_source"] = fn, "llm"
            else:
                row["fn"], row["fn_source"] = "Other", "rules"
            row["is_technical"] = is_technical(row["fn"])
    else:
        for row in unknown:
            row["fn"], row["fn_source"] = "Other", "rules"
            row["is_technical"] = False
    return rows


def model_enabled() -> bool:
    if os.environ.get("PORTFOLIO_CLASSIFY_LLM", "").strip() in {"0", "false", "no"}:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def ask_model(rows: list[dict], *, company: str = "") -> dict[str, str]:
    """One call for every title the rules could not place. Returns {title: bucket}."""
    import anthropic

    titles = [r.get("title", "") for r in rows if r.get("title")]
    if not titles:
        return {}
    prompt = (
        "Sort each job title into exactly one bucket. Answer with JSON only: "
        'an object mapping the title verbatim to its bucket.\n\n'
        f"Buckets: {', '.join(FUNCTIONS)}\n"
        f"Company: {company or 'unknown'}\n\n"
        "Titles:\n" + "\n".join(f"- {t}" for t in titles)
    )
    client = anthropic.Anthropic()
    reply = client.messages.create(
        model=os.environ.get("PORTFOLIO_MODEL") or os.environ.get("MODEL", "claude-sonnet-4-6"),
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in reply.content if getattr(block, "type", "") == "text")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    data = json.loads(text[start:end + 1])
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
