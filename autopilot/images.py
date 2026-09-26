"""Real images only — downloaded from the brand's own crawled site pages.

No synthetic/generated images: if nothing suitable was found on the site,
callers get an empty list and the article publishes without one, rather than
a stock-photo-style generated substitute.
"""
from __future__ import annotations

import hashlib
import logging
import re
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

log = logging.getLogger(__name__)

TIMEOUT = 20
MIN_BYTES = 4000
MIN_DIMENSION = 300
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def collect_real_image_urls(site_pages: list[dict], *, limit: int = 20) -> list[str]:
    """Flatten + dedupe the image URLs research.py collected while crawling
    the brand's own site (see research.extract_page / _real_images)."""
    seen: set[str] = set()
    out: list[str] = []
    for page in site_pages:
        for url in page.get("images", []) or []:
            url = str(url)
            if url and url not in seen:
                seen.add(url)
                out.append(url)
            if len(out) >= limit:
                return out
    return out


def _download_one(url: str, out_dir: Path, name_hint: str) -> Path | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        log.info("real-image download failed %s: %s", url, exc)
        return None
    content = r.content
    if len(content) < MIN_BYTES:
        return None
    try:
        with Image.open(BytesIO(content)) as im:
            im.verify()
        with Image.open(BytesIO(content)) as im:
            w, h = im.size
            if w < MIN_DIMENSION or h < MIN_DIMENSION:
                return None
            out_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", name_hint.lower())[:40].strip("-") or "image"
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
            path = out_dir / f"{slug}-{digest}.jpg"
            im.convert("RGB").save(path, "JPEG", quality=88)
            return path
    except Exception as exc:  # noqa: BLE001 - Pillow raises various error types on bad images
        log.info("real-image not a usable image %s: %s", url, exc)
        return None


def pick_real_images(site_pages: list[dict], headline: str, out_dir: Path, *, count: int) -> list[Path]:
    """Downloads up to `count` real images from the brand's own crawled pages.
    Returns fewer (possibly zero) if none are available or usable — never
    generates a substitute."""
    if count <= 0:
        return []
    urls = collect_real_image_urls(site_pages, limit=count * 3)
    paths: list[Path] = []
    for i, url in enumerate(urls):
        path = _download_one(url, out_dir, f"{headline}-{i}")
        if path:
            paths.append(path)
        if len(paths) >= count:
            break
    return paths


def section_headings(markdown: str) -> list[str]:
    return [m.strip() for m in re.findall(r"^##\s+(.*)$", markdown, flags=re.M)]
