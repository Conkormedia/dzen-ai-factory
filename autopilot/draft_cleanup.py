"""Deletes junk Dzen drafts through the real Studio UI (row menu → Удалить →
confirm), the same way a person would - no API-level delete call.

Two uses:
- one-off full sweep: ``python -m autopilot.draft_cleanup --all`` (backs up
  the draft list to JSON first);
- routine safety net from the live loop: ``sweep_orphan_drafts`` deletes only
  drafts whose title matches an article this pipeline wrote and whose id isn't
  the draft a queued article is still going to reuse. Drafts a person wrote by
  hand never match and are never touched.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings as default_settings
from .db import Database
from .dzen_client import EDITOR, CaptchaRequired, DzenClient

log = logging.getLogger(__name__)

PAGE_SIZE = 50
PAUSE_BETWEEN_DELETES_MS = 1500


def list_drafts(client: DzenClient, publisher_id: str) -> list[dict[str, Any]]:
    """The newest PAGE_SIZE drafts (the list API ignores page= - deeper drafts
    only surface once newer ones are deleted, which is how the full sweep
    walks through them in rounds)."""
    data = client.api("GET", f"/editor-api/v3/publications?state=draft&pageSize={PAGE_SIZE}"
                             f"&publisherId={publisher_id}")
    out = []
    for d in data.get("drafts") or []:
        if not d.get("id"):
            continue
        preview = (d.get("content") or {}).get("preview") or {}
        out.append({"id": d["id"], "title": preview.get("title", ""), "snippet": preview.get("snippet", "")[:300],
                    "add_time": d.get("addTime")})
    return out


def draft_count(client: DzenClient, publisher_id: str) -> int:
    data = client.api("GET", f"/editor-api/v2/publisher/{publisher_id}/publications-count?")
    return int(((data.get("items") or {}).get("article") or {}).get("draft") or 0)


class _StudioDrafts:
    """Drives the Studio drafts tab. The tab stays open between deletes (the
    row disappears in place); it's re-opened only after a failed attempt."""

    def __init__(self, client: DzenClient, publisher_id: str):
        self.publisher_id = publisher_id
        self.page = client._page  # noqa: SLF001
        self.tab_open = False

    def open_tab(self) -> None:
        # /id/{pid}/publications lands on the dashboard; the real list is
        # behind the "Публикации" nav link (a vanity-slug URL).
        self.page.goto(f"{EDITOR}/id/{self.publisher_id}", wait_until="networkidle", timeout=45000)
        self.page.wait_for_timeout(1200)
        self.page.locator("a[href$='/publications']").first.click(timeout=8000)
        self.page.wait_for_load_state("networkidle", timeout=30000)
        self.page.wait_for_timeout(1200)
        self.page.get_by_text("Черновики", exact=False).first.click(timeout=8000)
        self.page.wait_for_timeout(2000)
        self.tab_open = True

    def captcha_up(self) -> bool:
        return any("not_robot_captcha" in (f.url or "") or "id.vk.ru" in (f.url or "") for f in self.page.frames)

    def delete_by_title(self, title: str) -> None:
        if not self.tab_open:
            self.open_tab()
        title_el = self.page.get_by_text(title, exact=True).first
        title_el.scroll_into_view_if_needed(timeout=8000)
        row = title_el.locator("xpath=ancestor::*[.//button][1]")
        row.locator("button").last.click(force=True, timeout=5000)
        self.page.wait_for_timeout(600)
        self.page.get_by_text("Удалить", exact=True).last.click(timeout=5000)
        self.page.wait_for_timeout(600)
        self.page.get_by_role("button", name="Удалить").last.click(timeout=5000)
        self.page.wait_for_timeout(PAUSE_BETWEEN_DELETES_MS)
        if self.captcha_up():
            raise CaptchaRequired("Дзен показал капчу во время удаления черновиков")


def delete_drafts(client: DzenClient, publisher_id: str, drafts: list[dict[str, Any]],
                  *, max_deletes: int | None = None) -> int:
    """Deletes the given drafts newest-first via the UI. Each delete is
    confirmed by the draft count dropping; stops on captcha or when deletes
    stop taking effect (UI changed - better to stop than to click blindly)."""
    ui = _StudioDrafts(client, publisher_id)
    before = draft_count(client, publisher_id)
    deleted = 0
    misses = 0
    for d in sorted(drafts, key=lambda x: x.get("add_time") or 0, reverse=True):
        if max_deletes is not None and deleted >= max_deletes:
            break
        if not d.get("title"):
            continue
        try:
            ui.delete_by_title(d["title"])
        except CaptchaRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("could not delete draft %s (%r): %s", d["id"], d["title"], exc)
            ui.tab_open = False
            misses += 1
            if misses >= 3:
                break
            continue
        now = draft_count(client, publisher_id)
        if now < before:
            deleted += before - now
            before = now
            misses = 0
        else:
            ui.tab_open = False
            misses += 1
            if misses >= 3:
                log.warning("draft count stopped dropping - stopping cleanup")
                break
    return deleted


def sweep_orphan_drafts(db: Database, client: DzenClient, publisher_id: str, *, max_deletes: int = 20) -> int:
    """Routine safety net: deletes pipeline-created drafts that no queued
    article is going to reuse. Titles this pipeline never wrote are left
    alone, so drafts written by hand are safe."""
    ours = {r["title"] for r in db.query("SELECT title FROM articles")}
    keep = {r["dzen_publication_id"] for r in db.query(
        "SELECT dzen_publication_id FROM articles WHERE status IN ('ready','publishing') AND dzen_publication_id<>''")}
    drafts = list_drafts(client, publisher_id)
    # The UI delete targets the first row with a given title, which could be
    # the kept draft when titles repeat - so skip any title a kept draft has.
    kept_titles = {d["title"] for d in drafts if d["id"] in keep}
    orphans = [d for d in drafts if d["title"] in ours and d["id"] not in keep and d["title"] not in kept_titles]
    if not orphans:
        return 0
    return delete_drafts(client, publisher_id, orphans, max_deletes=max_deletes)


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
        print(f"drafts on channel: {draft_count(client, publisher_id)}, backup: {backup}", flush=True)
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
            n = delete_drafts(client, publisher_id, batch,
                              max_deletes=None if args.max is None else args.max - total)
            total += n
            left = draft_count(client, publisher_id)
            print(f"round: deleted {n}, total {total}, left {left}, {time.time() - started:.0f}s", flush=True)
            if n == 0:
                break
        print(f"done: deleted {total} drafts, left {draft_count(client, publisher_id)}, "
              f"backed up {len(backed_up)} to {backup}", flush=True)


if __name__ == "__main__":
    main()
