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
import base64
import smtplib
import datetime
from email.message import EmailMessage
from typing import List, Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from anthropic import Anthropic

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
SUBMISSIONS_FILE = "submissions.jsonl"

# Limits to keep token usage low
MAX_TOKENS = 400        # replies are short, no need for more
MAX_USER_TURNS = 12     # after this many user messages we close the conversation
MAX_MSG_CHARS = 2000    # trim a single overly long message
MAX_HISTORY = 20        # send the model only the last part of the history
MAX_CV_BYTES = 8 * 1024 * 1024  # reject CV attachments larger than 8 MB

CLOSING = {
    "cs": "Díky moc za pokec. Nech mi prosím kontakt (e-mail a LinkedIn). Projdeme si to a spojíme se s tebou, buď s pozvánkou na krátký call, nebo s informací, když zrovna nic nemáme.",
    "en": "Thanks for the chat. Please leave me your contact (email and LinkedIn). We will review it and get back to you, either with an invitation to a short call, or letting you know if we have nothing right now.",
}

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
    "cs": f"""Jsi přátelský průvodce náborem investiční skupiny Miton. Bavíš se s lidmi, kteří zvažují práci ve startupu, a z rozhovoru přirozeně zjišťuješ čtyři věci:
1) oblast nebo oddělení (Marketing, Software engineering, Accounting & Finance, People & HR, Sales & Business Development, Data Science, Product, Administration, Operations, případně jiné),
2) úroveň role (stáž, junior, specialista nebo senior, C-level, founder nebo co-founder),
3) preferovanou formu práce (remote, hybrid, on-site),
4) jak je na tom s hledáním (aktivně hledá, pasivně sleduje trh, právě nehledá).

Mluvíš česky, krátce a lidsky, bez marketingových frází. Reaguješ na to, co člověk napsal, a ptáš se vždy jen na jednu věc. Kde to sedne, můžeš mimochodem zmínit jednu konkrétní firmu z portfolia. Firmy nevyjmenovávej naráz. Bavíš se jen o kariéře, rolích a firmách z portfolia Mitonu. Když se někdo zeptá na něco mimo téma, jednou větou ho mile vrať zpátky a dál se tím nezabývej. Když má někdo technický problém nebo chce mluvit s živým člověkem, odkaž ho na Markétu Pařízek, marketa.parizek@miton.cz.

Portfolio podle oblastí: {PORTFOLIO_CS}

Jakmile máš základní obrázek (zhruba po 3 až 5 výměnách), krátce shrň, co by mohlo sednout, vyzvi člověka, ať ti nechá kontakt, a vysvětli, co bude dál: projdeme si to a ozveme se e-mailem, buď s pozvánkou na krátký call, nebo s informací, když zrovna nic nemáme.

Odpovídej VÝHRADNĚ jako JSON bez dalšího textu nebo markdownu:
{{"reply":"tvoje odpověď","profile":{{"area":["Software engineering"],"level":"Specialist role","workMode":"Remote","status":"Aktivně hledám"}},"stage":"exploring"}}
Hodnoty v profile používej PŘESNĚ z těchto možností:
- area (jedna nebo více): Marketing, Software engineering, Accounting & Finance, People & HR, Sales & Business Development, Data Science, Product, Administration, Operations, Other
- level (jedna): Intern role, Junior role, Mid level, Specialist role, C-level management, Founder/co-founder
- workMode (jedna): Remote, Hybrid, On-site
- status (jedna): Aktivně hledám, Pasivně sleduji možnosti na trhu, Právě nehledám
Do profile dávej jen to, co už zaznělo; co nevíš, vynech. stage = "collect_contact" jakmile má smysl posbírat kontakt, jinak "exploring".""",
    "en": f"""You are a friendly recruiting guide for the investment group Miton. You talk with people considering a startup job and naturally learn four things from the conversation:
1) area or department (Marketing, Software engineering, Accounting & Finance, People & HR, Sales & Business Development, Data Science, Product, Administration, Operations, or other),
2) role level (intern, junior, specialist or senior, C-level, founder or co-founder),
3) preferred work mode (remote, hybrid, on-site),
4) job-search status (actively seeking, passively interested, not seeking).

You speak English, briefly and naturally, no marketing cliches. You react to what the person wrote and ask only one thing at a time. Where it fits, you may casually mention one specific portfolio company. Do not list companies all at once. You only discuss careers, roles, and Miton's portfolio companies. If someone asks something off-topic, steer them back in one sentence and do not engage further. If someone has a technical problem or wants to talk to a human, point them to Marketa Parizek, marketa.parizek@miton.cz.

Portfolio by area: {PORTFOLIO_EN}

Once you have a basic picture (roughly after 3 to 5 exchanges), briefly summarize what could fit, invite the person to leave their contact, and explain what happens next: we will review it and get back by email, either with an invitation to a short call, or letting them know if we have nothing right now.

Reply ONLY as JSON with no other text or markdown:
{{"reply":"your reply","profile":{{"area":["Software engineering"],"level":"Specialist role","workMode":"Remote","status":"Actively seeking a new role"}},"stage":"exploring"}}
Use values in profile EXACTLY from these options:
- area (one or more): Marketing, Software engineering, Accounting & Finance, People & HR, Sales & Business Development, Data Science, Product, Administration, Operations, Other
- level (one): Intern role, Junior role, Mid level, Specialist role, C-level management, Founder/co-founder
- workMode (one): Remote, Hybrid, On-site
- status (one): Actively seeking a new role, Just passively interested in market opportunities, Not seeking a new role
Only put in profile what has already come up; omit what you do not know. stage = "collect_contact" once it makes sense to collect contact, otherwise "exploring".""",
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
    consent: bool = False
    consent_text: str = ""
    cv: Optional[CVIn] = None


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/chat")
def chat(body: ChatIn):
    lang = "en" if body.lang == "en" else "cs"

    # trim overly long messages so input tokens do not grow
    msgs = [{"role": m.role, "content": (m.content or "")[:MAX_MSG_CHARS]} for m in body.messages]

    # turn cap: after MAX_USER_TURNS close the conversation politely without calling the model
    user_turns = sum(1 for m in msgs if m["role"] == "user")
    if user_turns > MAX_USER_TURNS:
        return {"reply": CLOSING[lang], "profile": {}, "stage": "collect_contact"}

    # send the model only the last part of the history; it must start with a user message
    msgs = msgs[-MAX_HISTORY:]
    while msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM[lang],
        messages=msgs,
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    clean = raw.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(clean)
    except Exception:
        fallback = "Promiň, zkus to prosím znovu." if lang == "cs" else "Sorry, please try again."
        parsed = {"reply": clean or fallback, "profile": {}, "stage": "exploring"}

    return {
        "reply": parsed.get("reply", ""),
        "profile": parsed.get("profile", {}) or {},
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


def _build_notion_properties(profile, contact, consent, cv_name, lang):
    name = (contact.get("name") or "").strip()
    note = (contact.get("note") or "").strip()
    title = note or name or ("New contact from chat" if lang == "en" else "Nový kontakt z chatu")

    notes_parts = []
    if note:
        notes_parts.append(note)
    if cv_name:
        notes_parts.append(f"CV: {cv_name} (odesláno e-mailem)")
    if consent:
        stamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        notes_parts.append(f"Souhlas GDPR: ano ({stamp})")
    notes_text = " | ".join(notes_parts)

    # "Message" is the title-type column in this database
    props = {
        "Message": {"title": [{"text": {"content": title[:1900]}}]},
        "Inzerát": {"select": {"name": "Hledáme chytré lidi"}},
        "Status": {"status": {"name": "Unprocessed"}},
    }
    if name:
        props["Jméno"] = {"rich_text": [{"text": {"content": name}}]}
    if contact.get("email"):
        props["E-mail"] = {"email": contact["email"]}
    if contact.get("linkedin"):
        props["LinkedIn"] = {"url": contact["linkedin"]}
    if notes_text:
        props["Notes"] = {"rich_text": [{"text": {"content": notes_text[:1900]}}]}

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


def _send_cv_email(profile, contact, cv: Optional[CVIn], lang) -> bool:
    """Email the submission to the recruiter inbox with the CV attached. In-memory only."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_TO and MAIL_FROM):
        return False

    filename, blob, mime = _decode_cv(cv)

    name = (contact.get("name") or "").strip()
    who = name or (contact.get("email") or "").strip() or ("kandidát" if lang == "cs" else "candidate")
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

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    if contact.get("email"):
        msg["Reply-To"] = contact["email"]
    msg.set_content("\n".join(str(x) for x in lines))

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
        return False


@app.post("/submit")
def submit(body: SubmitIn):
    cv_name = (body.cv.name if body.cv else "") or ""

    # local backup just in case (filename only, never the CV bytes)
    record = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "lang": body.lang,
        "profile": body.profile,
        "contact": body.contact,
        "cv_name": cv_name,
        "consent": bool(body.consent),
        "consent_text": body.consent_text,
    }
    try:
        with open(SUBMISSIONS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # email the CV to the recruiter inbox (CV held in memory only)
    email_ok = _send_cv_email(body.profile or {}, body.contact or {}, body.cv, body.lang)

    # write a row into Notion (if a token is set)
    notion_ok = False
    if NOTION_TOKEN:
        try:
            props = _build_notion_properties(body.profile or {}, body.contact or {}, bool(body.consent), cv_name, body.lang)
            r = httpx.post(
                "https://api.notion.com/v1/pages",
                headers={
                    "Authorization": f"Bearer {NOTION_TOKEN}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
                json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props},
                timeout=15,
            )
            notion_ok = r.status_code < 300
        except Exception:
            notion_ok = False

    return {"ok": True, "notion": notion_ok, "email": email_ok}
