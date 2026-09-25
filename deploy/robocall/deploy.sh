#!/usr/bin/env bash
# Idempotent deploy/update for the Dzen autopilot on robocall-server.
# Run ON THE SERVER, as root: bash deploy.sh [git-ref]
set -euo pipefail

REPO_URL="https://github.com/Conkormedia/dzen-ai-factory.git"
REF="${1:-main}"
APP_DIR=/opt/dzen-autopilot/app
VENV=/opt/dzen-autopilot/.venv
ENV_FILE=/etc/dzen-autopilot.env

echo "== code =="
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth 1 origin "$REF"
  git -C "$APP_DIR" checkout -f FETCH_HEAD
else
  rm -rf "$APP_DIR"
  git clone --depth 1 --branch "$REF" "$REPO_URL" "$APP_DIR"
fi

echo "== venv =="
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip wheel
"$VENV/bin/pip" install -q -r "$APP_DIR/autopilot/requirements.txt"
"$VENV/bin/playwright" install --with-deps chromium

echo "== data dirs =="
mkdir -p /opt/dzen-autopilot/data/profile /opt/dzen-autopilot/data/images
chmod 700 /opt/dzen-autopilot/data/profile

echo "== env file =="
if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/deploy/robocall/dzen-autopilot.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE from template — fill TELEGRAM_BOT_TOKEN (and an LLM key) with configure.sh"
fi

echo "== systemd =="
cp "$APP_DIR/deploy/robocall/dzen-autopilot.service" /etc/systemd/system/dzen-autopilot.service
cp "$APP_DIR/deploy/robocall/configure.sh" /opt/dzen-autopilot/configure.sh
chmod +x /opt/dzen-autopilot/configure.sh
systemctl daemon-reload
systemctl enable dzen-autopilot

echo "== done =="
echo "Set credentials with:  /opt/dzen-autopilot/configure.sh TELEGRAM_BOT_TOKEN <token>"
echo "Then:                  /opt/dzen-autopilot/configure.sh ANTHROPIC_API_KEY <key>"
echo "Service starts itself once TELEGRAM_BOT_TOKEN is set (systemctl status dzen-autopilot)."
