"""Controlled vocabularies for Miton Talent.

Enforced in code (store.py, the admin, the MCP tools), not by the database, so
adding a value is a one-line change here plus nothing else. The chat-facing
lists (AREA, LEVEL, WORK_MODE, SEARCH_STATUS) must stay identical to the
ALLOWED_* constants in app.py: the model's record_reply output is validated
against them. The other lists came verbatim from the Notion databases they
replace, so the import maps one to one.
"""

SOURCES = ["talent_chat", "typeform", "startupjobs", "sourcing", "referral", "alister_search", "newsletter"]

# Candidate.stage. Verbatim from the Notion "Full databáze kandidátů" Stage
# property, lower-cased. The old inbox Status maps onto it:
#   Unprocessed -> applied, Contacted -> contacted,
#   Placed in database -> sourced, Rejected -> rejected.
STAGES = [
    "applied",
    "sourced",
    "contacted",
    "up_for_a_call",
    "interviewing",
    "offer",
    "hired",
    "rejected",
    "rejected_after_interview",
    "not_interested",
]
STAGE_LABELS = {
    "applied": "Applied",
    "sourced": "Sourced",
    "contacted": "Contacted",
    "up_for_a_call": "Up for a call",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "hired": "Hired",
    "rejected": "Rejected",
    "rejected_after_interview": "Rejected after interview",
    "not_interested": "Not interested",
}
OPEN_STAGES = ["applied", "sourced", "contacted", "up_for_a_call", "interviewing", "offer"]

# SearchCandidate.outcome. Verbatim from the per-search Notion tables' Status.
OUTCOMES = [
    "sourced",
    "contacted",
    "interviewing",
    "hired",
    "hired_with_miton_lead",
    "rejected",
    "konzultant",
    "newsletter_potential",
    "placed_in_newsletter",
]

FOUNDER_RATINGS = ["liked", "disliked", "maybe"]

OWNERS = ["Miton", "Miton C"]

# CandidateEvent.type
EVENT_TYPES = [
    "submitted",          # chat submission arrived (data: lang, source)
    "imported",           # row created by the Notion import (data: notion url)
    "scored",             # automatic scoring finished (data: score, recommendation)
    "stage_changed",      # data: from, to
    "note",               # data: text
    "outreach_drafted",   # data: channel, mode, company_role, subject
    "outreach_sent",      # data: channel, company_role
    "follow_up_sent",     # data: channel
    "outreach_replied",   # data: channel, sentiment
    "intro_booked",       # data: company_role, with
    "shared_with_founder",  # data: search_id
    "call",               # data: with, when
]

OUTREACH_CHANNELS = ["email", "linkedin"]
OUTREACH_MODES = ["pitch", "referral_ask"]
FOLLOW_UP_AFTER_DAYS = 4

# Chat-facing vocabularies: MUST match ALLOWED_* in app.py exactly.
AREA = ["Marketing", "Software engineering", "Accounting & Finance", "People & HR",
        "Sales & Business Development", "Data Science", "Product", "Administration", "Operations", "Other"]
LEVEL = ["Intern role", "Junior role", "Mid level", "Specialist role", "C-level management", "Founder/co-founder"]
WORK_MODE = ["Remote", "Hybrid", "On-site"]
SEARCH_STATUS = ["Aktivně hledám", "Pasivně sleduji možnosti na trhu", "Právě nehledám",
                 "Actively seeking a new role", "Just passively interested in market opportunities",
                 "Not seeking a new role"]

COMPANY_TIERS = ["T1", "T2", "T3", "T4"]
EDUCATION_TIERS = ["T1", "T2", "T3"]
FIT_AREAS = ["AI", "Krypto", "E-commerce", "Gastrotech", "Mental health", "Miton interní"]
RECOMMENDATIONS = ["Potential fit", "K rozhodnutí", "Low fit", "Call", "Poslat founderovi", "Template reply"]


def clean_list(values, allowed) -> list:
    """Keep only allowed strings, in the order given, without duplicates."""
    out = []
    for v in values or []:
        if isinstance(v, str) and v in allowed and v not in out:
            out.append(v)
    return out
