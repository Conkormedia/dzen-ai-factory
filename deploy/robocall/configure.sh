#!/usr/bin/env bash
# One command to set a credential and (re)start the autopilot.
#
#   ./configure.sh TELEGRAM_BOT_TOKEN 123456:AA...
#   ./configure.sh ANTHROPIC_API_KEY sk-ant-...
#
# Run this ON THE SERVER (robocall-server), as root.
set -euo pipefail

ENV_FILE=/etc/dzen-autopilot.env
KEY="${1:?usage: configure.sh KEY VALUE}"
VALUE="${2:?usage: configure.sh KEY VALUE}"

if [ ! -f "$ENV_FILE" ]; then
  echo "no $ENV_FILE yet — run deploy.sh first" >&2
  exit 1
fi

if grep -q "^${KEY}=" "$ENV_FILE"; then
  sed -i "s|^${KEY}=.*|${KEY}=${VALUE}|" "$ENV_FILE"
else
  echo "${KEY}=${VALUE}" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

echo "Set ${KEY}. Restarting dzen-autopilot…"
systemctl restart dzen-autopilot
sleep 2
systemctl --no-pager status dzen-autopilot | head -10
echo
echo "Watch it with: journalctl -u dzen-autopilot -f"
