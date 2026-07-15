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

import os
import ssl
import json
import time
import base64
import logging
import smtplib
import threading
import datetime
from email.message import EmailMessage
from typing import List, Optional

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from anthropic import Anthropic

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

# Log failures loudly (Notion, SMTP, model). Never log personal data or message contents.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("miton-talent")

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
SUBMISSIONS_FILE = "submissions.jsonl"

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
    messages: List[Msg] = []  # chat transcript, stored in the Notion page body


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


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


@app.get("/diag")
def diag():
    """Config self-check for operations. Reports only booleans and status codes,
    never secret values, so it is safe to expose."""
    out = {
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "model": MODEL,
        "notion_token_set": bool(NOTION_TOKEN),
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
            out["resend_hint"] = "ok" if r.status_code < 300 else ("neplatny klic" if r.status_code == 401 else "necekany stav")
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
    return out


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
    # Summary, Score, Doporučení, Fit oblast, Company tier and Education are left
    # empty on purpose - the evaluation process fills them, not the chat.

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


def _write_notion(profile, contact, consent, cv_name, summary, lang, messages=None) -> bool:
    """Create the Notion row with the chat transcript in the page body. If the write
    fails (e.g. a schema mismatch), retry once without the Summary property so the
    contact itself is never lost. Failures are logged, never silent."""
    if not NOTION_TOKEN:
        return False
    props = _build_notion_properties(profile, contact, consent, cv_name, summary, lang)
    children = _transcript_blocks(messages or [], lang) + _consent_block(consent, lang)
    try:
        r = _post_notion(props, children)
        if r.status_code < 300:
            return True
        log.error("Notion write failed (%s): %s", r.status_code, r.text[:500])
        if "Summary" in props:
            props.pop("Summary")
            r2 = _post_notion(props, children)
            if r2.status_code < 300:
                log.warning("Notion row created WITHOUT Summary - check the Summary column in Notion")
                return True
            log.error("Notion retry without Summary failed (%s): %s", r2.status_code, r2.text[:500])
        return False
    except Exception:
        log.exception("Notion write raised")
        return False


def _deliver_submission(profile, contact, cv: Optional[CVIn], summary, consent, cv_name, lang, messages=None):
    """Email + Notion delivery, run in the background so the visitor never waits on SMTP."""
    email_ok = _send_cv_email(profile, contact, cv, summary, lang)
    notion_ok = _write_notion(profile, contact, consent, cv_name, summary, lang, messages)
    if not email_ok and not notion_ok:
        log.error("submission delivered NOWHERE (email and Notion both failed/off); check submissions.jsonl")


@app.post("/submit")
def submit(body: SubmitIn, request: Request, background: BackgroundTasks):
    # per-IP rate limit: stop repeated submissions from the same visitor
    if _rate_limited("submit", _client_ip(request), SUBMIT_RATE_LIMIT, SUBMIT_RATE_WINDOW):
        return {"ok": True, "rate_limited": True}

    # GDPR: consent is enforced server-side, not just by the form in the browser
    if not body.consent:
        raise HTTPException(status_code=400, detail="consent required")

    cv_name = (body.cv.name if body.cv else "") or ""

    # local backup just in case (filename only, never the CV bytes)
    record = {
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
        body.profile or {}, body.contact or {}, body.cv, body.summary,
        bool(body.consent), cv_name, body.lang, body.messages or [],
    )
    return {"ok": True, "queued": True}
