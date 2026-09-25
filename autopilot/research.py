"""Brand research: crawl the official site, learn competitors, build a knowledge base."""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from .quality import domain_of

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")
PRIORITY_HINTS = ("about", "o-nas", "pricing", "price", "tarif", "tariff", "plans", "features", "funk", "vozmozh",
                  "faq", "help", "docs", "doc", "guide", "how", "kak", "blog", "news", "case", "integr", "api",
                  "review", "otzyv", "demo", "start", "contact")


def fetch(url: str, timeout: int = 25) -> requests.Response | None:
    try:
        resp = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}, timeout=timeout,
                            allow_redirects=True)
        if resp.status_code >= 400:
            return None
        return resp
    except requests.RequestException as exc:
        log.info("fetch failed %s: %s", url, exc)
        return None


def extract_page(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")[:200]
    desc = ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta and meta.get("content"):
        desc = str(meta["content"]).strip()[:400]
    headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])][:40]
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\s+", " ", main.get_text(" ", strip=True)) if main else ""
    return {"url": url, "title": title, "description": desc, "headings": headings, "text": text[:6000]}


def _sitemap_urls(base: str, limit: int = 300) -> list[str]:
    urls: list[str] = []
    for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"):
        resp = fetch(urljoin(base, path), timeout=15)
        if not resp or "xml" not in (resp.headers.get("content-type", "") + resp.text[:100]).lower():
            continue
        try:
            root = ElementTree.fromstring(resp.content)
        except ElementTree.ParseError:
            continue
        locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
        nested = [u for u in locs if u.endswith(".xml")][:5]
        for child in nested:
            sub = fetch(child, timeout=15)
            if not sub:
                continue
            try:
                sroot = ElementTree.fromstring(sub.content)
                locs += [el.text.strip() for el in sroot.iter() if el.tag.endswith("loc") and el.text]
            except ElementTree.ParseError:
                pass
        urls = [u for u in locs if not u.endswith(".xml")]
        if urls:
            break
    return urls[:limit]


def discover_pages(base_url: str, max_pages: int = 12) -> list[str]:
    base_domain = domain_of(base_url)
    found: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = u.split("#")[0].rstrip("/") or u
        if not u or u in seen:
            return
        if domain_of(u) != base_domain and not domain_of(u).endswith("." + base_domain):
            return
        if re.search(r"\.(png|jpe?g|gif|svg|webp|pdf|zip|css|js|ico|mp4|woff2?)($|\?)", u, re.I):
            return
        seen.add(u)
        found.append(u)

    add(base_url)
    home = fetch(base_url)
    if home:
        soup = BeautifulSoup(home.text, "lxml")
        for a in soup.find_all("a", href=True):
            add(urljoin(base_url, a["href"]))
    for u in _sitemap_urls(base_url):
        add(u)

    def priority(u: str) -> tuple[int, int]:
        path = urlparse(u).path.lower()
        hit = min((i for i, h in enumerate(PRIORITY_HINTS) if h in path), default=len(PRIORITY_HINTS))
        return (0 if u.rstrip("/") == base_url.rstrip("/") else 1, hit)

    ordered = sorted(found, key=priority)
    return ordered[:max_pages]


def crawl_site(base_url: str, max_pages: int = 12) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    if not base_url:
        return pages
    for url in discover_pages(base_url, max_pages=max_pages):
        resp = fetch(url)
        if not resp or "html" not in resp.headers.get("content-type", "").lower():
            continue
        page = extract_page(resp.text, resp.url)
        if len(page["text"]) > 200:
            pages.append(page)
    return pages


def mine_titles(domains: list[str], per_domain: int = 60) -> list[str]:
    """Collect blog/article titles from competitor sites (RSS, sitemap slugs)."""
    titles: list[str] = []
    for domain in domains[:8]:
        base = f"https://{domain.strip().lower().removeprefix('https://').removeprefix('http://').rstrip('/')}"
        got: list[str] = []
        for feed_path in ("/feed", "/rss", "/blog/feed", "/blog/rss", "/feed.xml", "/rss.xml", "/blog/rss.xml"):
            resp = fetch(urljoin(base, feed_path), timeout=15)
            if not resp or "<item" not in resp.text[:20000] and "<entry" not in resp.text[:20000]:
                continue
            got += re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", resp.text, flags=re.S)[1:per_domain]
            if got:
                break
        if not got:
            for u in _sitemap_urls(base, limit=400):
                path = urlparse(u).path
                if any(k in path for k in ("/blog/", "/articles/", "/news/", "/journal/", "/post/", "/stati/", "/guide")):
                    slug = path.rstrip("/").split("/")[-1]
                    words = re.sub(r"[-_]+", " ", re.sub(r"\.\w+$", "", slug)).strip()
                    if len(words) > 12 and not words.isdigit():
                        got.append(words)
                if len(got) >= per_domain:
                    break
        titles += [re.sub(r"\s+", " ", t).strip()[:140] for t in got if t and len(t.strip()) > 8]
    # dedupe, keep order
    seen: set[str] = set()
    out = []
    for t in titles:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out[:300]


KNOWLEDGE_SYSTEM = """Ты — ведущий контент-стратег и бренд-аналитик. Изучаешь бизнес по его сайту и брифу и
строишь базу знаний для последующего написания серии SEO-статей, продвигающих ТОЛЬКО этот бизнес.
Не выдумывай факты: если чего-то нет на сайте и в брифе — оставь поле пустым или пометь как предположение
в поле assumptions. Пиши по-русски, кратко и предметно."""


def build_knowledge(llm: Any, project: dict[str, Any], pages: list[dict[str, Any]],
                    mined_titles: list[str]) -> dict[str, Any]:
    site_digest = []
    for p in pages[:12]:
        site_digest.append({"url": p["url"], "title": p["title"], "description": p["description"],
                            "headings": p["headings"][:15], "text": p["text"][:2500]})
    user = (
        f"БИЗНЕС: {project['name']}\nСАЙТ: {project.get('url') or 'не указан'}\n"
        f"БРИФ ОТ ВЛАДЕЛЬЦА:\n{project.get('brief') or '(пусто)'}\n\n"
        f"СТРАНИЦЫ САЙТА (JSON):\n{json.dumps(site_digest, ensure_ascii=False)[:60000]}\n\n"
        f"ЗАГОЛОВКИ СТАТЕЙ ПОХОЖИХ СЕРВИСОВ (для понимания, о чём пишет рынок; их бренды упоминать нельзя):\n"
        f"{json.dumps(mined_titles[:120], ensure_ascii=False)}\n\n"
        "Верни JSON со структурой:\n"
        "{\n"
        '  "summary": "2-3 предложения, что это за продукт и для кого",\n'
        '  "audience": ["сегменты аудитории"],\n'
        '  "value_props": ["ключевые выгоды"],\n'
        '  "features": ["конкретные функции/возможности из сайта"],\n'
        '  "use_cases": ["сценарии использования"],\n'
        '  "pains": ["боли клиента, которые продукт решает"],\n'
        '  "objections": ["возражения и как отвечать"],\n'
        '  "terminology": ["термины/ключевые слова ниши"],\n'
        '  "seo_clusters": [{"cluster": "тема", "keywords": ["ключевые фразы"]}],\n'
        '  "internal_links": [{"url": "полный URL страницы сайта", "title": "как назвать ссылку", "use_for": "когда уместна"}],\n'
        '  "competitors": [{"name": "название похожего сервиса", "domain": "домен"}],\n'
        '  "content_pillars": ["5-8 контентных направлений"],\n'
        '  "tone": "рекомендуемый тон",\n'
        '  "cta_variants": ["варианты призыва к действию"],\n'
        '  "facts": ["проверяемые факты, цифры, тарифы, платформы — только с сайта/брифа"],\n'
        '  "assumptions": ["что осталось непонятным / предположения"]\n'
        "}\n"
        "В competitors перечисли 6-10 реальных известных сервисов той же категории (нужны только для чёрного списка упоминаний)."
    )
    knowledge = llm.json(KNOWLEDGE_SYSTEM, user, purpose="research:knowledge", max_tokens=9000, effort="high")
    if not isinstance(knowledge, dict):
        raise RuntimeError("knowledge JSON is not an object")
    knowledge.setdefault("competitors", [])
    knowledge.setdefault("internal_links", [])
    return knowledge


def run_research(llm: Any, db: Any, project: dict[str, Any], *, max_pages: int = 12) -> dict[str, Any]:
    pages = crawl_site(project.get("url", ""), max_pages=max_pages)
    log.info("crawled %s pages for %s", len(pages), project["slug"])
    previous = db.knowledge(project["id"])
    competitor_domains = [str(c.get("domain", "")) for c in previous.get("competitors", []) if isinstance(c, dict)]
    mined = mine_titles([d for d in competitor_domains if d]) if competitor_domains else []
    knowledge = build_knowledge(llm, project, pages, mined)
    if not mined:
        # Second pass: now that the LLM named competitors, mine their blogs for topic patterns.
        domains = [str(c.get("domain", "")) for c in knowledge.get("competitors", []) if isinstance(c, dict)]
        mined = mine_titles([d for d in domains if d])
        if mined:
            knowledge["competitor_topic_samples"] = mined[:80]
    knowledge["site_pages_count"] = len(pages)
    knowledge["site_reachable"] = bool(pages)
    db.save_knowledge(project["id"], knowledge, [{"url": p["url"], "title": p["title"]} for p in pages], mined)
    db.log_event(f"Исследование «{project['name']}»: страниц {len(pages)}, заголовков рынка {len(mined)}, "
                 f"конкурентов в стоп-листе {len(knowledge.get('competitors', []))}", kind="research")
    return knowledge
