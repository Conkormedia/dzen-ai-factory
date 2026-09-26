"""Real RSS news grounding, per project — so "news" topics reference an
actual recent item (title/link/summary) instead of the LLM inventing one.
Feeds were checked live for reachability/non-empty output at setup time,
not guessed from memory."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser

log = logging.getLogger(__name__)

NEWS_FEEDS: dict[str, list[str]] = {
    "beatscope": [
        "https://djmag.com/feed",
        "https://musictech.com/feed/",
        "https://www.digitaldjtips.com/feed/",
        "https://www.mixmag.net/rss.xml",
    ],
    "bizgateway": [
        "https://vc.ru/rss/all",  # general RU business/tech feed — the LLM picks relevant items
        "https://techcrunch.com/tag/whatsapp/feed/",
    ],
    "prioconcierge": [
        "https://frequentflyers.ru/feed/",
        "https://onemileatatime.com/feed/",
        "https://thepointsguy.com/feed/",
    ],
}

MAX_AGE_DAYS = 10  # generous: some feeds above post a few times/week


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _entry_date(entry) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_recent_news(project_slug: str, *, max_per_feed: int = 15, max_total: int = 40) -> list[dict]:
    """Best-effort; returns [] on total failure rather than raising — callers
    fall back to brief-only (evergreen) topics when no real news is found."""
    feeds = NEWS_FEEDS.get(project_slug, [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    items: list[dict] = []
    seen_links: set[str] = set()

    for url in feeds:
        try:
            parsed = feedparser.parse(url, agent="Mozilla/5.0 (compatible; DzenAutopilot/1.0)")
        except Exception:  # noqa: BLE001
            log.info("news feed fetch failed %s", url)
            continue
        for entry in list(getattr(parsed, "entries", []))[:max_per_feed]:
            link = str(getattr(entry, "link", "") or "").strip()
            title = _clean(str(getattr(entry, "title", "") or ""), 200)
            if not link or not title or link in seen_links:
                continue
            when = _entry_date(entry)
            if when and when < cutoff:
                continue
            summary = _clean(str(getattr(entry, "summary", "") or getattr(entry, "description", "") or ""), 500)
            seen_links.add(link)
            items.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": urlparse(link).netloc.removeprefix("www."),
                "published": when.isoformat() if when else "",
            })

    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:max_total]
