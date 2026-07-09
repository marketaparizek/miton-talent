# Handover: deploy and embed

For the web developer. The project has two parts. You deploy both:

1. The **backend** (`backend/`) is a small FastAPI server. It needs API keys, so
   it cannot live in the browser and must run on a host.
2. The **frontend** (`frontend/`) is a self-contained widget with no external CSS
   dependency. You build it into one file and drop it on the career page.

Markéta will send you the secret values (see "Secrets" below).

## Part 1: deploy the backend

Any host that runs a Python web app works (Render, Railway, Fly.io, a small VPS).

- Root/working dir: `backend`
- Install: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`

After deploying, open `https://YOUR-BACKEND-URL/health` and confirm it returns
`{"ok": true}`. Note that URL, the frontend needs it.

### Environment variables

Set these on the host (in its environment variables, never in the code or repo).

Required:
- `ANTHROPIC_API_KEY` — secret, from Markéta
- `NOTION_TOKEN` — secret, from Markéta

Notion (already has a sensible default, change only if needed):
- `NOTION_DATABASE_ID` — pre-filled to the "Hledáme chytré lidi" database

CORS (default already restricts to the Miton domains):
- `ALLOWED_ORIGINS` — `https://www.miton.cz,https://miton.cz`

Email / CV delivery (using Resend over SMTP):
- `SMTP_HOST` — `smtp.resend.com`
- `SMTP_PORT` — `587`
- `SMTP_USER` — `resend`
- `SMTP_PASS` — secret, the Resend `re_...` key from Markéta
- `MAIL_FROM` — `onboarding@resend.dev` for now; switch to an address on
  `miton.cz` once that domain is verified in Resend
- `MAIL_TO` — `marketa.parizek@miton.cz`

Optional:
- `MODEL` — the Claude model, defaults to `claude-sonnet-4-6`. Change here if you
  ever need a different model; no code edit required.
- `CHAT_RATE_LIMIT` / `CHAT_RATE_WINDOW` — per-IP chat limit, default 30 messages
  per 600 s. `SUBMIT_RATE_LIMIT` / `SUBMIT_RATE_WINDOW` — per-IP form limit,
  default 5 submissions per 600 s. Protects against spam burning API tokens.

Notes on graceful degradation: without `NOTION_TOKEN` the backend stores
submissions in a local `submissions.jsonl` only. Without the SMTP variables the
CV email is skipped. Everything else still works in both cases.

One-time Notion step (Markéta): in the "Hledáme chytré lidi" database, rename the
text column `Notes` to `Summary`. The backend writes the AI candidate summary
there; until renamed, rows are created without the summary (a warning is logged).

### Secrets

Three values are secret: `ANTHROPIC_API_KEY`, `NOTION_TOKEN` and `SMTP_PASS`. The
rest of the variables above are not secret.

If you (the developer) deploy the backend, Markéta will send these three via a
onetimesecret.com link (one-time, self-deleting), never in this repo or by plain
email. If Markéta deploys the backend herself, she sets them directly on the host
and you do not need them at all.

## Part 2: embed on the site

### Option A (recommended): iframe, one URL

The backend serves the whole widget page itself at `/`. The site only needs:

```html
<iframe
  src="https://YOUR-BACKEND-URL"
  title="Miton talent"
  style="width:100%;max-width:720px;height:720px;border:0;"
  loading="lazy"
></iframe>
```

- English version: `src="https://YOUR-BACKEND-URL/?lang=en"`.
- No CORS setup, no files to host, nothing to rebuild on the site. New versions
  go live by redeploying the backend.
- The embedded page is `backend/static/embed.html`; the widget bundle is served
  from `backend/static/miton-talent-chat.js`. After changing the frontend, rebuild
  and refresh the copy:

```
cd frontend && npm run build && cp dist/miton-talent-chat.js ../backend/static/
```

### Option B: native widget embed (no iframe)

Build the bundle and drop it on the page; the widget then blends into the site's
layout (no iframe border constraints):

```
cd frontend
npm install
npm run build
```

Output: `frontend/dist/miton-talent-chat.js` (one file, React bundled in).

```html
<div
  id="miton-talent-chat"
  data-backend="https://YOUR-BACKEND-URL"   <!-- the deployed backend URL -->
  data-lang="cs"                            <!-- "cs" or "en" -->
  style="max-width:640px;height:600px;margin:0 auto"
></div>
<script src="/path/to/miton-talent-chat.js"></script>
```

Notes:
- `data-backend` is required here (or set `VITE_BACKEND_URL` before building);
  with this option, `ALLOWED_ORIGINS` on the backend must include the site's domain.
- Size and position come from the div. Only one widget per page.

### If the site already uses React

Skip the standalone build and import the component directly:

```jsx
import MitonTalentChat from "./MitonTalentChat.jsx";

<MitonTalentChat backendUrl="https://YOUR-BACKEND-URL" defaultLang="cs" height="600px" />
```

## Placement on the career page

Hero section as the dominant element. On desktop: headline and reasons above,
the chat centered below. On mobile: full width below the headline. The widget
renders as a single white card and fills its container; the surrounding hero
(headline, brand shapes) is handled by the page, not the widget.

Hero copy (reference from the design, this text lives on the page, not in the widget):
- Headline: "Zvažujete práci pro startup? Pojďme to probrat."
- Subtitle: "Jsme úspěšná česká investiční skupina s desítkami startupů v portfoliu a pro naše projekty neustále hledáme zvědavé a chytré lidi."
- Three reasons: "Žádný formulář, jen krátká konverzace", "Řekni nám, co tě baví a jakou roli hledáš", "Zkusíme rozhodit síť napříč portfoliem"

## What the widget does

- Talks with the candidate, shows extracted profile chips as the chat goes.
- When it makes sense, shows a short contact form (name, email, LinkedIn, note,
  optional CV) with a GDPR consent checkbox.
- On submit it posts to the backend, which writes a row to Notion and emails the
  CV to the recruiter inbox.

Questions: marketa.parizek@miton.cz
