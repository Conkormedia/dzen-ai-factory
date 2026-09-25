"""Manual one-shot test: write + publish exactly one article for one project,
bypassing the daily pacing windows. Reuses the exact same code path as the
live service (main._prefetch_one / main._publish_one) — nothing duplicated.

The live systemd service MUST be stopped first: both processes would try to
hold the same Playwright profile lock otherwise.

Usage:
    systemctl stop dzen-autopilot
    /opt/dzen-autopilot/.venv/bin/python -m autopilot.testrun beatscope
    systemctl start dzen-autopilot
"""
from __future__ import annotations

import sys

from . import main as autopilot_main
from .config import settings
from .db import Database
from .dzen_client import DzenClient
from .llm import LLM
from .telegram import Telegram


def run(slug: str) -> None:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    autopilot_main.seed_projects(db, settings)

    project = db.project(slug)
    if not project:
        available = ", ".join(p["slug"] for p in db.projects())
        raise SystemExit(f"unknown project {slug!r}; available: {available}")

    publisher_id = db.get_setting("dzen_publisher_id", "")
    if not publisher_id or db.get_setting("dzen_login_complete", "") != "1":
        raise SystemExit("Dzen login not complete yet (dzen_publisher_id/dzen_login_complete unset)")

    llm = LLM(settings, db)
    tg = Telegram(settings.telegram_bot_token, db.get_setting("telegram_chat_id", ""))
    print(f"[testrun] project={project['name']} provider={llm.provider} model={llm.model} fast={llm.model_fast}")

    existing = db.ready_articles(project["id"])
    if existing:
        article = existing[0]
        print(f"[testrun] reusing already-ready article #{article['id']} {article['title']!r} "
              f"({article['chars']} chars) instead of writing a new one")
    else:
        print("[testrun] researching + writing (this can take a couple of minutes)...")
        ok = autopilot_main._prefetch_one(llm, db, settings, project)
        if not ok:
            raise SystemExit("[testrun] no article produced - check the log lines above for why")
        article = db.query(
            "SELECT id,title,chars,quality_json FROM articles WHERE project_id=? AND status='ready' "
            "ORDER BY id DESC LIMIT 1",
            (project["id"],),
        )[0]
        print(f"[testrun] wrote article #{article['id']} {article['title']!r} ({article['chars']} chars)")
        print(f"[testrun] quality: {article['quality_json']}")

    print("[testrun] publishing to Dzen...")
    with DzenClient(settings) as client:
        published = autopilot_main._publish_one(db, settings, tg, client, publisher_id, project)

    if published:
        row = db.article(article["id"])
        print(f"[testrun] PUBLISHED: {row['dzen_url']}")
    else:
        print("[testrun] publish failed - check the log lines above / article.last_error")
        row = db.article(article["id"])
        print(f"[testrun] last_error: {row.get('last_error')!r}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m autopilot.testrun <project_slug>")
    run(sys.argv[1])
