"""Deterministic quality / SEO / brand-compliance gate for generated articles."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
BARE_URL_RE = re.compile(r"(?<!\()https?://[^\s)\]]+")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

AI_CLICHES = [
    "в современном мире", "в заключение", "в наше время", "не секрет, что", "как известно",
    "давайте разберёмся", "давайте разберемся", "в данной статье", "в этой статье мы", "подводя итог",
    "стоит отметить, что", "важно отметить, что", "нельзя не отметить", "игра стоит свеч",
    "революционный", "уникальное решение", "инновационный", "передовой", "погрузимся",
]


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def allowed_domains(project: dict[str, Any]) -> set[str]:
    domains = set()
    main = domain_of(project.get("url", ""))
    if main:
        domains.add(main)
    for extra in str(project.get("extra_domains", "") or "").replace(";", ",").split(","):
        extra = extra.strip().lower()
        if extra:
            domains.add(domain_of(extra) if "://" in extra else extra.removeprefix("www."))
    domains.update({"dzen.ru", "t.me"})
    return domains


def _is_allowed(url: str, allowed: set[str]) -> bool:
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in allowed)


def plain_text(markdown: str) -> str:
    text = re.sub(r"```.*?```", " ", markdown, flags=re.S)
    text = LINK_RE.sub(r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"[*_`>]+", "", text)
    text = re.sub(r"^\s*[-+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.M)
    return re.sub(r"[ \t]+", " ", text).strip()


def sanitize_markdown(markdown: str, project: dict[str, Any]) -> str:
    """Normalise headings, drop H1, strip links to non-whitelisted domains, tidy blank lines."""
    allowed = allowed_domains(project)
    lines_out: list[str] = []
    in_fence = False
    for line in markdown.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            lines_out.append(line)
            continue
        m = HEADING_RE.match(stripped)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip().rstrip(":").strip()
            if level == 1:
                continue  # the title lives in the Dzen title field
            level = min(max(level, 2), 3)
            lines_out.append("#" * level + " " + title)
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            continue
        lines_out.append(line.rstrip())
    text = "\n".join(lines_out)

    def _link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2).rstrip(".,;")
        return m.group(0) if _is_allowed(url, allowed) else label

    text = LINK_RE.sub(_link, text)
    text = BARE_URL_RE.sub(lambda m: m.group(0) if _is_allowed(m.group(0), allowed) else "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


@dataclass
class QualityResult:
    ok: bool
    score: int
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "score": self.score, "problems": self.problems, "warnings": self.warnings,
                "stats": self.stats}


def check_article(*, title: str, description: str, tags: list[str], markdown: str, project: dict[str, Any],
                  knowledge: dict[str, Any], min_chars: int, max_chars: int,
                  primary_keyword: str = "") -> QualityResult:
    problems: list[str] = []
    warnings: list[str] = []
    text = plain_text(markdown)
    chars = len(text)
    name = str(project.get("name", "")).strip()
    lower_text = text.lower()

    # --- size & structure ---------------------------------------------------
    if chars < min_chars:
        problems.append(f"Слишком коротко: {chars} знаков (минимум {min_chars})")
    if chars > max_chars:
        problems.append(f"Слишком длинно: {chars} знаков (максимум {max_chars})")
    h2 = re.findall(r"^##\s+", markdown, flags=re.M)
    if len(h2) < 3:
        problems.append(f"Мало подзаголовков H2: {len(h2)} (нужно ≥3)")
    if re.search(r"^#\s+", markdown, flags=re.M):
        problems.append("Внутри текста есть H1 — заголовок должен быть только в поле title")
    if not re.search(r"^\s*([-+]|\d+\.)\s+\S", markdown, flags=re.M):
        warnings.append("Нет ни одного списка")
    if not re.search(r"^###?\s+.*\?\s*$", markdown, flags=re.M):
        warnings.append("Нет блока FAQ (вопросов-подзаголовков)")

    # --- title / description / tags ----------------------------------------
    t = title.strip()
    if not 15 <= len(t) <= 100:
        problems.append(f"Заголовок {len(t)} символов (нужно 15–100)")
    if t.endswith(".") or t.isupper() or "заголовок" in t.lower():
        problems.append("Заголовок: точка в конце, капс или служебное слово")
    if not 50 <= len(description.strip()) <= 220:
        problems.append(f"Описание {len(description.strip())} символов (нужно 50–220)")
    clean_tags = [x.strip() for x in tags if x and len(x.strip()) <= 30]
    if not 3 <= len(clean_tags) <= 8:
        problems.append(f"Тегов {len(clean_tags)} (нужно 3–8, до 30 символов каждый)")

    # --- brand presence -----------------------------------------------------
    mentions = 0
    if name:
        pattern = re.escape(name.lower())
        mentions = len(re.findall(pattern, lower_text))
        slug = str(project.get("slug", "")).lower()
        if slug and slug != name.lower():
            mentions += len(re.findall(re.escape(slug), lower_text))
    if mentions < 2:
        problems.append(f"Бренд «{name}» упомянут {mentions} раз (нужно ≥2)")
    elif mentions > 9:
        warnings.append(f"Бренд упомянут {mentions} раз — похоже на переспам")

    # --- links --------------------------------------------------------------
    allowed = allowed_domains(project)
    links = [u for _, u in LINK_RE.findall(markdown)] + BARE_URL_RE.findall(markdown)
    bad = [u for u in links if not _is_allowed(u, allowed)]
    if bad:
        problems.append("Ссылки на чужие домены: " + ", ".join(sorted({domain_of(u) for u in bad})))
    main_domain = domain_of(project.get("url", ""))
    if main_domain and not any(domain_of(u) == main_domain or domain_of(u).endswith("." + main_domain) for u in links):
        problems.append(f"Нет ни одной ссылки на сайт {main_domain}")

    # --- competitors --------------------------------------------------------
    competitors = [str(c.get("name") if isinstance(c, dict) else c) for c in (knowledge.get("competitors") or [])]
    found = [c for c in competitors if c and len(c) >= 3 and c.lower() in lower_text]
    if found:
        problems.append("Упомянуты чужие сервисы: " + ", ".join(found))

    # --- style --------------------------------------------------------------
    cliches = [c for c in AI_CLICHES if c in lower_text]
    if len(cliches) > 3:
        problems.append("Много штампов: " + ", ".join(cliches[:5]))
    elif cliches:
        warnings.append("Штампы: " + ", ".join(cliches))
    if re.search(r"[\U0001F300-\U0001FAFF]", markdown):
        warnings.append("В тексте есть эмодзи")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 80]
    if len(paragraphs) != len(set(paragraphs)):
        problems.append("Есть повторяющиеся абзацы")
    if primary_keyword:
        head = (t + " " + text[:400]).lower()
        stem = primary_keyword.lower()[: max(4, int(len(primary_keyword) * 0.7))]
        if stem not in head:
            warnings.append(f"Ключ «{primary_keyword}» не встречается в заголовке/лиде")

    score = max(0, 100 - 12 * len(problems) - 3 * len(warnings))
    return QualityResult(ok=not problems and score >= 80, score=score, problems=problems, warnings=warnings,
                         stats={"chars": chars, "h2": len(h2), "mentions": mentions, "links": len(links)})
