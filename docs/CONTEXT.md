# Miton talent chat — project context

Context primer for an AI assistant or a new collaborator. Everything here is drawn from
the repository (`README.md`, `HANDOVER.md`, `backend/app.py`, `frontend/src/`) as of the
current `main`. No secrets are included.

---

## 1. What this is

A candidate-facing AI chat widget for the career page of **Miton**, a Czech venture
capital group (miton.cz/kariera). It replaces a Typeform.

A visitor has a short conversation (3 to 4 questions). The model extracts a structured
profile, writes a running recruiter summary, then invites the person to leave contact
details and an optional CV. The submission becomes a row in the **Miton Talent
database** (`backend/talent/`, Postgres on Railway, SQLite locally), and the CV is
emailed to the recruiter inbox. While `NOTION_TOKEN` is still set, the row is also
written to the Notion database "Hledáme chytré lidi" (transitional; Notion is being
cancelled, see `docs/notion-replacement-analysis.md`).

Since September 2026 this repository is the home of **Miton Talent**, Miton's private
recruiting back office: the chat, the candidate database and searches, and (planned)
the TOP 300 watch list and the portfolio open-roles scrape. It is fully separate from
**Alister** (Miton's developer sourcing product): own Anthropic key, own database, own
repo. See `docs/miton-layer-architecture.md` for the rule that decides what lives where.

## 2. Who builds it

- **Markéta Pařízek** — Talent Partner at Miton. Owns the product, the copy, the Notion
  schema and the backend deployment. Non-programmer by background; builds this herself
  with Claude and Claude Code. marketa.parizek@miton.cz
- **Petr** — web developer at the external agency VIRTII DIGITAL. Embeds the widget on
  the Miton career page and handles the English translation of the page copy.
  Communication with him is in Czech, formal (vykání).
- **Míša Gregorová** — Miton colleague, earlier contact point with Petr.

Working conventions: repo, code, comments and documentation in English. Product copy in
Czech and English. Secrets are shared via onetimesecret.com, never plain email.

## 3. Repository layout

```
backend/                FastAPI service (the whole product runs from here in production)
  app.py                ~1090 lines, single module: prompts, endpoints, Notion, email, scoring
  requirements.txt      fastapi, uvicorn, anthropic, httpx, pydantic, pypdf, python-docx
  Procfile              web: uvicorn app:app --host 0.0.0.0 --port $PORT
  .env.example          documented environment variables
  static/               embed.html (the iframed page), miton-talent-chat.js (built widget), fonts
frontend/
  src/MitonTalentChat.jsx   the whole widget: chat, profile chips, contact form, styles
  src/main.jsx              auto-mount entry for the standalone bundle
  src/logo.js               inline brand assets
  vite.config.js            IIFE library build → dist/miton-talent-chat.js (React bundled in)
README.md               operator documentation
HANDOVER.md             deployment and embedding instructions for the web developer
```

The frontend build output is copied into the backend's static folder:

```
cd frontend && npm run build && cp dist/miton-talent-chat.js ../backend/static/
```

## 4. Runtime architecture

```
visitor → iframe on miton.cz/kariera
            ↓
        backend "/"  (embed.html + widget bundle, same origin)
            ↓ POST /chat        → Anthropic API (forced tool call)   → reply + profile + summary + stage
            ↓ POST /submit      → 202 immediately, then in background:
                                   → Resend HTTPS API   (CV + summary email to the recruiter)
                                   → Notion API         (row in "Hledáme chytré lidi" + transcript in page body)
                                   → optional scoring pass (off by default)
```

The backend is the only thing deployed. It serves the widget page, the bundle, the fonts
and the API from one URL, so the site needs no CORS setup and nothing to rebuild.

Production: **Railway**, root directory `backend`
→ `https://miton-talent-chat-production.up.railway.app`
Any Python host works the same way. Email goes through Resend's HTTPS API because Railway
blocks outbound SMTP ports.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | the embed page; `?lang=en` or `?locale=en` for English |
| `GET /widget.js` | the built widget bundle |
| `GET /fonts/{name}` | Degular Display woff2 files |
| `GET /health` | `{"ok": true}` |
| `GET /diag` | config self-check, booleans and status codes only, never secrets |
| `GET /embed-test` | local iframe test page |
| `POST /chat` | `{messages, lang}` → `{reply, profile, summary, stage}` |
| `POST /submit` | `{lang, profile, contact, summary, consent, consent_text, cv, messages}` → `{ok: true, queued: true}` |

## 5. Conversation design

Two full system prompts, `SYSTEM["cs"]` and `SYSTEM["en"]`. The persona is a curious,
friendly recruiting guide, "more like a curious person over coffee than a questionnaire".

Rules baked into the prompt:

- One question at a time, 3 to 4 questions for the whole conversation, at most one
  follow-up per topic.
- Never ask about seniority directly — infer it. Never ask about age or where the person
  lives.
- No em or en dashes in output, no marketing cliches.
- Careers and Miton portfolio companies only; off-topic gets one friendly redirect.
- Portfolio companies are listed by area in the prompt (AI, crypto, e-commerce,
  gastrotech, mental health, travel, HR tech, real estate tech) and may be name-dropped
  one at a time, never listed all at once.
- Wrap-up trigger: once area, work mode and job-search status are known plus one thing
  the person is good at, or once they signal they are done, stop asking and move to
  `stage: "collect_contact"`.
- Human escalation always points at Markéta.

### Structured output

The model must answer through a **forced tool call** `record_reply`:

```
reply    string                              text shown in the chat bubble
profile  { area[], level, workMode, status } values constrained by enum
summary  string                              running 2-4 sentence recruiter note, third person
stage    "exploring" | "collect_contact"
```

This is the central design decision: raw JSON can never leak into the bubble, and profile
values can only be the exact strings that already exist as Notion options. A plain-text
fallback parser exists but should not normally fire.

Enums (`ALLOWED_*` in `app.py`) — **must match the Notion options character for character**:

- `area`: Marketing, Software engineering, Accounting & Finance, People & HR,
  Sales & Business Development, Data Science, Product, Administration, Operations, Other
- `level`: Intern role, Junior role, Mid level, Specialist role, C-level management,
  Founder/co-founder
- `workMode`: Remote, Hybrid, On-site
- `status` (cs): Aktivně hledám, Pasivně sleduji možnosti na trhu, Právě nehledám

### Cost and abuse limits

| Limit | Value |
|---|---|
| max output tokens | 800 (`MAX_TOKENS`) |
| user turns | soft close from turn 5 (`SOFT_CLOSE_TURNS`), hard close at 12 (`MAX_USER_TURNS`) |
| single message | trimmed to 2000 chars |
| history sent to the model | last 20 messages |
| chat rate limit | 30 messages / 10 min per IP |
| submit rate limit | 5 submissions / 10 min per IP |
| CV size | 8 MB, held in memory only, never written to disk |

`X-Forwarded-For` handling is hardened so spoofing does not bypass the limits. The turn
cap and the rate limits answer without calling the model at all.

Default model: `claude-sonnet-4-6`, overridable with the `MODEL` env var, no code change.

## 6. Data flow on submit

1. GDPR consent is enforced **server-side** — no consent means HTTP 400. The exact
   consent text and a UTC timestamp are stored with the candidate.
2. A local `submissions.jsonl` backup line is written (filename only, never CV bytes).
   This does not persist on ephemeral hosts, so Notion is the real store.
3. The visitor gets the thank-you immediately; delivery runs in a background task.
4. Email via Resend: summary plus the CV as an attachment, to a fixed `MAIL_TO`. The
   candidate never receives a copy. PDF and DOCX are also parsed to text (`pypdf`,
   `python-docx`) for the optional scoring step.
5. Notion row created, then the chat transcript and the consent record appended as page
   body blocks.

### Notion mapping

Database "Hledáme chytré lidi", id `1ba6065a-c67d-4ba6-97f5-1a6c9662d137`,
integration `miton-talent-chat`.

| Notion property | Type | Written value |
|---|---|---|
| `Name` | title | candidate name, falls back to email, then a placeholder |
| `Application` | rich_text | the candidate's free-text note |
| `Inzerát` | select | `Hledáme chytré lidi` |
| `Source` | select | `Talent chat` |
| `Status` | status | `Unprocessed` |
| `E-mail` | email | |
| `LinkedIn` | url | |
| `Summary` | rich_text | the AI running summary |
| `Oblast` | multi_select | from `area` |
| `Level` | multi_select | from `level` |
| `Remote?` | multi_select | from `workMode` |
| `Aktivita hledání` | multi_select | from `status` |

**Hard rule:** the backend only writes allowlisted option values, so it never creates new
Notion options and never pollutes the schema. Do not rename, delete or retype the columns
above. Adding columns, hiding them in views, formulas and new views are all safe.

## 7. Optional scoring pass

`SCORING_ENABLED=0` by default — currently off; those Notion fields are filled manually.

When enabled, a second forced tool call (`record_score`) classifies an already-saved
candidate, so scoring can never affect whether the candidate is stored:

- `company_tier` T1-T4 and `education` T1-T3 (career-quality tiers adapted from the
  `github-sourcing` project)
- `ownership`, `impact`, `depth`, each 0-3
- `fit_oblast` from AI, Krypto, E-commerce, Gastrotech, Mental health, Miton interní
- `reasoning`, a short recruiter scorecard

The number is **computed in code, not by the model**: 45% company tier, 20% education,
35% track record, scaled to 0-100. Bands: ≥65 `Potential fit`, ≥53 `K rozhodnutí`, below
that `Low fit`. Results go to `Score`, `Doporučení`, `Company tier`, `Education`,
`Fit oblast`, plus an "Evaluace" section in the page body.

Works for all roles, not only technical ones. Handles Czech and English CVs.

## 8. Frontend

Single React component, no UI library, styles as inline objects plus one injected CSS
string. Built by Vite in IIFE library mode into one self-contained file with React
bundled in, so the host page needs nothing else.

Design direction: calm, minimal, LLM-style interface, not a classic chat bubble widget.
Reference points are the a16z fellowship application chat and the Claude/ChatGPT
aesthetic. No logo in the header, language toggle top left, small red "m" assistant
avatar, profile chips that appear progressively as the conversation extracts them.

Brand: red `#E32726`, ink `#16181D`, warm neutral surfaces, Degular Display self-hosted,
starter-prompt tints `#B4DABF` `#EB5E09` `#FFD300`.

State lives in the component: `messages`, `profile`, `summary`, `stage`, `contact`,
`cvFile`, `consent`, `submitted`. The contact form appears when `stage` flips to
`collect_contact`.

### Embedding

Option A, recommended — iframe, one URL, nothing to host:

```html
<iframe src="https://YOUR-BACKEND-URL" title="Miton talent"
        style="width:100%;max-width:720px;height:720px;border:0;" loading="lazy"></iframe>
```

The widget measures its own content with a `ResizeObserver` and reports height to the
parent via `postMessage({type: "miton-talent-height", height})`. The parent notification
is sent **before** the widget's own animation starts, so the outer wrapper and the inner
content animate together (270 ms, matching easing).

Option B — native embed with `<div id="miton-talent-chat" data-backend=... data-lang=...>`
plus the script tag. Requires the site's domain in `ALLOWED_ORIGINS`.

Placement on the career page: hero section, the chat as the dominant element, headline and
three reasons above it.

## 9. Configuration

All via environment variables, documented in `backend/.env.example`. Secrets never in the
repo.

| Variable | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | required, Miton's own key, not Alister's |
| `NOTION_TOKEN` | without it, submissions land in `submissions.jsonl` only |
| `NOTION_DATABASE_ID` | pre-filled |
| `ALLOWED_ORIGINS` | defaults to `https://www.miton.cz,https://miton.cz` |
| `SMTP_HOST/PORT/USER/PASS`, `MAIL_FROM`, `MAIL_TO` | Resend; without them the CV email is skipped |
| `MODEL`, `SCORE_MODEL` | default `claude-sonnet-4-6` |
| `SCORING_ENABLED` | `0` by default |
| `SOFT_CLOSE_TURNS`, `CHAT_RATE_LIMIT`, `CHAT_RATE_WINDOW`, `SUBMIT_RATE_LIMIT`, `SUBMIT_RATE_WINDOW` | tuning |

Graceful degradation is deliberate: only `ANTHROPIC_API_KEY` is needed to boot. Missing
Notion or SMTP config degrades that one path and leaves the rest working.

## 10. Security posture

- The embed page sends `Content-Security-Policy: frame-ancestors` limited to the Miton
  domains, so nobody else can iframe the chat and burn tokens.
- CORS limited to the Miton domains.
- The backend talks to exactly three external services: Anthropic, Notion, Resend.
- `MAIL_TO` is fixed in env; the recipient can never be influenced from the browser.
- CVs are size-capped, memory-only, forwarded as an attachment, never persisted to disk.
- `/diag` returns booleans and status codes, never secret values.

## 11. State and open items

Late pre-launch. Deployed to a dev preview by Petr, Czech copy being finalised, which
blocks the English translation.

Before go-live (from `README.md`):

- Railway: move off the trial to the Hobby plan so the service stays up.
- Resend: verify the miton.cz domain, then switch `MAIL_FROM` off `onboarding@resend.dev`
  so notifications do not land in spam.
- Consent text: `PRIVACY_URL` in `frontend/src/MitonTalentChat.jsx` is still empty and the
  legal entity name is missing. Legal review recommended; the notice should also mention
  AI processing and the processors used.
- Optionally tune the per-IP rate limits.

Longer-term infrastructure preference is Hetzner on a subdomain (EU data residency, cost),
with Render as the alternative to Railway.

## 12. Conventions worth knowing before changing anything

- Czech copy: no em dashes, no slashes, no colons. Natural Czech, not marketing Czech.
  Reference voice: "Zvažujete práci pro startup? Pojďme to probrat."
- Never change a Notion multi-select option value. The backend must emit strings that
  already exist in the database, exactly.
- Treat this as a split-service project. A flat single-service structure does not match
  the repo.
- Local JSONL backup is a safety net, not a store; it does not survive an ephemeral host.
- Frontend changes are not live until the bundle is rebuilt and copied into
  `backend/static/`.
