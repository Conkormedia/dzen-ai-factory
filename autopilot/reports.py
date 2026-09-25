"""Daily Telegram digest: what got published, per-project split, cost, errors."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import Settings
from .telegram import Telegram

log = logging.getLogger(__name__)


def _day_bounds(settings: Settings, day: datetime) -> tuple[str, str]:
    start_local = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(None).isoformat(), end_local.astimezone(None).isoformat()


def build_and_send(db, tg: Telegram, settings: Settings, *, now: datetime | None = None) -> None:
    now = now or datetime.now(settings.tz)
    day_key = now.strftime("%Y-%m-%d")
    if db.report_sent(day_key):
        return

    start_iso, end_iso = _day_bounds(settings, now)
    published = db.published_between(start_iso, end_iso)
    created = db.articles_created_between(start_iso, end_iso)
    usage = db.usage_between(start_iso, end_iso)
    publish_runs = db.runs_between("publish", start_iso, end_iso)
    failed_publishes = [r for r in publish_runs if r["status"] == "failed"]

    by_project: dict[str, list[dict]] = {}
    for a in published:
        by_project.setdefault(a["project_slug"], []).append(a)

    lines = [f"📊 Сводка за {now.strftime('%d.%m.%Y')}", ""]
    total = len(published)
    lines.append(f"Опубликовано статей: {total}")
    for project in db.projects():
        items = by_project.get(project["slug"], [])
        quota = project.get("daily_quota", 0)
        mark = "✅" if len(items) >= quota else ("⚠️" if items else "❌")
        lines.append(f"{mark} {project['name']}: {len(items)}/{quota}")
    lines.append("")

    if total:
        lines.append("Ссылки:")
        for project in db.projects():
            items = by_project.get(project["slug"], [])
            for a in items[:5]:
                url = a.get("dzen_url") or "(без ссылки — режим черновика)"
                lines.append(f"• {a['title'][:70]} — {url}")
        if any(len(v) > 5 for v in by_project.values()):
            lines.append("…")
        lines.append("")

    if failed_publishes:
        lines.append(f"⚠️ Ошибок публикации: {len(failed_publishes)}")
        for r in failed_publishes[:5]:
            lines.append(f"  - {r.get('note', '')[:150]}")
        lines.append("")

    if created and not published:
        lines.append(f"Статей подготовлено, но не опубликовано: {len(created)} (проверь вход в Дзен)")
        lines.append("")

    totals = db.metrics_total()
    if totals.get("views"):
        lines.append(f"Всего просмотров по каналу: {totals['views']} · лайков {totals['likes']} · комментов {totals['comments']}")
        lines.append("")

    if usage.get("calls"):
        lines.append(f"LLM: {usage['calls']} запросов, ≈${usage['cost_usd']:.2f}")

    text = "\n".join(lines).strip()
    message_id = tg.safe_send(text)
    db.save_report(day_key, message_id, {"published": total, "usage": usage})
    log.info("daily report sent for %s: %s articles", day_key, total)
