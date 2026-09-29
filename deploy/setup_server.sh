#!/usr/bin/env bash
#
# First-time install of Miton Talent on the Hetzner server, next to Alister.
# Idempotent: safe to re-run. Run as root on the server:
#
#   git clone git@github-miton-talent:marketaparizek/miton-talent.git /root/live/miton-talent
#   (github-miton-talent = ssh alias in /root/.ssh/config using the repo deploy key
#    /root/.ssh/id_miton_talent; the server's default key is bound to the Alister repo)
#   cd /root/live/miton-talent && bash deploy/setup_server.sh
#
# What it does:
#   1. Postgres: role + database `miton_talent` (own database, not a schema in alister_prod)
#   2. .env from deploy/env.example with a generated DB password (if .env is missing)
#   3. Python 3.11 venv via uv, dependencies, alembic upgrade head
#   4. systemd unit miton-talent.service (port 8200), enabled + started
#   5. nginx vhost for talent.miton.cz (TLS: run certbot afterwards, see below)
#   6. daily backup cron (03:15) -> /root/db_backups/miton_talent
#
# Afterwards, by hand, once:
#   certbot --nginx -d talent.miton.cz
#   fill in ANTHROPIC_API_KEY, RESEND_API_KEY etc. in .env, then: systemctl restart miton-talent
set -euo pipefail

APP_DIR="/root/live/miton-talent"
DB_NAME="miton_talent"
DB_USER="miton_talent"
PORT=8200
# Public hostname. Set DOMAIN in .env to override (e.g. talent.alisterai.com until
# the miton.cz DNS record exists). The app does not care which name it is served on.
export PATH="/root/.local/bin:$PATH"

cd "$APP_DIR"
mkdir -p logs

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
.venv/bin/alembic upgrade head
cd ..

# 3. systemd ----------------------------------------------------------------
install -m 644 deploy/miton-talent.service /etc/systemd/system/miton-talent.service
systemctl daemon-reload
systemctl enable miton-talent >/dev/null
systemctl restart miton-talent
sleep 2
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

echo "done. logs: ${APP_DIR}/logs/app.log   status: systemctl status miton-talent"
