# Miton talent chat

A chat widget for the miton.cz career page that replaces the Typeform. A candidate
has a short conversation, the chat extracts a profile (area, role level, work mode,
job-search status) and saves the contact into the Notion database
"Hledáme chytré lidi". CV files are emailed to the recruiter inbox.

Standalone project, separate from Alister (its own Anthropic and Notion keys).

## Structure

```
backend/    FastAPI server: /chat calls Claude, /submit writes to Notion + emails the CV
frontend/   React widget, builds into one embeddable script
HANDOVER.md instructions for the web developer (embedding)
```

The two parts are deployed separately:
- The **backend** is hosted by Miton (needs API keys, so it cannot live in the browser).
- The **frontend** widget is embedded on the site by the web developer and points at the backend URL.

## Backend: run locally

```
cd backend
pip install -r requirements.txt
cp .env.example .env      # fill in the keys
uvicorn app:app --reload --port 8000
```

Check http://localhost:8000/health returns `{"ok": true}`.

Environment variables are documented in `backend/.env.example`. Only
`ANTHROPIC_API_KEY` is strictly required to start. Without `NOTION_TOKEN`,
submissions are saved to `submissions.jsonl` only. Without the SMTP variables,
the CV email is skipped (everything else still works).

### Notion (one-time, already done in production)

1. https://www.notion.so/my-integrations, create an internal integration, copy the token (`ntn_...`).
2. Open the "Hledáme chytré lidi" database, then (...) -> Connections -> add the integration.
3. The database has a text column `Summary` where the AI candidate summary is written
   (together with the candidate's note, CV filename and the GDPR consent timestamp).
4. The database id is pre-filled in `.env.example`.

The chat only writes allowlisted values into the select columns, so it never creates
new options. Columns the backend writes to (do not rename/delete/retype them):
`Message`, `Inzerát`, `Status`, `Jméno`, `E-mail`, `LinkedIn`, `Summary`, `Oblast`,
`Level`, `Remote?`, `Aktivita hledání`. Adding new columns, hiding columns in views,
formulas and new views are all safe.

### Deploy the backend

Production runs on **Railway** (root directory `backend`, start command from the
`Procfile`): https://miton-talent-chat-production.up.railway.app

The backend serves everything on one URL: the widget page at `/` (embedded on the
site via `<iframe>`, `?lang=en` for English), the API (`/chat`, `/submit`), `/health`
and a config self-check at `/diag` (booleans and status codes only, no secrets).
Email is delivered through Resend's HTTPS API (Railway blocks outbound SMTP ports).

Any other Python host works the same way: `uvicorn app:app --host 0.0.0.0 --port $PORT`
plus the environment variables from `.env.example`.

## Frontend

See `HANDOVER.md`. Short version:

```
cd frontend
npm install
npm run dev       # local preview at http://localhost:5173
npm run build     # produces dist/miton-talent-chat.js
```

## Before go-live

- [ ] Railway: upgrade from the trial to the Hobby plan so the service stays up.
- [ ] Resend: verify the miton.cz domain, then switch `MAIL_FROM` from
      `onboarding@resend.dev` to a miton.cz address.
- [ ] Confirm the privacy policy URL and legal entity name in the consent text
      (`PRIVACY_URL` in `frontend/src/MitonTalentChat.jsx`). Legal review recommended
      (the notice should also mention AI processing and the processors used).
- [ ] Optionally tune the per-IP rate limits (`CHAT_RATE_LIMIT` etc., see `.env.example`).

## Safety limits (abuse protection)

- Per-IP rate limits: chat 30 messages / 10 min, form 5 submissions / 10 min
  (X-Forwarded-For hardened, spoofing does not bypass them).
- Cost caps per conversation: max 800 output tokens, max 12 user turns (soft close
  from turn 5), messages trimmed to 2000 chars, history to the last 20 messages.
- Off-topic questions are steered back to careers by the system prompt.
- The backend is isolated: it only calls Anthropic, Notion and Resend. The email
  recipient is fixed via env (`MAIL_TO`); the candidate never receives a copy.
- GDPR consent is enforced server-side (HTTP 400 without it) and stored with the
  exact consent text and a UTC timestamp.
- The embed page sends `Content-Security-Policy: frame-ancestors` restricted to the
  Miton domains, so the chat cannot be iframed on foreign sites.
- CVs are size-capped (8 MB), held in memory only and forwarded as an email
  attachment; they are never written to disk.

## How replies stay well-formed

The model returns its answer through a forced tool call (`record_reply`), so the
reply text, extracted profile, running summary and conversation stage arrive as a
validated object. Raw JSON can never leak into the chat bubble, and profile values
are constrained to the exact Notion option strings.

## Support

Markéta Pařízek, marketa.parizek@miton.cz
