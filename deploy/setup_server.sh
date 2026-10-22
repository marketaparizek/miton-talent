#!/usr/bin/env bash
#
# First-time install of Miton Talent on the Hetzner server, next to Alister.
# Idempotent: safe to re-run. Run as root on the server:
#
#   git clone git@github-miton-talent:marketaparizek/miton-talent.git /srv/miton-talent
#   (github-miton-talent = ssh alias in /root/.ssh/config using the repo deploy key
#    /root/.ssh/id_miton_talent; the server's default key is bound to the Alister repo)
#   ln -sfn /srv/miton-talent /root/live/miton-talent   # old path keeps working for people and cron
#   cd /srv/miton-talent && bash deploy/setup_server.sh
#
# What it does:
#   1. Postgres: role + database `miton_talent` (own database, not a schema in alister_prod)
#   2. .env from deploy/env.example with a generated DB password (if .env is missing)
#   3. Python 3.11 venv via uv (interpreter under /opt, readable by the service), dependencies
#   4. system user miton-talent, file ownership, systemd unit (port 8200, sandboxed), started
#   5. nginx vhost for talent.miton.cz (TLS: run certbot afterwards, see below)
#   6. daily backup cron (03:15) -> /root/db_backups/miton_talent
#   7. weekly portfolio scrape cron (Monday 06:10) -> the /admin/portfolio dashboard
#
# Afterwards, by hand, once:
#   certbot --nginx -d talent.miton.cz
#   fill in ANTHROPIC_API_KEY, RESEND_API_KEY etc. in .env, then: systemctl restart miton-talent
set -euo pipefail

APP_DIR="/srv/miton-talent"
SVC_USER="miton-talent"
# uv's own Python lives under /root/.local by default, where the service user
# cannot reach it; a venv built on it would not start under the sandboxed unit.
export UV_PYTHON_INSTALL_DIR="/opt/uv-python"
DB_NAME="miton_talent"
DB_USER="miton_talent"
PORT=8200
# Public hostname. Set DOMAIN in .env to override (e.g. talent.alisterai.com until
# the miton.cz DNS record exists). The app does not care which name it is served on.
export PATH="/root/.local/bin:$PATH"

cd "$APP_DIR"
mkdir -p logs
id -u "$SVC_USER" >/dev/null 2>&1 || \
  useradd --system --user-group --home-dir /var/lib/miton-talent --no-create-home --shell /usr/sbin/nologin "$SVC_USER"

# 1. database ---------------------------------------------------------------
if [[ ! -f .env ]]; then
  pw="$(openssl rand -hex 24)"
  sed "s#CHANGE_ME#${pw}#" deploy/env.example > .env
  chmod 600 .env
  echo "wrote .env with a generated database password; fill in the API keys"
fi
set -a; source .env; set +a
DOMAIN="${DOMAIN:-talent.miton.cz}"
db_pw="$(sed -n 's#^DATABASE_URL=postgresql+psycopg://[^:]*:\([^@]*\)@.*#\1#p' .env)"

if ! sudo -u postgres psql -tAc "select 1 from pg_roles where rolname='${DB_USER}'" | grep -q 1; then
  sudo -u postgres psql -c "create role ${DB_USER} login password '${db_pw}'"
  echo "created role ${DB_USER}"
else
  sudo -u postgres psql -c "alter role ${DB_USER} password '${db_pw}'" >/dev/null
fi
if ! sudo -u postgres psql -tAc "select 1 from pg_database where datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
  echo "created database ${DB_NAME}"
fi

# 2. python -----------------------------------------------------------------
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
cd backend
[[ -d .venv ]] || uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cd ..

# Code stays root-owned (the service cannot rewrite what it runs); the service
# group may read it; only logs/ and the submissions fallback are its own.
chown -R root:"$SVC_USER" "$APP_DIR"
chmod -R g+rX,o-rwx "$APP_DIR"
chown root:root .env && chmod 600 .env
touch backend/submissions.jsonl
chown -R "$SVC_USER":"$SVC_USER" logs backend/submissions.jsonl
[[ -d /opt/uv-python ]] && chmod -R a+rX /opt/uv-python   # an interpreter, nothing secret
install -m 644 deploy/logrotate-miton-talent /etc/logrotate.d/miton-talent

# 3. systemd ----------------------------------------------------------------
install -m 644 deploy/miton-talent.service /etc/systemd/system/miton-talent.service
systemctl daemon-reload
systemctl enable miton-talent >/dev/null
systemctl restart miton-talent   # ExecStartPre runs alembic upgrade head
sleep 3
curl -fsS "http://127.0.0.1:${PORT}/health" && echo " <- service answers on :${PORT}"

# 4. nginx ------------------------------------------------------------------
if [[ ! -f "/etc/nginx/sites-available/${DOMAIN}" ]]; then
  sed "s#__DOMAIN__#${DOMAIN}#g" deploy/nginx-talent.miton.cz.conf > "/etc/nginx/sites-available/${DOMAIN}"
  ln -sf "/etc/nginx/sites-available/${DOMAIN}" "/etc/nginx/sites-enabled/${DOMAIN}"
  nginx -t && systemctl reload nginx
  echo "nginx vhost installed for ${DOMAIN}; now run: certbot --nginx -d ${DOMAIN}"
fi

# 5. backup cron ------------------------------------------------------------
cron_line="15 3 * * * cd ${APP_DIR} && bash deploy/backup_db.sh >> logs/backup.log 2>&1"
( crontab -l 2>/dev/null | grep -v "miton-talent && bash deploy/backup_db.sh" ; echo "$cron_line" ) | crontab -
echo "backup cron installed (daily 03:15)"

# 6. weekly portfolio scrape -------------------------------------------------
# Monday morning, before anyone looks at the dashboard or writes the Slack digest.
# Reads every portfolio company's careers page; unread pages keep last week's
# roles, so a bad Monday costs a note on the page and nothing else.
# cron gets none of the service's environment, so .env is loaded by hand:
# without DATABASE_URL the run would seed a local SQLite file, and without
# ANTHROPIC_API_KEY it could not read the careers pages that are not an ATS.
scrape_line="10 6 * * 1 cd ${APP_DIR} && set -a && . ./.env && set +a && backend/.venv/bin/python scripts/scrape_portfolio.py --actor cron >> logs/portfolio.log 2>&1"
( crontab -l 2>/dev/null | grep -v "scripts/scrape_portfolio.py" ; echo "$scrape_line" ) | crontab -
echo "portfolio scrape cron installed (Mondays 06:10)"

echo "done. logs: ${APP_DIR}/logs/app.log   status: systemctl status miton-talent"
