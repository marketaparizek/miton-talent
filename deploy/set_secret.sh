#!/usr/bin/env bash
#
# Put a secret into /root/live/miton-talent/.env without it ever appearing in a
# command line, chat or shell history. Prompts for the value (hidden input).
#
#   ssh root@46.225.26.134 'bash /root/live/miton-talent/deploy/set_secret.sh ANTHROPIC_API_KEY'
#   ssh root@46.225.26.134 'bash /root/live/miton-talent/deploy/set_secret.sh RESEND_API_KEY'
#
# Restarts the service afterwards so the new value is live.
set -euo pipefail
ENV_FILE="/root/live/miton-talent/.env"
name="${1:?usage: set_secret.sh VARIABLE_NAME}"
[[ "$name" =~ ^[A-Z0-9_]+$ ]] || { echo "bad variable name" >&2; exit 2; }

printf 'Vlož hodnotu pro %s (nic se nezobrazí) a stiskni Enter: ' "$name" >&2
IFS= read -r -s value
echo >&2
value="${value//[$'\r\n\t ']/}"        # strip stray whitespace from a paste
[[ -n "$value" ]] || { echo "prázdná hodnota, nic se nezměnilo" >&2; exit 1; }

if grep -q "^${name}=" "$ENV_FILE"; then
  # use a delimiter that cannot appear in the value
  python3 - "$ENV_FILE" "$name" "$value" <<'EOF'
import sys, re
path, name, value = sys.argv[1:]
lines = open(path).read().splitlines()
out = [f"{name}={value}" if l.startswith(f"{name}=") else l for l in lines]
open(path, "w").write("\n".join(out) + "\n")
EOF
else
  printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
echo "${name} uloženo (${#value} znaků, začíná ${value:0:6}…)" >&2
systemctl restart miton-talent && sleep 2 && curl -fsS http://127.0.0.1:8200/diag >&2 && echo >&2
