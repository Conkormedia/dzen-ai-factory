"""Single-browser publish queue with a DB-backed state machine.

Article statuses (articles.status) on the way to Dzen:
  ready            written, waiting its turn
  publishing       claimed; the editor is being filled right now
  awaiting_captcha publish clicked, Dzen asked «Я не робот». The browser tab
                   is left exactly where the human needs it and nothing else
                   touches Dzen until this resolves - re-submitting while a
                   captcha is open is what makes Dzen treat us as a bot
  in_review        left the drafts state but isn't live yet («Ожидают публикации»)
  published        live
  rejected         Dzen refused it
  failed           kept failing without a captcha; never auto-retried

The gate (settings key ``dzen_publish_gate``) names the one article waiting on
a human. Every tick reads it before any browser action.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from . import draft_cleanup
from .db import Database
from .dzen_client import DzenClient

log = logging.getLogger(__name__)

GATE_KEY = "dzen_publish_gate"
NEXT_PUBLISH_KEY = "dzen_next_publish_at"
PUBLISH_GAP_S = (7 * 60, 14 * 60)
CAPTCHA_REMINDER_S = 2 * 3600
MAX_ATTEMPTS = 3
REJECT_MARKERS = ("reject", "block", "ban", "declin")


# ------------------------------------------------------------------ gate
def gate_get(db: Database) -> dict[str, Any] | None:
    raw = db.get_setting(GATE_KEY, "")
    if not raw:
        return None
    try:
        gate = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return gate if gate.get("article_id") else None


def gate_set(db: Database, article_id: int, draft_id: str) -> None:
    now = int(time.time())
    db.set_setting(GATE_KEY, json.dumps({"article_id": article_id, "draft_id": draft_id,
                                         "since": now, "reminded_at": now}))


def gate_clear(db: Database) -> None:
    db.set_setting(GATE_KEY, "")


# ---------------------------------------------------------------- pacing
def publish_due(db: Database, now: float | None = None) -> bool:
    return (now or time.time()) >= float(db.get_setting(NEXT_PUBLISH_KEY, "0") or 0)


def schedule_next_publish(db: Database, now: float | None = None) -> float:
    at = (now or time.time()) + random.uniform(*PUBLISH_GAP_S)
    db.set_setting(NEXT_PUBLISH_KEY, str(int(at)))
    return at


def pick_project(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """candidates: [{"project":..., "published_today": int, "quota": int}] that
    are under target and have a ready article. The one furthest behind its
    quota goes first, so the three projects interleave instead of bursting."""
    if not candidates:
        return None
    best = min(c["published_today"] / max(1, c["quota"]) for c in candidates)
    tied = [c for c in candidates if c["published_today"] / max(1, c["quota"]) == best]
    return random.choice(tied)["project"]


# --------------------------------------------------------------- probing
def publication_state(client: DzenClient, publisher_id: str, draft_id: str) -> tuple[str, str]:
    """Where a submitted draft stands on Dzen: ("published", url),
    ("rejected", reason), ("draft", "") or ("in_review", "")."""
    for page in list(client._ctx.pages):  # noqa: SLF001 - the human may have published from this tab
        if "/a/" in (page.url or "") and draft_id:
            url = client.published_url_by_id(publisher_id, draft_id)
            if url:
                return "published", url
    url = client.published_url_by_id(publisher_id, draft_id)
    if url:
        return "published", url
    for d in draft_cleanup.list_drafts(client, publisher_id):
        if d["id"] == draft_id:
            status = (d.get("draft_status") or "").lower()
            if any(m in status for m in REJECT_MARKERS):
                return "rejected", status
            return "draft", ""
    return "in_review", ""


def _close_extra_tabs(client: DzenClient) -> None:
    for page in list(client._ctx.pages):  # noqa: SLF001
        if page is not client._page:  # noqa: SLF001
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass


def resolve_pending(db: Database, client: DzenClient, tg: Any, publisher_id: str, portal: Any) -> bool:
    """Returns True while an article is still waiting on a human (block all
    browser work), False once the gate is clear."""
    gate = gate_get(db)
    if not gate:
        return False
    article = db.one("SELECT * FROM articles WHERE id=?", (gate["article_id"],))
    if not article or article.get("status") != "awaiting_captcha":
        gate_clear(db)
        return False
    state, detail = publication_state(client, publisher_id, gate.get("draft_id", ""))
    title = article["title"]
    if state == "published":
        db.update_article(article["id"], status="published", dzen_url=detail, publish_mode="publish",
                          published_at=datetime.now(timezone.utc).isoformat(), last_error="")
        db.add_run("publish", "ok", project_id=article["project_id"], article_id=article["id"],
                   note=f"after captcha: {title[:150]}")
        gate_clear(db)
        _close_extra_tabs(client)
        tg.safe_send(f"🟢 Опубликовано после проверки\n{title}\n{detail}")
        return False
    if state == "rejected":
        db.update_article(article["id"], status="rejected", last_error=f"dzen: {detail}"[:500])
        gate_clear(db)
        tg.safe_send(f"⛔ Дзен отклонил публикацию: {detail}\n{title}")
        return False
    if state == "in_review":
        db.update_article(article["id"], status="in_review", last_error="")
        gate_clear(db)
        _close_extra_tabs(client)
        tg.safe_send(f"🕓 Статья ушла на проверку Дзена, жду результата\n{title}")
        return False
    if time.time() - int(gate.get("reminded_at") or 0) > CAPTCHA_REMINDER_S:
        gate["reminded_at"] = int(time.time())
        db.set_setting(GATE_KEY, json.dumps(gate))
        hours = (time.time() - int(gate.get("since") or time.time())) / 3600
        tg.safe_send(f"⏳ Публикация ждёт «Я не робот» уже {hours:.0f} ч, остальное на паузе.\n{title}\n"
                     + portal.message())
    return True


def check_in_review(db: Database, client: DzenClient, tg: Any, publisher_id: str) -> None:
    for article in db.query("SELECT * FROM articles WHERE status='in_review' AND dzen_publication_id<>''"):
        state, detail = publication_state(client, publisher_id, article["dzen_publication_id"])
        if state == "published":
            db.update_article(article["id"], status="published", dzen_url=detail, publish_mode="publish",
                              published_at=datetime.now(timezone.utc).isoformat())
            db.add_run("publish", "ok", project_id=article["project_id"], article_id=article["id"],
                       note=f"after review: {article['title'][:150]}")
            tg.safe_send(f"🟢 Прошло проверку и опубликовано\n{article['title']}\n{detail}")
        elif state == "rejected":
            db.update_article(article["id"], status="rejected", last_error=f"dzen: {detail}"[:500])
            tg.safe_send(f"⛔ Дзен отклонил после проверки: {detail}\n{article['title']}")


def recover_after_restart(db: Database, client: DzenClient, tg: Any, publisher_id: str) -> None:
    """A restart loses the tab the human was supposed to solve the captcha
    in. If Dzen already moved the article on, record that; if it's still just
    a draft, requeue it (same draft, reused) - it goes again only after the
    normal gap, never immediately."""
    gate = gate_get(db)
    if gate:
        article = db.one("SELECT * FROM articles WHERE id=?", (gate["article_id"],))
        if article and article.get("status") == "awaiting_captcha":
            state, detail = publication_state(client, publisher_id, gate.get("draft_id", ""))
            if state == "published":
                db.update_article(article["id"], status="published", dzen_url=detail, publish_mode="publish",
                                  published_at=datetime.now(timezone.utc).isoformat(), last_error="")
            elif state == "rejected":
                db.update_article(article["id"], status="rejected", last_error=f"dzen: {detail}"[:500])
            elif state == "in_review":
                db.update_article(article["id"], status="in_review", last_error="")
            else:
                db.update_article(article["id"], status="ready", last_error="captcha tab lost on restart")
                tg.safe_send(f"🔁 После перезапуска окно с капчей пропало — статья вернулась в очередь "
                             f"(тот же черновик, пойдёт после паузы)\n{article['title']}")
        gate_clear(db)
    # a restart must not turn into a burst of publishes
    now = time.time()
    if float(db.get_setting(NEXT_PUBLISH_KEY, "0") or 0) < now + 180:
        db.set_setting(NEXT_PUBLISH_KEY, str(int(now + random.uniform(180, 360))))
