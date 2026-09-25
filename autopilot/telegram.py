"""Telegram side-channel: status notifications, one-time chat-id capture from
the owner's first /start, and the daily digest."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20


class Telegram:
    def __init__(self, bot_token: str, chat_id: str = ""):
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()

    def _call(self, method: str, **params) -> dict:
        if not self.bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
        r = requests.post(API.format(token=self.bot_token, method=method), json=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} ok=false: {data.get('description')}")
        return data.get("result")

    def send(self, text: str, *, disable_preview: bool = True) -> int | None:
        if not self.chat_id:
            log.warning("no chat_id yet, dropping message: %s", text[:80])
            return None
        parts = _split(text)
        last_id = None
        for part in parts:
            result = self._call("sendMessage", chat_id=self.chat_id, text=part,
                                disable_web_page_preview=disable_preview)
            last_id = result.get("message_id")
        return last_id

    def safe_send(self, text: str) -> int | None:
        try:
            return self.send(text)
        except Exception:  # noqa: BLE001
            log.exception("telegram send failed")
            return None

    def send_photo(self, photo_path: Path, caption: str = "") -> int | None:
        if not self.chat_id:
            return None
        with photo_path.open("rb") as fh:
            r = requests.post(
                API.format(token=self.bot_token, method="sendPhoto"),
                data={"chat_id": self.chat_id, "caption": caption[:1024]},
                files={"photo": (photo_path.name, fh, "image/jpeg")},
                timeout=60,
            )
        r.raise_for_status()
        data = r.json()
        return (data.get("result") or {}).get("message_id") if data.get("ok") else None

    def get_me(self) -> dict | None:
        """Cheap token-validity check (no long-poll wait)."""
        try:
            r = requests.post(API.format(token=self.bot_token, method="getMe"), timeout=15)
        except requests.RequestException:
            return None
        if r.status_code == 401:
            raise InvalidToken("Telegram отклонил TELEGRAM_BOT_TOKEN (401) — это не настоящий токен от @BotFather")
        r.raise_for_status()
        data = r.json()
        return data.get("result") if data.get("ok") else None

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        r = requests.post(API.format(token=self.bot_token, method="getUpdates"), json=params,
                          timeout=timeout + 10)
        if r.status_code == 401:
            raise InvalidToken("Telegram отклонил TELEGRAM_BOT_TOKEN (401) — это не настоящий токен от @BotFather")
        r.raise_for_status()
        data = r.json()
        return data.get("result", []) if data.get("ok") else []


class InvalidToken(RuntimeError):
    pass


def _split(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def capture_chat_id(bot_token: str, *, env_path: Path, db, timeout_seconds: int = 20) -> str | None:
    """Long-poll getUpdates once for a message; on success, persist the chat id
    to both the env file (systemd reads it on restart) and the DB settings, and
    ACK the user in chat. Returns the chat id or None if nothing arrived yet."""
    tg = Telegram(bot_token)
    last_update_id = int(db.get_setting("telegram_update_offset", "0") or 0)
    try:
        updates = tg.get_updates(offset=last_update_id + 1 if last_update_id else None,
                                 timeout=timeout_seconds)
    except InvalidToken:
        raise
    except Exception:  # noqa: BLE001
        log.exception("getUpdates failed while waiting for chat id")
        return None
    for update in updates:
        db.set_setting("telegram_update_offset", str(update.get("update_id", last_update_id)))
        message = update.get("message") or update.get("channel_post")
        if not message:
            continue
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            continue
        db.set_setting("telegram_chat_id", chat_id)
        _persist_env(env_path, "TELEGRAM_CHAT_ID", chat_id)
        tg2 = Telegram(bot_token, chat_id)
        tg2.safe_send(
            "🤖 Готово, бот подключён.\n\n"
            "Дальше жду входа в Дзен (ссылку для входа я уже прислал/пришлю отдельно) — "
            "как только сессия будет активна, генерация и публикация статей начнутся автоматически. "
            "Раз в день сюда будет приходить сводка."
        )
        return chat_id
    return None


def _persist_env(env_path: Path, key: str, value: str) -> None:
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ[key] = value
    except OSError:
        log.exception("could not persist %s to %s", key, env_path)
