# Miton Talent

Miton's private recruiting back office. One small service, one database,
three jobs:

1. **Talent chat**: the AI chat on miton.cz/kariera. Visitors talk, leave a
   contact and a CV; the submission lands in the candidate database and in
   the recruiter's inbox.
2. **Candidates and searches**: every person Miton is in contact with about a
   role, what stage they are in, which founder saw them and what the founder
   said, who was messaged and when to follow up. Replaces the Notion
   databases "Hledáme chytré lidi", "Full databáze kandidátů",
   "Sdílení searchů" and "Outreach table".
3. **Monitoring** (planned, moving in from Alister): the TOP 300 watch list
   and the weekly scrape of open roles across the portfolio.

It deliberately is **not** part of [Alister](https://alisterai.com). Alister
holds facts about the market and is a product. Miton Talent holds Miton's
relationships and decisions and is private. Miton Talent reads from Alister
through Alister's API; Alister never knows this service exists. The
reasoning is in [docs/miton-layer-architecture.md](docs/miton-layer-architecture.md),
the data inventory and migration plan in
[docs/notion-replacement-analysis.md](docs/notion-replacement-analysis.md).

## Layout

```
backend/                FastAPI service (runs on the Hetzner server next to Alister)
  app.py                the chat: prompts, /chat, /submit, e-mail, scoring
  talent/               the back office
    models.py           candidates, candidate_events, searches, search_candidates
    store.py            every write and read path (vocab checks, event log)
    vocab.py            stages, outcomes, event types, chat vocabularies
    db.py               engine + session; DATABASE_URL or local SQLite
  alembic/              schema migrations (`alembic upgrade head` runs on deploy)
  tests/                pytest, SQLite in memory
  static/               built chat widget + fonts served by the backend
frontend/               the chat widget (Vite + React)
scripts/
  export_notion.py      dump the Notion databases to JSONL before cancellation
  import_notion_export.py  load that dump into the database (re-runnable)
deploy/                 Hetzner install: systemd unit, nginx vhost, setup, deploy, backup
docs/                   context, analysis, architecture
```

## Run locally

```bash
cd backend
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/alembic upgrade head            # creates ./miton_talent.db (SQLite)
ANTHROPIC_API_KEY=sk-... .venv/bin/uvicorn app:app --reload --port 8000
ANTHROPIC_API_KEY=test .venv/bin/python -m pytest -q tests
```

Without `DATABASE_URL` the service uses a local SQLite file.

## Deploy (Hetzner)

Runs on the same server as Alister, as its own systemd service on port 8200
with its own Postgres database `miton_talent` and its own nginx vhost
`talent.miton.cz`. Nothing is shared with Alister except the machine.

```bash
# first time, as root on the server
# the server clones through its own deploy key (ssh alias github-miton-talent, see deploy/setup_server.sh)
git clone git@github-miton-talent:marketaparizek/miton-talent.git /root/live/miton-talent
cd /root/live/miton-talent && bash deploy/setup_server.sh
certbot --nginx -d talent.miton.cz
# then fill the API keys into /root/live/miton-talent/.env and: systemctl restart miton-talent
# No miton.cz DNS record yet? Put DOMAIN=talent.alisterai.com into .env before running
# setup_server.sh and use that name with certbot; switch later, nothing else changes.

# every later release
cd /root/live/miton-talent && bash deploy/deploy.sh
```

Backups: `deploy/backup_db.sh` runs daily at 03:15 (cron installed by the
setup script), keeps 30 days under `/root/db_backups/miton_talent`, and runs
`OFFSITE_CMD` from `.env` if set. The same rule as Alister prod applies: agents
only pull, restart and restore here; code changes happen locally first.

## Environment

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY`, `MODEL` | the chat model |
| `DATABASE_URL` | Postgres on the server (`deploy/env.example`); omitted locally |
| `RESEND_API_KEY` or `SMTP_*`, `MAIL_FROM`, `MAIL_TO` | CV e-mail to the recruiter |
| `ALLOWED_ORIGINS` | miton.cz origins allowed to embed the widget |
| `SCORING_ENABLED`, `SCORE_MODEL` | automatic candidate scoring after submit |
| `NOTION_TOKEN`, `NOTION_DATABASE_ID` | **transitional**: while set, every submission is also written to Notion. Remove after the Notion import is done. |

## Migration status

- [x] Schema and store (candidates, events, searches)
- [x] Chat writes to the database; Notion dual-write while `NOTION_TOKEN` is set
- [ ] Notion export (`scripts/export_notion.py`, needs `NOTION_TOKEN`)
- [ ] Notion import into the database
- [ ] Admin (Google Sheet mirror first, server-rendered pages next)
- [ ] Founder share pages (`/s/<token>`)
- [ ] MCP server for the Claude skills (triage, search tables, outreach)
- [ ] Watch list and portfolio roles moved in from Alister
