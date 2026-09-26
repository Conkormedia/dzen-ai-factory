"""One-off: hand ONE real queued 'ready' article to a human to get past
Dzen's "Я не робот" captcha gate, over noVNC on the SAME persistent browser
profile/cookies the automated pipeline uses. This script never touches the
captcha checkbox itself - it prepares the draft (content + images pasted,
autosaved) and opens the "Публикация" panel, then just watches the URL
while a human finishes the checkbox + final click live over VNC.

Usage: python -m autopilot.manual_publish_portal
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .db import Database
from .dzen_client import DzenClient, publish_full_article

log = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 1800
POLL_SECONDS = 3


class _Proc:
    def __init__(self, name: str, args: list[str], **kwargs):
        self.name = name
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()


def run() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    db = Database(settings.db_path)
    publisher_id = db.get_setting("dzen_publisher_id", "")
    if not publisher_id:
        print("[manual] нет dzen_publisher_id — сначала нужен вход")
        return

    ready = db.ready_articles()
    if not ready:
        print("[manual] нет статей со статусом ready")
        return
    article_stub = ready[0]
    projects = {p["id"]: p for p in db.projects()}
    project = projects.get(article_stub["project_id"])
    article = db.claim_article(article_stub["project_id"])
    if not article:
        print("[manual] не удалось claim'ить статью (гонка с другим процессом?)")
        return
    print(f"[manual] project={project['slug'] if project else '?'} article #{article['id']}: {article['title']!r}")

    tags = json.loads(article.get("tags_json") or "[]")
    images_meta = json.loads(article.get("images_json") or "[]")
    inline_paths = [Path(x["path"]) for x in images_meta if x.get("path")]
    cover = Path(article["cover_path"]) if article.get("cover_path") else None

    password = secrets.token_hex(4)
    procs: list[_Proc] = []
    headed_settings = settings.__class__(**{**settings.__dict__, "headless": False})

    try:
        procs.append(_Proc("xvfb", ["Xvfb", settings.login_display, "-screen", "0", "1366x850x24",
                                     "-nolisten", "tcp"]))
        time.sleep(1.5)
        procs.append(_Proc("x11vnc", ["x11vnc", "-display", settings.login_display, "-forever", "-shared",
                                       "-quiet", "-rfbport", str(settings.login_vnc_port), "-passwd", password]))
        time.sleep(1.0)
        procs.append(_Proc("websockify", ["websockify", "--web", settings.novnc_web_root,
                                           f"{settings.login_bind_addr}:{settings.login_novnc_port}",
                                           f"localhost:{settings.login_vnc_port}"]))
        time.sleep(1.0)

        print("[manual] === ссылка для тебя ===")
        print(f"[manual] 1) ssh -L {settings.login_novnc_port}:localhost:{settings.login_novnc_port} robocall-server")
        print(f"[manual] 2) http://localhost:{settings.login_novnc_port}/vnc.html"
              f"?autoconnect=true&resize=scale&password={password}")
        print("[manual] 3) в открывшемся окне: отметь 'Я не робот', жми 'Опубликовать' (не 'позже')")
        print("[manual] ==========================")

        os.environ["DISPLAY"] = settings.login_display
        with DzenClient(headed_settings) as client:
            result = publish_full_article(
                client, publisher_id, title=article["title"], markdown=article["markdown"],
                description=article["description"], tags=tags, cover_path=cover,
                inline_image_paths=inline_paths, mode="draft",
            )
            print(f"[manual] draft ready, publication_id={result['publication_id']}, content+images pasted")

            client._page.get_by_role("button", name="Опубликовать").first.click(force=True, timeout=10000)  # noqa: SLF001
            print("[manual] opened 'Публикация' panel - waiting for a human to finish it over VNC...")

            deadline = time.time() + MAX_WAIT_SECONDS
            published_url = ""
            while time.time() < deadline:
                time.sleep(POLL_SECONDS)
                try:
                    url = client._page.url  # noqa: SLF001
                except Exception:  # noqa: BLE001
                    print("[manual] page/context gone (window closed?) - stopping")
                    break
                if "/a/" in url:
                    published_url = url
                    break

        if published_url:
            db.update_article(
                article["id"], status="published", dzen_publication_id=result["publication_id"],
                dzen_url=published_url, publish_mode="publish",
                published_at=datetime.now(timezone.utc).isoformat(),
            )
            db.add_run("publish", "ok", project_id=article["project_id"], article_id=article["id"],
                       note=article["title"][:200])
            print(f"[manual] SUCCESS: {published_url}")
        else:
            db.update_article(article["id"], status="ready", last_error="manual portal: timed out waiting for human")
            print("[manual] TIMED OUT - article reverted to status=ready, nothing published")
    finally:
        for p in reversed(procs):
            p.stop()


if __name__ == "__main__":
    run()
