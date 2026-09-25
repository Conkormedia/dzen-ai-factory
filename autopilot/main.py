"""Entrypoint: `python -m autopilot.main`. One long-running process, safe to
run under systemd with Restart=always — every external call is wrapped so a
single failure never crashes the whole service."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import images, login_portal, reports
from . import topics as topics_mod
from . import writer
from .config import Settings, settings as default_settings
from .db import Database
from .dzen_client import CaptchaRequired, DzenClient, DzenError, NoChannel, SessionExpired, publish_full_article
from .llm import LLM, LLMError
from .research import run_research
from .telegram import Telegram, capture_chat_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("autopilot.main")

TICK_SECONDS = 90
KNOWLEDGE_MAX_AGE_DAYS = 14
STATS_REFRESH_SECONDS = 3600


def seed_projects(db: Database, settings: Settings) -> None:
    seed_path = Path(__file__).with_name("projects.seed.json")
    if not seed_path.is_file():
        return
    try:
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.exception("projects.seed.json is invalid, skipping seed")
        return
    for item in seed:
        db.upsert_project(
            item["slug"], item["name"], item.get("url", ""), item.get("brief", ""),
            daily_quota=int(item.get("daily_quota", 10)), tone=item.get("tone", ""),
        )
    log.info("seeded/updated %s projects", len(seed))


def wait_for_telegram(settings: Settings, db: Database, env_path: Path) -> Telegram:
    nagged_no_token = False
    while True:
        bot_token = settings.telegram_bot_token or db.get_setting("telegram_bot_token", "")
        if not bot_token:
            if not nagged_no_token:
                log.warning("Waiting for TELEGRAM_BOT_TOKEN. Set it with deploy/robocall/configure.sh and restart the service.")
                nagged_no_token = True
            time.sleep(20)
            settings.telegram_bot_token = Settings().telegram_bot_token
            continue
        chat_id = db.get_setting("telegram_chat_id", "") or settings.telegram_chat_id
        if chat_id:
            return Telegram(bot_token, chat_id)
        captured = capture_chat_id(bot_token, env_path=env_path, db=db, timeout_seconds=20)
        if captured:
            return Telegram(bot_token, captured)
        log.info("Waiting for the owner to send /start to the bot…")


def wait_for_llm(settings: Settings, db: Database, tg: Telegram) -> LLM:
    nagged = False
    while True:
        fresh = Settings()
        settings.__dict__.update(fresh.__dict__)
        try:
            return LLM(settings, db)
        except LLMError:
            if not nagged:
                tg.safe_send(
                    "🔑 Нужен ключ LLM, чтобы писать статьи: задай один из "
                    "ANTHROPIC_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY "
                    "в /etc/dzen-autopilot.env и перезапусти сервис: "
                    "`systemctl restart dzen-autopilot`."
                )
                nagged = True
            time.sleep(30)


def _today_window(settings: Settings, now: datetime) -> float:
    """Fraction (0..1) of today's publish window that has elapsed."""
    local = now.astimezone(settings.tz)
    sh, sm = (int(x) for x in settings.window_start.split(":"))
    eh, em = (int(x) for x in settings.window_end.split(":"))
    start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:
        end = start.replace(hour=23, minute=59)
    total = max(1.0, (end - start).total_seconds())
    elapsed = max(0.0, min(total, (local - start).total_seconds()))
    return elapsed / total


def _published_today(db: Database, project_id: int, settings: Settings, now: datetime) -> int:
    local = now.astimezone(settings.tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    end = local.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(timezone.utc).isoformat()
    rows = db.query(
        "SELECT COUNT(*) n FROM articles WHERE project_id=? AND status='published' AND published_at>=? AND published_at<=?",
        (project_id, start, end),
    )
    return int(rows[0]["n"]) if rows else 0


def _days_old(iso_ts: str | None) -> float:
    if not iso_ts:
        return 999.0
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return 999.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def _prefetch_one(llm: LLM, db: Database, settings: Settings, project: dict) -> bool:
    knowledge = db.knowledge(project["id"])
    stale = (not knowledge) or (_days_old(knowledge.get("_updated_at")) > KNOWLEDGE_MAX_AGE_DAYS)
    if stale:
        try:
            knowledge = run_research(llm, db, project, max_pages=12)
        except Exception:  # noqa: BLE001
            log.exception("research failed for %s", project["slug"])
            if not knowledge:
                return False

    mined_titles = db.mined_titles(project["id"])
    added = topics_mod.ensure_topic_bank(llm, db, project, knowledge, mined_titles, minimum=6, batch=20)
    if added:
        log.info("project=%s topics_added=%s", project["slug"], added)

    topic = db.claim_topic(project["id"])
    if not topic:
        return False

    try:
        article = writer.write_article(
            llm, project, topic, knowledge,
            min_chars=settings.article_min_chars, target_chars=settings.article_target_chars,
            max_chars=settings.article_max_chars,
        )
    except Exception:
        log.exception("write_article failed for topic #%s (%s)", topic["id"], topic["title"])
        db.finish_topic(topic["id"], "error")
        db.add_run("write", "failed", project_id=project["id"], note=str(topic["title"])[:200])
        return False

    out_dir = settings.images_dir / project["slug"]
    cover_path = images.make_cover(article["title"], project["name"], project["slug"], out_dir)
    inline_paths: list[Path] = []
    if settings.images_per_article > 0:
        sections = images.section_headings(article["markdown"])
        inline_paths = images.make_inline_images(
            article["title"], sections, project["name"], project["slug"], out_dir, settings.images_per_article,
        )

    db.add_article(
        project["id"], topic["id"], title=article["title"], description=article["description"],
        tags=article["tags"], markdown=article["markdown"], quality=article["quality"],
        cover_path=str(cover_path), images=[{"path": str(p)} for p in inline_paths], status="ready",
    )
    db.finish_topic(topic["id"], "used")
    db.add_run("write", "ok", project_id=project["id"], note=article["title"][:200])
    log.info("project=%s article_ready=%r score=%s", project["slug"], article["title"], article["quality"].get("score"))
    return True


def _publish_one(db: Database, settings: Settings, tg: Telegram, client: DzenClient, publisher_id: str,
                 project: dict) -> bool:
    article = db.claim_article(project["id"])
    if not article:
        return False
    try:
        tags = json.loads(article.get("tags_json") or "[]")
        images_meta = json.loads(article.get("images_json") or "[]")
        inline_paths = [Path(x["path"]) for x in images_meta if x.get("path")]
        cover = Path(article["cover_path"]) if article.get("cover_path") else None
        result = publish_full_article(
            client, publisher_id, title=article["title"], markdown=article["markdown"],
            description=article["description"], tags=tags, cover_path=cover,
            inline_image_paths=inline_paths, mode=settings.publish_mode,
        )
        db.update_article(
            article["id"], status="published" if result["mode"] == "publish" else "draft_saved",
            dzen_publication_id=result["publication_id"], dzen_url=result["url"],
            publish_mode=result["mode"], published_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add_run("publish", "ok", project_id=project["id"], article_id=article["id"], note=article["title"][:200])
        tg.safe_send(f"🟢 {project['name']}: опубликовано\n{article['title']}\n{result['url']}")
        log.info("published project=%s article_id=%s url=%s", project["slug"], article["id"], result["url"])
        return True
    except CaptchaRequired as exc:
        db.update_article(article["id"], status="ready", last_error=str(exc))
        db.set_setting("dzen_cooldown_until", str(int(time.time()) + 3600))
        db.add_run("publish", "failed", project_id=project["id"], article_id=article["id"], note=str(exc)[:200])
        tg.safe_send("⏸ Дзен запросил проверку — публикация на паузе час, потом попробую снова.")
        return False
    except (SessionExpired, NoChannel):
        db.update_article(article["id"], status="ready", last_error="dzen session expired")
        raise  # bubble up: main() re-runs login_portal.ensure_login()
    except DzenError as exc:
        log.exception("publish failed for article #%s", article["id"])
        db.update_article(article["id"], status="ready", last_error=str(exc)[:500])
        db.add_run("publish", "failed", project_id=project["id"], article_id=article["id"], note=str(exc)[:200])
        return False


def run_forever(settings: Settings, db: Database, tg: Telegram, llm: LLM, publisher_id: str) -> None:
    tg.safe_send(
        "🚀 Автопилот запущен.\n"
        f"Проекты: {', '.join(p['name'] for p in db.projects(only_active=True))}\n"
        f"Режим публикации: {settings.publish_mode} · окно {settings.window_start}-{settings.window_end} МСК."
    )
    last_stats_refresh = 0.0
    with DzenClient(settings) as client:
        while True:
            now = datetime.now(timezone.utc)
            cooldown = int(db.get_setting("dzen_cooldown_until", "0") or 0)
            in_cooldown = cooldown > time.time()

            for project in db.projects(only_active=True):
                try:
                    fraction = _today_window(settings, now)
                    target = project["daily_quota"] * fraction
                    published_today = _published_today(db, project["id"], settings, now)

                    if db.count_articles(project["id"], "ready") < settings.queue_depth and published_today < project["daily_quota"]:
                        _prefetch_one(llm, db, settings, project)

                    if not in_cooldown and published_today < target and published_today < project["daily_quota"]:
                        if db.ready_articles(project["id"]):
                            _publish_one(db, settings, tg, client, publisher_id, project)
                except (SessionExpired, NoChannel):
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("tick failed for project %s", project.get("slug"))
                    db.log_event(f"Tick failed for {project.get('slug')}", kind="tick", level="error")

            db.release_stale_claims()

            if time.time() - last_stats_refresh > STATS_REFRESH_SECONDS:
                _refresh_stats(db, client, publisher_id)
                last_stats_refresh = time.time()

            local_now = now.astimezone(settings.tz)
            if local_now.hour == settings.report_hour:
                try:
                    reports.build_and_send(db, tg, settings, now=local_now)
                except Exception:  # noqa: BLE001
                    log.exception("daily report failed")

            time.sleep(TICK_SECONDS)


def _refresh_stats(db: Database, client: DzenClient, publisher_id: str) -> None:
    published = db.all_published()
    ids = [a["dzen_publication_id"] for a in published if a.get("dzen_publication_id")]
    if not ids:
        return
    try:
        stats = client.publication_stats(publisher_id, ids)
    except Exception:  # noqa: BLE001
        log.exception("stats refresh failed")
        return
    for a in published:
        s = stats.get(a["dzen_publication_id"])
        if s:
            db.upsert_metric(a["id"], s["views"], s["likes"], s["comments"], s["shares"])


def main() -> None:
    settings = default_settings
    settings.ensure_dirs()
    db = Database(settings.db_path)
    seed_projects(db, settings)
    db.log_event("autopilot starting", kind="startup")

    env_path = Path(os.getenv("DZEN_AUTOPILOT_ENV", "/etc/dzen-autopilot.env"))
    tg = wait_for_telegram(settings, db, env_path)
    llm = wait_for_llm(settings, db, tg)
    tg.safe_send(f"🧠 LLM подключён: {llm.provider}/{llm.model}")

    while True:
        publisher_id = login_portal.ensure_login(settings, db, tg)
        try:
            run_forever(settings, db, tg, llm, publisher_id)
        except (SessionExpired, NoChannel) as exc:
            log.warning("Dzen session needs re-authentication: %s", exc)
            db.set_setting("dzen_login_complete", "0")
            tg.safe_send("🔐 Сессия Дзена слетела — открываю окно входа заново.")
            continue
        except Exception:  # noqa: BLE001
            log.exception("run_forever crashed, restarting in 30s")
            db.log_event("run_forever crashed, restarting", kind="crash", level="error")
            tg.safe_send("⚠️ Внутренняя ошибка, перезапускаю цикл через 30 секунд.")
            time.sleep(30)


if __name__ == "__main__":
    main()
