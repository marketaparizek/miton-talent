#!/usr/bin/env bash
#
# Update the running Miton Talent on the server to the latest main:
#
#   cd /root/live/miton-talent && bash deploy/deploy.sh
#
# pull -> dependencies -> restart (the unit runs `alembic upgrade head` on start)
# -> health check. Takes a safety backup first, because a migration may follow.
set -euo pipefail
export PATH="/root/.local/bin:$PATH"
cd "$(dirname "$0")/.."

bash deploy/backup_db.sh
git pull --ff-only
uv pip install --python backend/.venv/bin/python -r backend/requirements.txt
systemctl restart miton-talent
sleep 3
# The portfolio list (and, the very first time, the 14 Sep 2026 baseline it
# diffs against). An upsert with no network calls: safe on every release.
# .env has to be loaded by hand here: the app gets it from systemd, a script
# started from this shell does not, and without DATABASE_URL it would quietly
# seed a local SQLite file instead of Postgres.
( set -a; . ./.env; set +a; backend/.venv/bin/python scripts/scrape_portfolio.py --seed )
curl -fsS http://127.0.0.1:8200/health && echo " <- healthy"

# /diag is behind the Alister sign-in, so from the server it must REFUSE us.
# A 200 here would mean the back office is open to anyone.
diag=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8200/diag)
case "$diag" in
  401|302|303|307) echo "/diag is behind the sign-in (HTTP $diag) <- ok" ;;
  *) echo "/diag answered HTTP $diag; expected a refusal"; exit 1 ;;
esac
