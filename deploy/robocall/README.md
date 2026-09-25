# Dzen Autopilot — deploy on robocall-server

Multi-project SEO article factory with automatic Yandex Dzen publishing.
Lives in `autopilot/` in this repo; this directory only holds the
server-specific glue (systemd unit, env template, Caddy snippet, deploy
script). See [`autopilot/`](../../autopilot) for the actual service code.

## Why this host

`robocall-server` (Frankfurt, DE) reaches dzen.ru, api.telegram.org,
api.anthropic.com, openrouter.ai and image.pollinations.ai directly, no
proxy needed — confirmed at setup time. It already runs Caddy with
Let's-Encrypt TLS on `31-76-40-61.sslip.io` subdomains (see
`/opt/prio-mcp/Caddyfile`), which the login portal reuses.

## One-time setup (already run by the assistant)

```bash
bash deploy.sh          # clones the repo, builds the venv, installs Chromium,
                         # writes /etc/dzen-autopilot.env from the template,
                         # installs + enables the systemd unit
```

Then add the login-portal site to the existing Caddy (see
`caddy-snippet.txt` — appended to `/opt/prio-mcp/Caddyfile`, then
`docker exec prio-mcp-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile`).

## What's left for a human (by design — the service will never do these)

1. **Telegram bot token.** Create a bot with @BotFather, then on the server:
   ```bash
   /opt/dzen-autopilot/configure.sh TELEGRAM_BOT_TOKEN 123456:AA...
   ```
   Send `/start` to the bot from your own Telegram account — the chat id is
   captured automatically and the bot confirms.

2. **An LLM key** (whichever you have): `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`,
   `OPENAI_API_KEY`, or `DEEPSEEK_API_KEY`.
   ```bash
   /opt/dzen-autopilot/configure.sh ANTHROPIC_API_KEY sk-ant-...
   ```
   The bot nags you in Telegram if this is still missing once it's alive.

3. **Log into Dzen.** Once the bot token is set, it sends you a one-time
   `https://dzen-login.31-76-40-61.sslip.io/vnc.html?...` link in Telegram —
   open it, log into your dzen.ru account exactly as you normally would. The
   service detects the session, tears the login window down automatically,
   and starts writing + publishing. The link only works while a login is
   pending; nothing is exposed afterwards.

Nothing else is required — no per-project setup. The three brands (BeatScope,
BizGateWay, PRIO Concierge) are seeded from `autopilot/projects.seed.json` at
first start, 10 articles/day each, spread across 08:00–23:00 Moscow time. A
digest lands in Telegram every day at 21:00 MSK.

## Operating

```bash
systemctl status dzen-autopilot
journalctl -u dzen-autopilot -f
```

`PUBLISH_MODE=draft` in `/etc/dzen-autopilot.env` + restart switches to
saving Dzen drafts instead of publishing live, if you want to review first.
