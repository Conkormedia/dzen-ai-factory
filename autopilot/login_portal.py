"""One-time remote Dzen login: opens a real (non-headless) browser on a
virtual display, exposes it over noVNC so the operator can log in from any
browser, and blocks until a session is detected. Never touches credentials
itself — the human types them into the real dzen.ru page over the VNC feed.

Only ONE process may hold the Chromium ``user_data_dir`` lock at a time, so
this must not run concurrently with the headless publisher (``main.py``
calls this to completion *before* starting the headless publish loop).
"""
from __future__ import annotations

import logging
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

from .config import Settings
from .dzen_client import DzenClient
from .telegram import Telegram

log = logging.getLogger(__name__)

POLL_SECONDS = 5
NUDGE_EVERY_SECONDS = 15 * 60


class _Proc:
    """Tracks a subprocess and guarantees it's killed on teardown."""

    def __init__(self, name: str, args: list[str], **kwargs):
        self.name = name
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.alive():
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()


def marker_path(settings: Settings) -> Path:
    return settings.data_dir / "dzen_login.json"


def ensure_login(settings: Settings, db, tg: Telegram) -> str:
    """Returns the publisher_id, fast-pathing if a saved session is still valid."""
    publisher_id = db.get_setting("dzen_publisher_id", "")
    if publisher_id and db.get_setting("dzen_login_complete", "") == "1":
        try:
            with DzenClient(settings) as client:
                info = client.check_session()
            if info.get("logged_in") and info.get("publisher_id"):
                return info["publisher_id"]
            log.warning("saved Dzen session no longer valid (%s); re-opening login portal", info)
        except Exception:  # noqa: BLE001
            log.exception("headless re-check failed; re-opening login portal")

    return _run_login_portal(settings, db, tg)


def _run_login_portal(settings: Settings, db, tg: Telegram) -> str:
    # Classic VNC auth (DES-keyed challenge) only uses the first 8 bytes of the
    # password; anything longer risks a client/server truncation mismatch.
    password = secrets.token_hex(4)
    procs: list[_Proc] = []
    headed_settings = Settings(**{**settings.__dict__, "headless": False})

    try:
        procs.append(_Proc("xvfb", ["Xvfb", settings.login_display, "-screen", "0", "1366x850x24", "-nolisten", "tcp"]))
        time.sleep(1.5)
        procs.append(_Proc(
            "x11vnc",
            ["x11vnc", "-display", settings.login_display, "-forever", "-shared", "-quiet",
             "-rfbport", str(settings.login_vnc_port), "-passwd", password],
        ))
        time.sleep(1.0)
        procs.append(_Proc(
            "websockify",
            ["websockify", "--web", settings.novnc_web_root,
             f"{settings.login_bind_addr}:{settings.login_novnc_port}", f"localhost:{settings.login_vnc_port}"],
        ))
        time.sleep(1.0)

        link = _login_link(settings, password)
        db.log_event(f"Login portal opened, bind={settings.login_bind_addr}", kind="login")
        tg.safe_send(_login_message(settings, password, link))

        os.environ["DISPLAY"] = settings.login_display
        with DzenClient(headed_settings) as client:
            client.goto("https://dzen.ru/profile/editor", wait_ms=2000)  # one-time; nothing else is on the page yet

            publisher_id = ""
            last_nudge = time.time()
            while not publisher_id:
                time.sleep(POLL_SECONDS)
                # peek, never goto(): the human may be mid-navigation (passport.yandex
                # redirects etc.) — forcing a navigation here raced their own clicks
                # and crashed Playwright's frame tracking.
                info = client.peek_session()
                if info.get("captcha"):
                    if time.time() - last_nudge > NUDGE_EVERY_SECONDS:
                        last_nudge = time.time()
                        tg.safe_send("⚠️ Дзен показал капчу в окне входа. Пройди её глазами — окно то же, открывай заново.")
                    continue
                if info.get("logged_in") and info.get("need_channel") and not info.get("publisher_id"):
                    if time.time() - last_nudge > NUDGE_EVERY_SECONDS:
                        last_nudge = time.time()
                        tg.safe_send("ℹ️ Вижу вход, но у аккаунта ещё нет канала Дзена — создай канал в открытом окне, дальше подхвачу сам.")
                    continue
                if info.get("logged_in") and info.get("publisher_id"):
                    publisher_id = info["publisher_id"]
                    break
                if time.time() - last_nudge > NUDGE_EVERY_SECONDS:
                    last_nudge = time.time()
                    tg.safe_send(_login_message(settings, password, link, reminder=True))

        db.set_setting("dzen_publisher_id", publisher_id)
        db.set_setting("dzen_login_complete", "1")
        marker_path(settings).write_text(f'{{"publisher_id": "{publisher_id}"}}', encoding="utf-8")
        tg.safe_send(f"✅ Вход в Дзен подтверждён (канал {publisher_id}). Запускаю генерацию и публикацию.")
        log.info("Dzen login complete, publisher_id=%s", publisher_id)
        return publisher_id
    finally:
        for p in reversed(procs):
            p.stop()


def _login_link(settings: Settings, password: str) -> str:
    """Only meaningful when LOGIN_PUBLIC_URL is set (e.g. a reverse-proxied
    hostname the operator chose to expose). Otherwise unused — see _login_message."""
    base = settings.login_public_url.rstrip("/")
    # No reconnect=true: noVNC would otherwise keep silently re-dialing (and
    # websockify forking a new worker per attempt) on any transient hiccup.
    return f"{base}/vnc.html?autoconnect=true&resize=scale&password={password}"


def _login_message(settings: Settings, password: str, link: str, *, reminder: bool = False) -> str:
    header = "⏳ Ещё жду входа в Дзен." if reminder else "🔐 Нужен вход в Дзен, чтобы публикация заработала."
    if settings.login_public_url:
        return (
            f"{header}\n\n"
            f"Открой ссылку и войди в свой аккаунт dzen.ru как обычно:\n{link}\n\n"
            "Ничего нажимать после входа не нужно — как только увижу активную сессию, "
            "сам закрою окно и запущу генерацию и публикацию."
        )
    # No public URL configured (default, no internet exposure): tunnel over SSH.
    return (
        f"{header}\n\n"
        "Вход не выставлен в интернет — заходим через SSH-туннель с твоего компьютера:\n\n"
        f"1. В терминале на своей машине:\n"
        f"   ssh -L {settings.login_novnc_port}:localhost:{settings.login_novnc_port} robocall-server\n"
        f"   (оставь эту сессию открытой)\n"
        f"2. Открой в браузере:\n"
        f"   http://localhost:{settings.login_novnc_port}/vnc.html?autoconnect=true&resize=scale&password={password}\n"
        "3. Войди в dzen.ru как обычно в открывшемся окне.\n\n"
        "Ничего нажимать после входа не нужно — как только увижу активную сессию, "
        "сам закрою окно и запущу генерацию и публикацию."
    )
