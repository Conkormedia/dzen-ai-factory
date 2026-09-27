"""Deletes junk Dzen drafts through the real editor UI, one exact draft id at
a time: open the draft's editor → header ⋯ → «Удалить публикацию» → confirm.

Targeting by id (not by title in the Studio list) matters: pipeline drafts
share titles with published articles, and a title match could land on the
wrong row. Guards on every delete: the id must not be a published one, the
draft count must drop by exactly one, and the published count must never
drop - if it does, everything stops.

- one-off full sweep: ``python -m autopilot.draft_cleanup --all`` (backs up
  every listed draft's id/title/snippet to JSON first);
- routine safety net from the live loop: ``sweep_orphan_drafts``.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .config import settings as default_settings
from .db import Database
from .dzen_client import EDITOR, CaptchaRequired, DzenClient, DzenError

log = logging.getLogger(__name__)

PAGE_SIZE = 50
PAUSE_BETWEEN_DELETES_S = (4.0, 9.0)
EMPTY_DRAFT_MIN_AGE_S = 2 * 3600


class PublishedCountDropped(DzenError):
    pass


def list_drafts(client: DzenClient, publisher_id: str) -> list[dict[str, Any]]:
    """The newest PAGE_SIZE drafts (the list API ignores page= - deeper drafts
    only surface once newer ones are deleted)."""
    data = client.api("GET", f"/editor-api/v3/publications?state=draft&pageSize={PAGE_SIZE}"
                             f"&publisherId={publisher_id}")
    out = []
    for d in data.get("drafts") or []:
        if not d.get("id"):
            continue
        preview = (d.get("content") or {}).get("preview") or {}
        out.append({"id": d["id"], "title": preview.get("title", ""), "snippet": preview.get("snippet", "")[:300],
                    "add_time": d.get("addTime"), "draft_status": d.get("draftStatus", "")})
    return out


def published_ids(client: DzenClient, publisher_id: str) -> set[str]:
    data = client.api("GET", f"/editor-api/v3/publications?state=published&pageSize=100&publisherId={publisher_id}")
    return {p.get("id") for p in data.get("publications") or [] if p.get("id")}


def counts(client: DzenClient, publisher_id: str) -> dict[str, int]:
    data = client.api("GET", f"/editor-api/v2/publisher/{publisher_id}/publications-count?")
    article = (data.get("items") or {}).get("article") or {}
    return {k: int(v or 0) for k, v in article.items()}


def _pause(page: Any, lo: float, hi: float) -> None:
    page.wait_for_timeout(int(random.uniform(lo, hi) * 1000))


def delete_draft_by_id(client: DzenClient, publisher_id: str, draft_id: str) -> None:
    page = client._page  # noqa: SLF001
    page.goto(f"{EDITOR}/id/{publisher_id}/{draft_id}/edit", wait_until="networkidle", timeout=45000)
    _pause(page, 1.5, 3.0)
    page.keyboard.press("Escape")
    if any("not_robot_captcha" in (f.url or "") or "id.vk.ru" in (f.url or "") for f in page.frames):
        raise CaptchaRequired("Дзен показал капчу при удалении черновика")
    publish_btn = page.get_by_role("button", name="Опубликовать").first
    if not publish_btn.is_visible(timeout=5000):
        raise DzenError(f"draft {draft_id} editor did not open")
    publish_btn.locator("xpath=preceding::button[1]").click(timeout=5000)
    _pause(page, 0.8, 1.6)
    page.get_by_text("Удалить публикацию", exact=True).last.click(timeout=5000)
    _pause(page, 0.8, 1.6)
    page.get_by_role("button", name="Удалить", exact=True).last.click(timeout=5000)
    _pause(page, 2.0, 3.0)


def delete_drafts(client: DzenClient, publisher_id: str, draft_ids: list[str],
                  *, max_deletes: int | None = None) -> int:
    """Deletes the given draft ids one by one. Stops on captcha, on a drop in
    the published count, or when deletes stop taking effect."""
    live = published_ids(client, publisher_id)
    start = counts(client, publisher_id)
    published_floor = start.get("published", 0)
    drafts_before = start.get("draft", 0)
    deleted = 0
    misses = 0
    for draft_id in draft_ids:
        if max_deletes is not None and deleted >= max_deletes:
            break
        if draft_id in live:
            log.warning("refusing to delete %s: it is a published article", draft_id)
            continue
        try:
            delete_draft_by_id(client, publisher_id, draft_id)
        except CaptchaRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("could not delete draft %s: %s", draft_id, str(exc)[:200])
            misses += 1
            if misses >= 3:
                break
            continue
        now = counts(client, publisher_id)
        if now.get("published", 0) < published_floor:
            raise PublishedCountDropped(
                f"published count dropped {published_floor} -> {now.get('published')} after deleting {draft_id}")
        if now.get("draft", 0) < drafts_before:
            deleted += drafts_before - now.get("draft", 0)
            drafts_before = now.get("draft", 0)
            misses = 0
        else:
            misses += 1
            if misses >= 3:
                log.warning("draft count stopped dropping - stopping cleanup")
                break
        _pause(client._page, *PAUSE_BETWEEN_DELETES_S)  # noqa: SLF001
    return deleted


def orphan_draft_ids(db: Database, drafts: list[dict[str, Any]], now_ms: int) -> list[str]:
    """Drafts the pipeline created that no article is going to reuse: title
    matches one of our articles, or it's an empty draft (created, never
    filled) old enough that nobody is typing into it. Drafts written by hand
    have other titles and are never touched."""
    ours = {r["title"] for r in db.query("SELECT title FROM articles")}
    keep = {r["dzen_publication_id"] for r in db.query(
        "SELECT dzen_publication_id FROM articles WHERE status IN ('ready','publishing','awaiting_captcha') "
        "AND dzen_publication_id<>''")}
    out = []
    for d in drafts:
        if d["id"] in keep:
            continue
        added = d.get("add_time")
        age_ms = now_ms - int(added) if added is not None else 0
        empty_and_old = not d["title"] and age_ms > EMPTY_DRAFT_MIN_AGE_S * 1000
        diagnostic = d["title"].startswith("Диагностика") and "паблиша" in d["title"]
        if d["title"] in ours or empty_and_old or diagnostic:
            out.append(d["id"])
    return out


def sweep_orphan_drafts(db: Database, client: DzenClient, publisher_id: str, *, max_deletes: int = 10) -> int:
    ids = orphan_draft_ids(db, list_drafts(client, publisher_id), int(time.time() * 1000))
    return delete_drafts(client, publisher_id, ids, max_deletes=max_deletes) if ids else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="delete every draft on the channel")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max", type=int, default=None)
    args = parser.parse_args()

    db = Database(default_settings.db_path)
    publisher_id = db.get_setting("dzen_publisher_id", "")
    backup = default_settings.data_dir / f"drafts-backup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    backed_up: dict[str, dict[str, Any]] = {}
    with DzenClient(default_settings) as client:
        print(f"counts: {counts(client, publisher_id)}, backup: {backup}", flush=True)
        if not args.all:
            print("nothing deleted: pass --all for a full sweep")
            return
        started = time.time()
        total = 0
        while args.max is None or total < args.max:
            batch = list_drafts(client, publisher_id)
            if not batch:
                break
            for d in batch:
                backed_up.setdefault(d["id"], d)
            backup.write_text(json.dumps(list(backed_up.values()), ensure_ascii=False, indent=1), encoding="utf-8")
            if args.dry_run:
                break
            n = delete_drafts(client, publisher_id, [d["id"] for d in batch],
                              max_deletes=None if args.max is None else args.max - total)
            total += n
            print(f"round: deleted {n}, total {total}, counts {counts(client, publisher_id)}, "
                  f"{time.time() - started:.0f}s", flush=True)
            if n == 0:
                break
        print(f"done: deleted {total}, counts {counts(client, publisher_id)}, backed up {len(backed_up)} to {backup}",
              flush=True)


if __name__ == "__main__":
    main()
