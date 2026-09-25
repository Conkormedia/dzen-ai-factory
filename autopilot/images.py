"""Cover + inline image generation.

Primary provider: Pollinations (free, keyless, reachable without a Russian
proxy — confirmed from the deploy host). Falls back to a locally rendered
PIL text-card so publishing is never blocked by an image provider outage.
"""
from __future__ import annotations

import hashlib
import logging
import random
import re
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
TIMEOUT = 45
SIZE = (1600, 900)

_LATIN_HINT = (
    "editorial photo, clean modern style, soft natural light, realistic, no text, no watermark, "
    "no logos, high detail, 35mm photography"
)


def _translit_prompt(headline: str, category: str, style_hint: str) -> str:
    # Pollinations reads any language fine, but a short latin steer keeps style consistent.
    return f"{headline}, {category}, {style_hint}, {_LATIN_HINT}"


def _fetch_pollinations(prompt: str, out_path: Path, *, seed: int | None = None) -> bool:
    seed = seed if seed is not None else random.randint(1, 999_999)
    url = POLLINATIONS_URL.format(prompt=quote(prompt[:800])) + f"?width={SIZE[0]}&height={SIZE[1]}&seed={seed}&nologo=true"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        if len(r.content) < 2000:
            return False
        out_path.write_bytes(r.content)
        return True
    except requests.RequestException as exc:
        log.warning("pollinations fetch failed: %s", exc)
        return False


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        trial = (current + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _fallback_card(headline: str, project_name: str, out_path: Path) -> None:
    seedv = int(hashlib.sha1(headline.encode("utf-8")).hexdigest()[:8], 16)
    rnd = random.Random(seedv)
    base = (rnd.randint(20, 60), rnd.randint(20, 60), rnd.randint(60, 110))
    img = Image.new("RGB", SIZE, base)
    draw = ImageDraw.Draw(img)
    for i in range(0, SIZE[0], 40):
        shade = tuple(min(255, c + 14) for c in base)
        draw.line([(i, 0), (i - SIZE[1], SIZE[1])], fill=shade, width=18)
    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        title_font = ImageFont.truetype(font_path, 64)
        brand_font = ImageFont.truetype(font_path, 34)
    except OSError:
        title_font = ImageFont.load_default()
        brand_font = ImageFont.load_default()
    lines = _wrap(draw, headline, title_font, SIZE[0] - 160)[:5]
    total_h = len(lines) * 78
    y = (SIZE[1] - total_h) // 2 - 20
    for line in lines:
        w = draw.textlength(line, font=title_font)
        draw.text(((SIZE[0] - w) / 2, y), line, font=title_font, fill=(255, 255, 255))
        y += 78
    w = draw.textlength(project_name.upper(), font=brand_font)
    draw.text(((SIZE[0] - w) / 2, SIZE[1] - 90), project_name.upper(), font=brand_font, fill=(230, 230, 230))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=88)


def make_cover(headline: str, project_name: str, category: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", headline.lower())[:40].strip("-") or "cover"
    path = out_dir / f"{slug}-cover.jpg"
    prompt = _translit_prompt(headline, category or project_name, "cover image, wide 16:9, no people faces close-up")
    if not _fetch_pollinations(prompt, path):
        _fallback_card(headline, project_name, path)
    return path


def make_inline_images(headline: str, sections: list[str], project_name: str, category: str, out_dir: Path,
                       count: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    pool = sections or [headline]
    for i in range(count):
        section = pool[i % len(pool)]
        slug = re.sub(r"[^a-z0-9]+", "-", (headline + str(i)).lower())[:40].strip("-") or f"img{i}"
        path = out_dir / f"{slug}-{i}.jpg"
        prompt = _translit_prompt(section, category or project_name, "illustrative photo, wide 16:9")
        if not _fetch_pollinations(prompt, path, seed=1000 + i):
            _fallback_card(section, project_name, path)
        paths.append(path)
    return paths


def section_headings(markdown: str) -> list[str]:
    return [m.strip() for m in re.findall(r"^##\s+(.*)$", markdown, flags=re.M)]
