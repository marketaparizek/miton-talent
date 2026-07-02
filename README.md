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

### Notion (one-time)

1. https://www.notion.so/my-integrations, create an internal integration, copy the token (`ntn_...`).
2. Open the "Hledáme chytré lidi" database, then (...) -> Connections -> add the integration.
3. The database id is pre-filled in `.env.example`.

The chat only writes values that already exist in the Notion options, so it never
creates new options.

### Deploy the backend

Any host that runs a Python web app works (Render, Railway, Fly.io, a small VPS).
Start command:

```
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Set the same environment variables there. After deploying, note the public URL
(e.g. `https://miton-talent.onrender.com`) for the frontend.

## Frontend

See `HANDOVER.md`. Short version:

```
cd frontend
npm install
npm run dev       # local preview at http://localhost:5173
npm run build     # produces dist/miton-talent-chat.js
```

## Before go-live

- [ ] Deploy the backend and set its URL on the embed div (`data-backend`).
- [ ] `ALLOWED_ORIGINS` restricted to the Miton domains (default already does this).
- [ ] Confirm the privacy policy URL and legal entity name in the consent text
      (`PRIVACY_URL` in `frontend/src/MitonTalentChat.jsx`). Legal review recommended.
- [ ] Set the recruiter inbox (`MAIL_TO`) and SMTP credentials for CV delivery.

## Support

Markéta Pařízek, marketa.parizek@miton.cz
