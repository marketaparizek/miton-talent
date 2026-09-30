"""
Miton talent chat - backend.

A small server that:
  POST /chat    takes the conversation, calls Claude, returns reply + extracted profile
  POST /submit  writes the submitted contact as a row in Notion, emails the CV, backs it up to a file
  GET  /health  checks that the server is running

Run:
  export ANTHROPIC_API_KEY=sk-ant-...      (Windows: set ANTHROPIC_API_KEY=...)
  export NOTION_TOKEN=ntn_...              (token from the Miton Notion integration)
  pip install -r requirements.txt
  uvicorn app:app --reload --port 8000

NOTION_DATABASE_ID is pre-filled to the "Hledame chytre lidi" database.
Without NOTION_TOKEN, contacts are stored only in submissions.jsonl.
The CV file is kept in memory only. It is emailed as an attachment and never written to disk.
"""

import io
import os
import ssl
import json
import time
import base64
import uuid
import logging
import smtplib
import threading
import datetime
from email.message import EmailMessage
from typing import List, Optional

import httpx
from fastapi import Depends, FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from anthropic import Anthropic

from talent import auth as tauth
from talent import db as tdb
from talent import store as tstore
from talent.portfolio import view as portfolio_view

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

# Log failures loudly (Notion, SMTP, model). Never log personal data or message contents.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("miton-talent")

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
SUBMISSIONS_FILE = "submissions.jsonl"

# Automatic candidate scoring after submit (best-effort, never blocks the save).
# Turn on with SCORING_ENABLED=1 once the evaluation prompt is finalised.
SCORING_ENABLED = os.environ.get("SCORING_ENABLED", "0") == "1"
SCORE_MODEL = os.environ.get("SCORE_MODEL", MODEL)  # can point to a cheaper/faster model
SCORE_MAX_TOKENS = 1500       # room for the structured verdict + reasoning
SCORE_CV_CHARS = 14000        # cap CV text fed to the model
SCORE_TIMEOUT = 45            # seconds for the scoring model call

# Limits to keep token usage low
MAX_TOKENS = 800        # the record_reply tool call must fit reply + summary + profile
SOFT_CLOSE_TURNS = int(os.environ.get("SOFT_CLOSE_TURNS", "5"))  # from this user turn on, the model is told to wrap up now
MAX_USER_TURNS = 12     # after this many user messages we close the conversation
MAX_MSG_CHARS = 2000    # trim a single overly long message
MAX_HISTORY = 20        # send the model only the last part of the history
MAX_CV_BYTES = 8 * 1024 * 1024  # reject CV attachments larger than 8 MB

# Rate limit per visitor IP, so nobody can spam the chat (burns Claude tokens) or the
# form. Counted in memory per process. Tune via env without touching code. A normal
# conversation is capped at MAX_USER_TURNS messages, so these leave room for a couple
# of chats. Behind Render/Railway the real visitor IP comes from X-Forwarded-For.
CHAT_RATE_LIMIT = int(os.environ.get("CHAT_RATE_LIMIT", "30"))      # messages
CHAT_RATE_WINDOW = int(os.environ.get("CHAT_RATE_WINDOW", "600"))   # per this many seconds (10 min)
SUBMIT_RATE_LIMIT = int(os.environ.get("SUBMIT_RATE_LIMIT", "5"))   # submissions
SUBMIT_RATE_WINDOW = int(os.environ.get("SUBMIT_RATE_WINDOW", "600"))

_hits = {}               # (endpoint, ip) -> list of recent timestamps
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    # Behind Render/Railway the platform proxy APPENDS the real visitor IP to
    # X-Forwarded-For, so the LAST entry is trustworthy. Earlier entries can be
    # spoofed by the client and must not be used for rate limiting.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(endpoint: str, ip: str, limit: int, window: int) -> bool:
    now = time.time()
    key = (endpoint, ip)
    with _hits_lock:
        # occasional sweep so stale IPs do not accumulate in memory forever
        if len(_hits) > 500:
            cutoff = now - max(CHAT_RATE_WINDOW, SUBMIT_RATE_WINDOW)
            for k in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
                del _hits[k]
        stamps = [t for t in _hits.get(key, []) if now - t < window]
        if len(stamps) >= limit:
            _hits[key] = stamps
            return True
        stamps.append(now)
        _hits[key] = stamps
        return False


RATE_MSG = {
    "cs": "Ještě tu jsi? Zpráv bylo teď hodně, dej tomu prosím chvilku a napiš mi za pár minut. Díky za pochopení.",
    "en": "Still here? That was a lot of messages, please give it a moment and write to me again in a few minutes. Thanks for understanding.",
}

CLOSING = {
    "cs": "Díky moc za pokec. Nech mi prosím kontakt (e-mail, klidně i LinkedIn nebo životopis). Rozhodíme sítě napříč naším portfoliem, jestli je něco, co by ti mohlo sedět, a spojíme se s tebou. A kdyby cokoliv, napiš naší kolegyni Markétě Pařízek na marketa.parizek@miton.cz.",
    "en": "Thanks for the chat. Please leave me your contact (email, and feel free to add LinkedIn or a CV). We will cast the net across our portfolio to see if there is something that could fit you, and we will get in touch. And if you need anything, write to our colleague Markéta Pařízek at marketa.parizek@miton.cz.",
}

ERR_MSG = {
    "cs": "Promiň, něco se mi teď pokazilo. Zkus to prosím za chvilku znovu.",
    "en": "Sorry, something went wrong on my side. Please try again in a moment.",
}

# Appended to the system prompt once the conversation reaches SOFT_CLOSE_TURNS user
# messages: a hard instruction to wrap up now, so chats never drag on regardless of
# how curious the model feels.
WRAP_UP = {
    "cs": "\n\nDŮLEŽITÉ: Rozhovor už je dost dlouhý. V TÉTO odpovědi už nepokládej žádnou další otázku. Shrň, co o člověku víš, vyzvi ho, ať nechá kontakt, a nastav stage na \"collect_contact\".",
    "en": "\n\nIMPORTANT: The conversation is already long enough. Do NOT ask any further question in THIS reply. Summarize what you know about the person, invite them to leave their contact, and set stage to \"collect_contact\".",
}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _hdr(value: str) -> str:
    # Strip CR/LF so user-supplied values can never inject extra email headers.
    return (value or "").replace("\r", " ").replace("\n", " ").strip()

client = Anthropic()  # reads the key from ANTHROPIC_API_KEY

# --- CORS -------------------------------------------------------------------
# Restrict to the Miton domains. Override with ALLOWED_ORIGINS (comma-separated) if needed.
_DEFAULT_ORIGINS = "https://www.miton.cz,https://miton.cz"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()]

# --- Notion -----------------------------------------------------------------
# A dedicated integration for the Miton website (separate from Alister).
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "1ba6065a-c67d-4ba6-97f5-1a6c9662d137")
NOTION_VERSION = "2022-06-28"

# --- Email (CV delivery) ----------------------------------------------------
# Gmail SMTP by default. Use a Gmail App Password, not the account password.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
MAIL_TO = os.environ.get("MAIL_TO", "marketa.parizek@miton.cz")

# Some PaaS providers (Railway trial among them) block outbound SMTP ports entirely.
# When the configured secret is a Resend API key (re_...), deliver over Resend's
# HTTPS API on port 443 instead of SMTP. RESEND_API_KEY can also be set explicitly.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "") or (SMTP_PASS if SMTP_PASS.startswith("re_") else "")

# Allowed values so we match existing Notion options and never create new ones.
# These strings must match the Notion schema exactly, including diacritics.
ALLOWED_AREA = ["Marketing", "Software engineering", "Accounting & Finance", "People & HR",
                "Sales & Business Development", "Data Science", "Product", "Administration", "Operations", "Other"]
ALLOWED_LEVEL = ["Intern role", "Junior role", "Mid level", "Specialist role", "C-level management", "Founder/co-founder"]
ALLOWED_MODE = ["Remote", "Hybrid", "On-site"]
ALLOWED_STATUS = ["Aktivně hledám", "Pasivně sleduji možnosti na trhu", "Právě nehledám",
                  "Actively seeking a new role", "Just passively interested in market opportunities", "Not seeking a new role"]

# Scoring option sets - must match the Notion select/multi_select options exactly.
ALLOWED_COMPANY_TIER = ["T1", "T2", "T3", "T4"]
ALLOWED_EDUCATION = ["T1", "T2", "T3"]
ALLOWED_FIT = ["AI", "Krypto", "E-commerce", "Gastrotech", "Mental health", "Miton interní"]
ALLOWED_DOPORUCENI = ["Potential fit", "K rozhodnutí", "Low fit"]

# Score weights (tune here). Adapted from the github-sourcing career-quality method:
# company + education tiers are the backbone, plus a conversation track-record signal.
COMPANY_TIER_POINTS = {"T1": 1.0, "T2": 0.65, "T3": 0.35, "T4": 0.10}
EDUCATION_TIER_POINTS = {"T1": 1.0, "T2": 0.50, "T3": 0.20}
SCORE_W_COMPANY = 0.45
SCORE_W_EDUCATION = 0.20
SCORE_W_TRACK = 0.35
# Score -> recommendation bands. Narrow middle ("K rozhodnutí") = only genuine
# borderline cases need a human decision; everyone else is a clear fit or a pass.
REC_POTENTIAL_MIN = 65   # >= this -> Potential fit
REC_DECIDE_MIN = 53      # >= this (and < POTENTIAL) -> K rozhodnutí; below -> Weak profile

PORTFOLIO_CS = (
    "AI a augmentovaná práce: Equilibre, DeepScout, Pangea AI, Whisper. "
    "Krypto a web3: Coinmate, Confirmo, Firefish, Marinade. "
    "E-commerce: Rohlík, Bonami, Glami, Biano, Displate. "
    "Gastrotech: Qerko, Grason, Septim, Savarin. "
    "Psychedelika a duševní zdraví: Psyon, Wavepaths. "
    "Cestování: Boataround, Freeway Camper, AmperGuru, DoKempu. "
    "HR tech: StartupJobs. Real estate tech: UlovDomov, Reas."
)
PORTFOLIO_EN = (
    "AI and augmented workforce: Equilibre, DeepScout, Pangea AI, Whisper. "
    "Crypto and web3: Coinmate, Confirmo, Firefish, Marinade. "
    "E-commerce: Rohlik, Bonami, Glami, Biano, Displate. "
    "Gastrotech: Qerko, Grason, Septim, Savarin. "
    "Psychedelics and mental health: Psyon, Wavepaths. "
    "Travel: Boataround, Freeway Camper, AmperGuru, DoKempu. "
    "HR tech: StartupJobs. Real estate tech: UlovDomov, Reas."
)

SYSTEM = {
    "cs": f"""Jsi zvědavý a přátelský průvodce náborem investiční skupiny Miton. Bavíš se s lidmi, kteří zvažují práci ve startupu. Vedeš přirozený, lidský rozhovor — spíš jako zvědavý člověk u kávy než dotazník. Nejvíc tě zajímá ten člověk sám.

Ptej se otevřeně a navazuj na to, co říká. Zajímá tě hlavně:
- co ho na práci nejvíc baví a nabíjí, čemu by se chtěl věnovat,
- co považuje za svůj největší úspěch nebo na čem odvedl práci, které si sám cení,
- jak by sám sebe popsal, v čem je silný,
- jak si představuje ideální roli a formu práce (remote, hybrid, on-site),
- jestli zrovna aktivně hledá, jen sleduje trh, nebo je v pohodě tam, kde je.

Ptáš se vždy jen na jednu věc a reaguješ na to, co člověk řekl. Šetři otázkami: celý rozhovor má mít zhruba 3 až 4 tvoje otázky, tak ať se každá počítá. K jednomu tématu polož nanejvýš jednu doplňující otázku a nerozpitvávej technické detaily (nepotřebuješ vědět přesnou technologii ani celou historii). Neptej se na nic, co nepatří do profilu nebo shrnutí, třeba na bydliště nebo věk. Na úroveň role (junior, senior apod.) se přímo neptej; tu si domyslíš z toho, co člověk řekne a z jeho životopisu. Kde to sedne, můžeš mimochodem zmínit jednu konkrétní firmu z portfolia. Firmy nevyjmenovávej naráz.

Mluvíš česky, krátce a lidsky, bez marketingových frází. Nepoužívej dlouhé pomlčky (— ani –); místo nich piš běžnou interpunkci, tedy čárky, tečky nebo dvojtečky. Bavíš se jen o kariéře, rolích a firmách z portfolia Mitonu. Když se někdo zeptá na něco mimo téma, jednou větou ho mile vrať zpátky a dál se tím nezabývej. Když má někdo technický problém nebo chce mluvit s živým člověkem, odkaž ho na Markétu Pařízek, marketa.parizek@miton.cz.

Portfolio podle oblastí: {PORTFOLIO_CS}

Nezdržuj člověka. Jakmile znáš oblast, formu práce a stav hledání a máš aspoň jednu věc, kterou umí nebo na kterou je hrdý, NEBO jakmile dá najevo, že už řekl vše nebo je připravený to posunout dál, okamžitě přestaň s otázkami a přejdi k závěru: osobně a konkrétně shrň, co tě na něm zaujalo, vyzvi ho, ať ti nechá kontakt (a klidně i životopis), a vysvětli, co bude dál: rozhodíme sítě napříč naším portfoliem, jestli je něco, co by mu mohlo sedět, a spojíme se s ním. Na úplný závěr dodej větu ve stylu „A kdyby cokoliv, napiš naší kolegyni Markétě Pařízek na marketa.parizek@miton.cz." (drž se tykání a vyhni se rodově zabarveným tvarům). V tu chvíli nastav stage na "collect_contact".

Svou odpověď vždy vrať zavoláním nástroje record_reply: do pole "reply" napiš text pro člověka, do "profile" strukturovaný profil, do "summary" průběžné shrnutí a do "stage" fázi ("exploring", nebo "collect_contact").
Hodnoty v profile používej PŘESNĚ z těchto možností:
- area (jedna nebo více): Marketing, Software engineering, Accounting & Finance, People & HR, Sales & Business Development, Data Science, Product, Administration, Operations, Other
- level (jedna): Intern role, Junior role, Mid level, Specialist role, C-level management, Founder/co-founder
- workMode (jedna): Remote, Hybrid, On-site
- status (jedna): Aktivně hledám, Pasivně sleduji možnosti na trhu, Právě nehledám
V profile pokaždé vrať svůj aktuální nejlepší odhad VŠECH polí, která už znáš — jednou zjištěné hodnoty (oblast, forma, stav) v KAŽDÉ odpovědi zopakuj a oprav je, kdykoli je člověk upřesní (např. změň „Aktivně hledám" na „Pasivně sleduji možnosti na trhu", když řekne, že jen sleduje trh). Vynech jen to, co ještě opravdu nevíš (level klidně vynech, dokud o něm nepadla řeč). Do "summary" průběžně piš stručné shrnutí kandidáta ve 2 až 4 větách (silné stránky, největší úspěchy, co hledá, odhad seniority) — věcně, ve třetí osobě, jako poznámku pro recruitera; v každé odpovědi ho aktualizuj podle toho, co nového zaznělo. Dokud nevíš skoro nic, nech "summary" prázdné. stage = "collect_contact" jakmile má smysl posbírat kontakt, jinak "exploring".""",
    "en": f"""You are a curious, friendly recruiting guide for the investment group Miton. You talk with people considering a startup job. You lead a natural, human conversation — more like a curious person over coffee than a questionnaire. What interests you most is the person themselves.

Ask open questions and follow up on what they say. You mainly care about:
- what they most enjoy and find energizing in work, what they'd like to focus on,
- what they consider their biggest achievement, or work they're genuinely proud of,
- how they'd describe themselves, where their strengths lie,
- how they picture their ideal role and work mode (remote, hybrid, on-site),
- whether they're actively looking, just watching the market, or happy where they are.

Ask only one thing at a time and react to what the person said. Be economical with questions: the whole conversation should have roughly 3 to 4 of your questions, so make each one count. Ask at most one follow-up per topic and do not dig into technical detail (you do not need the exact technology or the full story). Never ask about things that do not belong in the profile or summary, such as where they live or their age. Don't ask directly about seniority (junior, senior, etc.); infer it from what they say and from their CV. Where it fits, you may casually mention one specific portfolio company. Do not list companies all at once.

You speak English, briefly and naturally, no marketing cliches. Do not use em or en dashes (— or –); use normal punctuation instead, i.e. commas, periods or colons. You only discuss careers, roles, and Miton's portfolio companies. If someone asks something off-topic, steer them back in one sentence and do not engage further. If someone has a technical problem or wants to talk to a human, point them to Marketa Parizek, marketa.parizek@miton.cz.

Portfolio by area: {PORTFOLIO_EN}

Don't keep the person long. As soon as you know their area, work mode and job-search status and have at least one thing they are good at or proud of, OR as soon as they signal they've said everything or are ready to move on, immediately stop asking questions and wrap up: give a personal, specific summary of what stood out, invite them to leave their contact (and their CV if they like), and explain what happens next: we will cast the net across our portfolio to see if there is something that could fit them, and we will get in touch. At the very end add that if they need anything, they can write to our colleague Markéta Pařízek at marketa.parizek@miton.cz. At that point set stage to "collect_contact".

Always return your answer by calling the record_reply tool: put the text for the person in "reply", the structured profile in "profile", the running summary in "summary" and the phase in "stage" ("exploring" or "collect_contact").
Use values in profile EXACTLY from these options:
- area (one or more): Marketing, Software engineering, Accounting & Finance, People & HR, Sales & Business Development, Data Science, Product, Administration, Operations, Other
- level (one): Intern role, Junior role, Mid level, Specialist role, C-level management, Founder/co-founder
- workMode (one): Remote, Hybrid, On-site
- status (one): Actively seeking a new role, Just passively interested in market opportunities, Not seeking a new role
In profile, every turn return your current best guess for ALL fields you already know — repeat known values (area, work mode, status) in EVERY reply and correct them whenever the person clarifies (e.g. change "Actively seeking a new role" to "Just passively interested in market opportunities" once they say they're only watching the market). Only omit what you genuinely do not know yet (feel free to omit level until it comes up). In "summary" keep a running 2 to 4 sentence summary of the candidate (strengths, biggest achievements, what they seek, inferred seniority) — factual, third person, as a note for the recruiter; update it each reply as new things come up. While you still know almost nothing, leave "summary" empty. stage = "collect_contact" once it makes sense to collect contact, otherwise "exploring".""",
}


# Structured output via a forced tool call. This makes the model's reply arrive as a
# validated object, so a stray quote in the reply text can never break JSON parsing.
REPLY_TOOL = {
    "name": "record_reply",
    "description": "Return the reply for the person together with the extracted profile, running summary and conversation stage.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "Text odpovědi zobrazený člověku."},
            "profile": {
                "type": "object",
                "properties": {
                    "area": {"type": "array", "items": {"type": "string", "enum": ALLOWED_AREA}},
                    "level": {"type": "string", "enum": ALLOWED_LEVEL},
                    "workMode": {"type": "string", "enum": ALLOWED_MODE},
                    "status": {"type": "string", "enum": ALLOWED_STATUS},
                },
            },
            "summary": {"type": "string", "description": "Průběžné stručné shrnutí kandidáta pro recruitera."},
            "stage": {"type": "string", "enum": ["exploring", "collect_contact"]},
        },
        "required": ["reply", "stage"],
    },
}


# --- Candidate scoring ------------------------------------------------------
# Forced tool call, same pattern as record_reply: the verdict arrives as a
# validated object with values constrained to the exact Notion option strings.
SCORE_TOOL = {
    "name": "record_score",
    "description": "Classify a candidate. The numeric score and recommendation are computed from these fields, so focus on accurate tiers and scorecard ratings.",
    "input_schema": {
        "type": "object",
        "properties": {
            "company_tier": {"type": "string", "enum": ALLOWED_COMPANY_TIER,
                             "description": "Tier of the companies the candidate worked at or built. T4 if none/weak."},
            "education": {"type": "string", "enum": ALLOWED_EDUCATION,
                          "description": "Tier of the candidate's education. T3 if weak/unclear/none."},
            "ownership": {"type": "integer", "minimum": 0, "maximum": 3,
                          "description": "Real owned outcomes (built/led/shipped/decided), not just participation. 0-3."},
            "impact": {"type": "integer", "minimum": 0, "maximum": 3,
                       "description": "Concrete achievements with scale/results (numbers, growth, launches). 0-3."},
            "depth": {"type": "integer", "minimum": 0, "maximum": 3,
                      "description": "Depth in their field and level of responsibility/seniority. 0-3."},
            "fit_oblast": {"type": "array", "items": {"type": "string", "enum": ALLOWED_FIT},
                           "description": "Which portfolio areas the candidate genuinely fits (can be empty)."},
            "reasoning": {"type": "string",
                          "description": "Short scorecard for the recruiter: one to two sentence snapshot, then the per-signal notes and why the tiers. Plain text, concise, no filler."},
        },
        "required": ["company_tier", "education", "ownership", "impact", "depth"],
    },
}

# NOTE: interim wording is fine to tweak; the weights live in the constants above and
# the final score/recommendation are computed in code from the fields below.
SCORE_SYSTEM = """You are an evaluator for the investment group Miton. You classify a candidate for fit with Miton's startup portfolio so a recruiter can triage them. You receive the chat profile, the running summary, the chat transcript, and (when available) the text of the candidate's CV. The CV or chat may be in Czech or English; handle either. This works for ALL roles (marketing, finance, ops, product, engineering, etc.), not only technical ones.

Assign these, weighting recent and notable experience most:

Company tier - quality and prestige of the companies the candidate worked at or built:
- T1: top global innovators, well-known scaleups or unicorns, big tech, AI-frontier labs, or Czech companies with real global reach (the calibre of Rohlik, Productboard, Mews, SentinelOne). A founder of a real, funded, notable company is also T1.
- T2: established and respected companies, strong enterprises, solid scaleups, recognisable brands.
- T3: average or smaller local firms, an unremarkable employer, or unclear.
- T4: weak or none - no meaningful company history, very junior.

Education tier:
- T1: a top university (roughly QS top 100, or a top, highly relevant Czech programme).
- T2: a solid university degree, average to good school.
- T3: weak, unclear, or no higher education.

Scorecard, each 0 to 3 (from CV and chat):
- ownership: real owned outcomes (built, led, shipped, decided), not just participation.
- impact: concrete achievements with scale or results (numbers, growth, launches).
- depth: depth in their field and level of responsibility/seniority.

fit_oblast: which portfolio areas genuinely fit (AI, Krypto, E-commerce, Gastrotech, Mental health, Miton interní). Pick only real fits, can be empty.

reasoning: a short scorecard for the recruiter. A one to two sentence snapshot, then the per-signal notes and one line on why the tiers. Plain text, concise, specific, no filler.

Be honest and evidence-based. If the CV is missing, judge from the chat alone and lean conservative. Always return your verdict by calling the record_score tool. The final numeric score and next-step recommendation are computed from your fields, so do not invent a number - just classify accurately."""


class Msg(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    messages: List[Msg]
    lang: str = "cs"


class CVIn(BaseModel):
    name: str = ""
    type: str = ""
    data: str = ""  # base64, kept in memory only, never written to disk


class SubmitIn(BaseModel):
    lang: str = "cs"
    profile: dict = {}
    contact: dict = {}
    summary: str = ""
    consent: bool = False
    consent_text: str = ""
    cv: Optional[CVIn] = None
    messages: List[Msg] = []  # chat transcript, stored with the candidate row


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# --- Sign-in (Alister handoff) -----------------------------------------------
# The back office is for Miton people only. There is no login here: Alister
# signs people in and hands them over with a one-minute signed token; see
# talent/auth.py. The chat widget routes below stay public on purpose.
app.include_router(tauth.router)

# The portfolio hiring dashboard (/admin/portfolio). Its own router, behind the
# same session as the rest of the back office; the weekly scrape that feeds it
# runs from cron (scripts/scrape_portfolio.py), not from a request.
app.include_router(portfolio_view.router)


@app.exception_handler(tauth.LoginRequired)
async def _login_required(request: Request, exc: tauth.LoginRequired):
    return tauth.login_required_response(request)


@app.middleware("http")
async def frame_ancestors_header(request: Request, call_next):
    # The embed page may only be iframed from the Miton domains (and same origin).
    # Prevents anyone else from embedding the chat on their site and burning tokens.
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' " + " ".join(ALLOWED_ORIGINS)
    return response


@app.get("/health")
def health():
    return {"ok": True}


def _db_ok() -> bool:
    try:
        from sqlalchemy import text
        with tdb.engine().connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:
        log.exception("database check failed")
        return False


@app.get("/diag", dependencies=[Depends(tauth.require_user)])
def diag():
    """Config self-check for operations. Reports only booleans and status codes,
    never secret values, but it does say which integrations are wired and how
    they fail, so it is for signed-in Miton people only."""
    out = {
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "model": MODEL,
        "scoring_enabled": SCORING_ENABLED,
        "score_model": SCORE_MODEL,
        "notion_token_set": bool(NOTION_TOKEN),
        "database": tdb.engine().url.drivername,
        "database_ok": _db_ok(),
        "smtp_pass_set": bool(SMTP_PASS),
        "smtp_user_set": bool(SMTP_USER),
        "mail_from": bool(MAIL_FROM),
        "mail_to": bool(MAIL_TO),
    }
    if NOTION_TOKEN:
        try:
            r = httpx.get(
                f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}",
                headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NOTION_VERSION},
                timeout=10,
            )
            out["notion_api_status"] = r.status_code
            if r.status_code == 401:
                out["notion_hint"] = "token je neplatny (preklep / stary klic)"
            elif r.status_code == 404:
                out["notion_hint"] = "token plati, ale integrace neni pripojena k databazi (Notion: ... > Connections) nebo je spatne NOTION_DATABASE_ID"
            elif r.status_code < 300:
                out["notion_hint"] = "ok"
            else:
                out["notion_hint"] = "necekany stav"
        except Exception as e:
            out["notion_api_status"] = "error"
            out["notion_hint"] = type(e).__name__
    out["resend_key_set"] = bool(RESEND_API_KEY)
    if RESEND_API_KEY:
        try:
            r = httpx.get(
                "https://api.resend.com/domains",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                timeout=10,
            )
            out["resend_api_status"] = r.status_code
            # A "sending access" key is rejected on /domains with 401 although sending works;
            # only /emails proves it, so 401 here is "klic je send-only nebo neplatny".
            out["resend_hint"] = ("ok" if r.status_code < 300
                                  else "klic je send-only (v poradku) nebo neplatny; overi az prvni odeslany e-mail" if r.status_code == 401
                                  else "necekany stav")
        except Exception as e:
            out["resend_api_status"] = "error"
            out["resend_hint"] = type(e).__name__
    elif SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.starttls(context=ctx)
                server.login(SMTP_USER, SMTP_PASS)
            out["smtp_login"] = "ok"
        except Exception as e:
            out["smtp_login"] = f"failed: {type(e).__name__}"
    # Live Anthropic key check: a tiny 1-token call surfaces auth/model problems
    # that anthropic_key_set (non-empty only) cannot see. Never echoes the key.
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"model": MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
                timeout=15,
            )
            out["anthropic_api_status"] = r.status_code
            if r.status_code == 401:
                out["anthropic_hint"] = "klic je neplatny (zneplatneny / stary / preklep)"
            elif r.status_code == 404:
                out["anthropic_hint"] = f"model '{MODEL}' neexistuje nebo neni dostupny"
            elif r.status_code < 300:
                out["anthropic_hint"] = "ok"
            else:
                try:
                    out["anthropic_hint"] = r.json().get("error", {}).get("message", "necekany stav")
                except Exception:
                    out["anthropic_hint"] = "necekany stav"
        except Exception as e:
            out["anthropic_api_status"] = "error"
            out["anthropic_hint"] = type(e).__name__
    return out


# --- Back office ------------------------------------------------------------------
# The landing page after the Alister handoff. The real admin (inbox, candidate,
# search, share pages) grows from here; every route under /admin depends on
# tauth.require_user the same way.


@app.get("/admin", response_class=HTMLResponse)
def admin_home(user: dict = Depends(tauth.require_user)):
    email = _hdr(user["email"]).replace("<", "&lt;")
    return HTMLResponse(
        "<!doctype html><html lang='cs'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Miton Talent</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:48px 24px;color:#1b1b1b;background:#f6f3ee}"
        "main{max-width:640px;margin:0 auto}h1{font-size:28px;margin:0 0 8px}p{margin:0 0 16px;color:#555}"
        "a{color:#1b1b1b}</style></head><body><main>"
        "<h1>Miton Talent</h1>"
        f"<p>Přihlášen(a) jako <strong>{email}</strong> přes Alister.</p>"
        "<p>Back office (inbox, kandidáti, searche) vzniká tady. Zatím: "
        "<a href='/admin/portfolio'>portfolio hiring</a> · "
        "<a href='/diag'>diagnostika</a> · <a href='/auth/logout'>odhlásit</a></p>"
        "</main></body></html>",
        headers={"Cache-Control": "no-store"},
    )


# --- Iframe embed -------------------------------------------------------------
# The backend serves the widget page itself, so the website only needs ONE URL:
#   <iframe src="https://YOUR-BACKEND-URL" ...></iframe>
# The page calls /chat and /submit same-origin, so no CORS is involved.

# Revalidate on every load so a new deploy reaches visitors immediately instead of
# being masked by a stale browser cache. ETag/Last-Modified still allow cheap 304s.
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
def embed_page():
    page = os.path.join(STATIC_DIR, "embed.html")
    if os.path.exists(page):
        return FileResponse(page, media_type="text/html", headers=_NO_CACHE)
    return JSONResponse({"ok": True, "note": "embed page not found, API only"})


@app.get("/embed-test")
def embed_test():
    # Live demo of the auto-resize embed (?resize=1 + postMessage listener),
    # same listener snippet the website uses. No secrets, safe to expose.
    page = os.path.join(STATIC_DIR, "embed-test.html")
    if os.path.exists(page):
        return FileResponse(page, media_type="text/html", headers=_NO_CACHE)
    raise HTTPException(status_code=404)


@app.get("/widget.js")
def widget_js():
    bundle = os.path.join(STATIC_DIR, "miton-talent-chat.js")
    if os.path.exists(bundle):
        return FileResponse(bundle, media_type="application/javascript", headers=_NO_CACHE)
    return JSONResponse({"error": "widget bundle not found"}, status_code=404)


@app.get("/fonts/{name}")
def font_file(name: str):
    # Serve the Miton brand font (DegularDisplay) next to the widget, because the
    # site's font files have no CORS headers and cannot be loaded cross-origin.
    if "/" in name or ".." in name or not name.endswith(".woff2"):
        raise HTTPException(status_code=404)
    path = os.path.join(STATIC_DIR, "fonts", name)
    if os.path.exists(path):
        return FileResponse(path, media_type="font/woff2")
    raise HTTPException(status_code=404)


def _parse_model_json(raw: str, lang: str) -> dict:
    """Parse the model's JSON reply. Robust to the model wrapping the JSON in prose:
    if the whole string is not valid JSON, fall back to the outermost { ... } block."""
    clean = raw.replace("```json", "").replace("```", "").strip()

    candidates = [clean]
    start, end = clean.find("{"), clean.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(clean[start:end + 1])

    for c in candidates:
        try:
            obj = json.loads(c)
        except Exception:
            continue
        if isinstance(obj, dict) and "reply" in obj:
            return obj

    fallback = "Promiň, zkus to prosím znovu." if lang == "cs" else "Sorry, please try again."
    return {"reply": clean or fallback, "profile": {}, "summary": "", "stage": "exploring"}


@app.post("/chat")
def chat(body: ChatIn, request: Request):
    lang = "en" if body.lang == "en" else "cs"

    # per-IP rate limit: if exceeded, answer politely without calling the model
    if _rate_limited("chat", _client_ip(request), CHAT_RATE_LIMIT, CHAT_RATE_WINDOW):
        return {"reply": RATE_MSG[lang], "profile": {}, "summary": "", "stage": "exploring"}

    # keep only valid roles and trim overly long messages so input tokens do not grow
    msgs = [{"role": m.role, "content": (m.content or "")[:MAX_MSG_CHARS]}
            for m in body.messages if m.role in ("user", "assistant")]

    # turn cap: after MAX_USER_TURNS close the conversation politely without calling the model
    user_turns = sum(1 for m in msgs if m["role"] == "user")
    if user_turns > MAX_USER_TURNS:
        return {"reply": CLOSING[lang], "profile": {}, "summary": "", "stage": "collect_contact"}

    # send the model only the last part of the history; it must start with a user message
    msgs = msgs[-MAX_HISTORY:]
    while msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]

    # soft close: from SOFT_CLOSE_TURNS on, order the model to wrap up right now
    system = SYSTEM[lang]
    if user_turns >= SOFT_CLOSE_TURNS:
        system = system + WRAP_UP[lang]

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=msgs,
            tools=[REPLY_TOOL],
            tool_choice={"type": "tool", "name": "record_reply"},
        )
    except Exception:
        log.exception("model call failed")
        return {"reply": ERR_MSG[lang], "profile": {}, "summary": "", "stage": "exploring"}

    parsed = None
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_reply":
            parsed = block.input or {}
            break
    if parsed is None:
        # Fallback: model answered in plain text instead of the tool
        raw = "".join(b.text for b in resp.content if b.type == "text")
        parsed = _parse_model_json(raw, lang)

    return {
        "reply": parsed.get("reply", ""),
        "profile": parsed.get("profile", {}) or {},
        "summary": parsed.get("summary", "") or "",
        "stage": parsed.get("stage", "exploring"),
    }


def _coerce_list(value):
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = [p.strip() for p in value.split(",")]
    else:
        items = []
    return [i for i in items if i]


def _multi(value, allowed):
    return [{"name": v} for v in _coerce_list(value) if v in allowed]


def _build_notion_properties(profile, contact, consent, cv_name, summary, lang):
    name = (contact.get("name") or "").strip()
    note = (contact.get("note") or "").strip()
    summary = (summary or "").strip()
    # Title now carries the candidate's name (Message -> Name rename). Fall back to
    # email or a default so the card is never blank when the name was skipped.
    title = name or (contact.get("email") or "").strip() or ("New candidate from chat" if lang == "en" else "Nový kandidát z chatu")

    props = {
        "Name": {"title": [{"text": {"content": title[:1900]}}]},
        "Inzerát": {"select": {"name": "Hledáme chytré lidi"}},
        "Status": {"status": {"name": "Unprocessed"}},
        "Source": {"select": {"name": "Talent chat"}},
    }
    if note:
        props["Application"] = {"rich_text": [{"text": {"content": note[:1900]}}]}
    if contact.get("email"):
        props["E-mail"] = {"email": contact["email"]}
    if contact.get("linkedin"):
        props["LinkedIn"] = {"url": contact["linkedin"]}
    if summary:
        props["Summary"] = {"rich_text": [{"text": {"content": summary[:1900]}}]}
    # Score, Doporučení, Fit oblast, Company tier and Education stay empty: they were
    # the evaluation automation's outputs and are now filled manually, not by the chat.

    area = _multi(profile.get("area"), ALLOWED_AREA)
    level = _multi(profile.get("level"), ALLOWED_LEVEL)
    mode = _multi(profile.get("workMode"), ALLOWED_MODE)
    status = _multi(profile.get("status"), ALLOWED_STATUS)
    if area:
        props["Oblast"] = {"multi_select": area}
    if level:
        props["Level"] = {"multi_select": level}
    if mode:
        props["Remote?"] = {"multi_select": mode}
    if status:
        props["Aktivita hledání"] = {"multi_select": status}
    return props


def _decode_cv(cv: Optional[CVIn]):
    """Return (filename, raw_bytes, mime) or (None, None, None). CV stays in memory only."""
    if not cv or not cv.data:
        return None, None, None
    raw = cv.data
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]  # strip data URL prefix if present
    try:
        blob = base64.b64decode(raw)
    except Exception:
        return None, None, None
    if not blob or len(blob) > MAX_CV_BYTES:
        return None, None, None
    return (cv.name or "cv"), blob, (cv.type or "application/octet-stream")


def _cv_to_text(blob, mime, filename) -> str:
    """Extract readable text from a CV in memory (PDF or DOCX). Best-effort: returns
    "" on anything unexpected so scoring can degrade to transcript-only. Never raises."""
    if not blob:
        return ""
    name = (filename or "").lower()
    is_pdf = "pdf" in (mime or "") or name.endswith(".pdf")
    is_docx = "word" in (mime or "") or name.endswith(".docx")
    try:
        if is_pdf:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(blob))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        elif is_docx:
            import docx  # python-docx
            doc = docx.Document(io.BytesIO(blob))
            text = "\n".join(p.text for p in doc.paragraphs)
        else:
            return ""  # old .doc or unknown format: skip, scoring falls back to transcript
    except Exception:
        log.exception("CV text extraction failed")
        return ""
    return " ".join(text.split())[:SCORE_CV_CHARS]


def _send_via_resend(subject, body_text, reply_to, filename, blob) -> bool:
    """Deliver the email through Resend's HTTPS API (works where SMTP ports are blocked)."""
    payload = {
        "from": f"Miton talent <{MAIL_FROM}>",
        "to": [MAIL_TO],
        "subject": subject,
        "text": body_text,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    if blob:
        payload["attachments"] = [{"filename": filename or "cv", "content": base64.b64encode(blob).decode()}]
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        if r.status_code < 300:
            return True
        log.error("Resend API failed (%s): %s", r.status_code, r.text[:300])
        return False
    except Exception:
        log.exception("Resend API raised")
        return False


def _send_cv_email(profile, contact, cv: Optional[CVIn], summary, lang) -> bool:
    """Email the submission to the recruiter inbox with the CV attached. In-memory only."""
    if not RESEND_API_KEY and not (SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_TO and MAIL_FROM):
        return False

    summary = (summary or "").strip()

    filename, blob, mime = _decode_cv(cv)

    name = (contact.get("name") or "").strip()
    who = _hdr(name) or _hdr(contact.get("email")) or ("kandidát" if lang == "cs" else "candidate")
    subject = f"Nový kandidát z webu: {who}" if lang == "cs" else f"New candidate from the website: {who}"

    if lang == "cs":
        lines = [
            f"Jméno: {name}",
            f"E-mail: {contact.get('email', '')}",
            f"LinkedIn: {contact.get('linkedin', '')}",
            "",
            f"Oblast: {profile.get('area', '')}",
            f"Level: {profile.get('level', '')}",
            f"Forma: {profile.get('workMode', '')}",
            f"Stav hledání: {profile.get('status', '')}",
            "",
            f"Poznámka: {contact.get('note', '')}",
        ]
    else:
        lines = [
            f"Name: {name}",
            f"Email: {contact.get('email', '')}",
            f"LinkedIn: {contact.get('linkedin', '')}",
            "",
            f"Area: {profile.get('area', '')}",
            f"Level: {profile.get('level', '')}",
            f"Work mode: {profile.get('workMode', '')}",
            f"Search status: {profile.get('status', '')}",
            "",
            f"Note: {contact.get('note', '')}",
        ]

    if summary:
        lines = [f"Shrnutí: {summary}" if lang == "cs" else f"Summary: {summary}", ""] + lines

    body_text = "\n".join(str(x) for x in lines)

    if RESEND_API_KEY:
        return _send_via_resend(subject, body_text, _hdr(contact.get("email")), filename, blob)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    reply_to = _hdr(contact.get("email"))
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body_text)

    if blob:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(blob, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=filename)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls(context=ctx)
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception:
        log.exception("CV email delivery failed")
        return False


def _post_notion(props: dict, children=None):
    payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props}
    if children:
        payload["children"] = children
    return httpx.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )


_NOTION_HEADERS = {
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def _notion_patch(url: str, payload: dict):
    """PATCH a Notion resource with one retry on 429 (rate limit). Returns the
    response, or None if the request itself raised."""
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", **_NOTION_HEADERS}
    for attempt in (1, 2):
        try:
            r = httpx.patch(url, headers=headers, json=payload, timeout=15)
        except Exception:
            log.exception("Notion PATCH raised (%s)", url)
            return None
        if r.status_code != 429:
            return r
        time.sleep(float(r.headers.get("Retry-After", "1")))
    return r


def _update_notion_page(page_id: str, props: dict) -> bool:
    r = _notion_patch(f"https://api.notion.com/v1/pages/{page_id}", {"properties": props})
    if r is not None and r.status_code < 300:
        return True
    if r is not None:
        log.error("Notion property update failed (%s): %s", r.status_code, r.text[:400])
    return False


def _append_notion_blocks(page_id: str, blocks: list) -> bool:
    if not blocks:
        return True
    r = _notion_patch(f"https://api.notion.com/v1/blocks/{page_id}/children", {"children": blocks})
    if r is not None and r.status_code < 300:
        return True
    if r is not None:
        log.error("Notion block append failed (%s): %s", r.status_code, r.text[:400])
    return False


def _transcript_blocks(messages, lang):
    """Turn the chat transcript into Notion page-body blocks (one paragraph per turn)."""
    if not messages:
        return []
    heading = "Přepis konverzace" if lang != "en" else "Chat transcript"
    blocks = [{"object": "block", "type": "heading_2",
               "heading_2": {"rich_text": [{"text": {"content": heading}}]}}]
    for m in messages[:60]:  # Notion allows max 100 blocks per create request
        role = getattr(m, "role", "")
        if role not in ("user", "assistant"):
            continue
        who = ("Kandidát" if lang != "en" else "Candidate") if role == "user" else "Miton"
        text = (getattr(m, "content", "") or "")[:1900]
        if not text:
            continue
        blocks.append({"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": [
                           {"text": {"content": f"{who}: "}, "annotations": {"bold": True}},
                           {"text": {"content": text}},
                       ]}})
    return blocks


def _consent_block(consent, lang):
    """GDPR consent stamp for the page body (Summary column is reserved for evaluation)."""
    if not consent:
        return []
    stamp = _utcnow().strftime("%Y-%m-%d %H:%M UTC")
    text = f"Souhlas GDPR: ano ({stamp})" if lang != "en" else f"GDPR consent: yes ({stamp})"
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [
                 {"text": {"content": text}, "annotations": {"italic": True, "color": "gray"}}]}}]


def _write_notion(profile, contact, consent, cv_name, summary, lang, messages=None) -> Optional[str]:
    """Create the Notion row with the chat transcript in the page body. Returns the new
    page id on success (needed for scoring), or None. If the write fails (e.g. a schema
    mismatch), retry once without the Summary property so the contact is never lost."""
    if not NOTION_TOKEN:
        return None
    props = _build_notion_properties(profile, contact, consent, cv_name, summary, lang)
    children = _transcript_blocks(messages or [], lang) + _consent_block(consent, lang)
    try:
        r = _post_notion(props, children)
        if r.status_code < 300:
            return r.json().get("id")
        log.error("Notion write failed (%s): %s", r.status_code, r.text[:500])
        if "Summary" in props:
            props.pop("Summary")
            r2 = _post_notion(props, children)
            if r2.status_code < 300:
                log.warning("Notion row created WITHOUT Summary - check the Summary column in Notion")
                return r2.json().get("id")
            log.error("Notion retry without Summary failed (%s): %s", r2.status_code, r2.text[:500])
        return None
    except Exception:
        log.exception("Notion write raised")
        return None


def _score_candidate(cv_text, profile, summary, messages, lang):
    """Call Claude to score the candidate. Returns the record_score dict, or None."""
    transcript = "\n".join(
        f"{'Kandidát' if getattr(m, 'role', '') == 'user' else 'Miton'}: {getattr(m, 'content', '')}"
        for m in (messages or []) if getattr(m, "role", "") in ("user", "assistant")
    )[:8000]
    user_input = "\n\n".join([
        f"Profil z chatu: {json.dumps(profile or {}, ensure_ascii=False)}",
        f"Shrnutí: {summary or ''}",
        f"Přepis konverzace:\n{transcript or '(žádný)'}",
        f"Text CV:\n{cv_text or '(kandidát nepřiložil čitelné CV, hodnoť jen z chatu)'}",
    ])
    try:
        resp = client.messages.create(
            model=SCORE_MODEL,
            max_tokens=SCORE_MAX_TOKENS,
            system=SCORE_SYSTEM,
            messages=[{"role": "user", "content": user_input}],
            tools=[SCORE_TOOL],
            tool_choice={"type": "tool", "name": "record_score"},
            timeout=SCORE_TIMEOUT,
        )
    except Exception:
        log.exception("scoring model call failed")
        return None
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_score":
            return block.input or {}
    return None


def _clamp03(v) -> int:
    try:
        return max(0, min(3, int(v)))
    except (TypeError, ValueError):
        return 0


def _compute_score(verdict) -> int:
    """Deterministic 0-100 score from the classified tiers + track-record scorecard.
    Company + education tiers are the backbone (github-sourcing career-quality method),
    the conversation scorecard adds the rest."""
    company = COMPANY_TIER_POINTS.get(verdict.get("company_tier"), 0.0)
    education = EDUCATION_TIER_POINTS.get(verdict.get("education"), 0.0)
    track = (_clamp03(verdict.get("ownership")) + _clamp03(verdict.get("impact"))
             + _clamp03(verdict.get("depth"))) / 9.0
    score = 100.0 * (SCORE_W_COMPANY * company + SCORE_W_EDUCATION * education + SCORE_W_TRACK * track)
    return max(0, min(100, round(score)))


def _recommendation_for(score) -> str:
    if score >= REC_POTENTIAL_MIN:
        return "Potential fit"
    if score >= REC_DECIDE_MIN:
        return "K rozhodnutí"
    return "Low fit"


def _score_props(verdict) -> dict:
    """Map the classified verdict to Notion properties. Score and recommendation are
    computed here from the tiers/scorecard; only allowed option values are written."""
    score = _compute_score(verdict)
    props = {
        "Score": {"number": score},
        "Doporučení": {"select": {"name": _recommendation_for(score)}},
    }
    if verdict.get("company_tier") in ALLOWED_COMPANY_TIER:
        props["Company tier"] = {"select": {"name": verdict["company_tier"]}}
    if verdict.get("education") in ALLOWED_EDUCATION:
        props["Education"] = {"select": {"name": verdict["education"]}}
    fit = [{"name": f} for f in (verdict.get("fit_oblast") or []) if f in ALLOWED_FIT]
    if fit:
        props["Fit oblast"] = {"multi_select": fit}
    return props


def _score_blocks(verdict, lang) -> list:
    """The evaluation as page-body blocks: a computed-breakdown line + the reasoning."""
    score = _compute_score(verdict)
    breakdown = (f"Score {score} · Company {verdict.get('company_tier', '?')} · "
                 f"Education {verdict.get('education', '?')} · "
                 f"Ownership {_clamp03(verdict.get('ownership'))}/3 · "
                 f"Impact {_clamp03(verdict.get('impact'))}/3 · "
                 f"Depth {_clamp03(verdict.get('depth'))}/3 · "
                 f"{_recommendation_for(score)}")
    heading = "Evaluace" if lang != "en" else "Evaluation"
    blocks = [
        {"object": "block", "type": "heading_2",
         "heading_2": {"rich_text": [{"text": {"content": heading}}]}},
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": [{"text": {"content": breakdown},
                                      "annotations": {"bold": True}}]}},
    ]
    for para in [p.strip() for p in (verdict.get("reasoning") or "").split("\n") if p.strip()][:40]:
        blocks.append({"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": [{"text": {"content": para[:1900]}}]}})
    return blocks


def _run_scoring(uid, page_id, cv: Optional[CVIn], profile, summary, messages, lang):
    """Best-effort scoring of an already-saved candidate. Never raises to the caller.
    Writes to the Miton Talent database (always) and to Notion (while dual-writing)."""
    filename, blob, mime = _decode_cv(cv)
    cv_text = _cv_to_text(blob, mime, filename) if blob else ""
    score = _score_candidate(cv_text, profile, summary, messages, lang)
    if not score:
        log.warning("scoring produced no result for %s", uid)
        return
    if uid:
        try:
            with tdb.session() as s:
                cand = tstore.find_candidate(s, uid=uid)
                if cand:
                    computed = _compute_score(score)
                    tstore.apply_score(
                        s, cand, score=computed,
                        company_tier=score.get("company_tier"), education_tier=score.get("education"),
                        fit_areas=score.get("fit_oblast") or [], recommendation=_recommendation_for(computed),
                        reasoning=score.get("reasoning"),
                        breakdown=(f"company {score.get('company_tier', '?')} · education {score.get('education', '?')} · "
                                   f"ownership {_clamp03(score.get('ownership'))}/3 · impact {_clamp03(score.get('impact'))}/3 · "
                                   f"depth {_clamp03(score.get('depth'))}/3"),
                    )
        except Exception:
            log.exception("scoring write to database failed for %s", uid)
    if page_id:
        props = _score_props(score)
        if props:
            _update_notion_page(page_id, props)
        _append_notion_blocks(page_id, _score_blocks(score, lang))


def _write_database(uid, profile, contact, consent, consent_text, cv_name, summary, lang, messages) -> bool:
    """Store the submission in the Miton Talent database. Idempotent on uid. Returns
    True when the row exists afterwards (created now or already there)."""
    try:
        with tdb.session() as s:
            _, created = tstore.create_from_submission(
                s, uid=uid, profile=profile, contact=contact, summary=summary, consent=consent,
                consent_text=consent_text, cv_name=cv_name, lang=lang, messages=messages,
            )
        log.info("candidate %s %s", uid, "stored" if created else "already stored")
        return True
    except Exception:
        log.exception("database write failed for %s", uid)
        return False


def _deliver_submission(uid, profile, contact, cv: Optional[CVIn], summary, consent, consent_text,
                        cv_name, lang, messages=None):
    """Database + email + Notion delivery, run in the background so the visitor never
    waits. Order: the database first (it is the system of record), then the CV e-mail,
    then Notion while NOTION_TOKEN is still set (dual-write during the migration).
    Scoring runs after the candidate is safely written and never affects the save."""
    db_ok = _write_database(uid, profile, contact, consent, consent_text, cv_name, summary, lang, messages)
    email_ok = _send_cv_email(profile, contact, cv, summary, lang)
    page_id = _write_notion(profile, contact, consent, cv_name, summary, lang, messages)
    if not db_ok and not email_ok and not page_id:
        log.error("submission delivered NOWHERE (database, email and Notion all failed/off); check submissions.jsonl")
    if SCORING_ENABLED and (db_ok or page_id):
        try:
            _run_scoring(uid if db_ok else None, page_id, cv, profile, summary, messages, lang)
        except Exception:
            log.exception("scoring step raised (candidate already saved)")


@app.post("/submit")
def submit(body: SubmitIn, request: Request, background: BackgroundTasks):
    # per-IP rate limit: stop repeated submissions from the same visitor
    if _rate_limited("submit", _client_ip(request), SUBMIT_RATE_LIMIT, SUBMIT_RATE_WINDOW):
        return {"ok": True, "rate_limited": True}

    # GDPR: consent is enforced server-side, not just by the form in the browser
    if not body.consent:
        raise HTTPException(status_code=400, detail="consent required")

    cv_name = (body.cv.name if body.cv else "") or ""
    uid = uuid.uuid4().hex  # stable id of this submission across database, e-mail and logs

    # local backup just in case (filename only, never the CV bytes)
    record = {
        "uid": uid,
        "ts": _utcnow().isoformat().replace("+00:00", "Z"),
        "lang": body.lang,
        "profile": body.profile,
        "contact": body.contact,
        "summary": body.summary,
        "cv_name": cv_name,
        "consent": bool(body.consent),
        "consent_text": body.consent_text,
    }
    try:
        with open(SUBMISSIONS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("failed to write submissions backup")

    # deliver by email + Notion in the background; the visitor gets the thank-you immediately
    background.add_task(
        _deliver_submission,
        uid, body.profile or {}, body.contact or {}, body.cv, body.summary,
        bool(body.consent), body.consent_text, cv_name, body.lang, body.messages or [],
    )
    return {"ok": True, "queued": True}
