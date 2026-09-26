"""Brief-driven topic generation: turns a project's knowledge dossier into a
bank of concrete, non-repeating article topics that promote ONLY that project."""
from __future__ import annotations

import logging
import re

from .llm import LLM

log = logging.getLogger(__name__)

FORMATS = [
    "боль-решение", "сравнение форматов работы (не брендов)", "пошаговый гайд",
    "разбор кейса/сценария использования", "мифы и заблуждения", "чек-лист",
    "до/после (как было без сервиса и как стало)", "ответ на частый вопрос аудитории",
    "тренд рынка и при чём здесь сервис", "экономика/выгода в цифрах",
]


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[а-яёa-z0-9]{4,}", text.lower()))


def _too_similar(title: str, existing: list[str], threshold: float = 0.6) -> bool:
    tt = _tokens(title)
    if not tt:
        return False
    for other in existing:
        ot = _tokens(other)
        if not ot:
            continue
        overlap = len(tt & ot) / max(1, min(len(tt), len(ot)))
        if overlap >= threshold:
            return True
    return False


def generate_topics(llm: LLM, project: dict, knowledge: dict, mined_titles: list[str],
                    history: list[str], *, count: int = 20, news_items: list[dict] | None = None) -> list[dict]:
    """Ask the LLM for a batch of topics; dedupe against title history locally.

    When news_items (real RSS entries) are supplied, topics are grounded in a
    SPECIFIC real item each — this is the "news" mode: timely commentary on a
    real, dated event, tying back to the service, rather than an invented
    evergreen angle. Falls back to evergreen-only when no news is available.
    """
    inspiration = "\n".join(f"- {t}" for t in mined_titles[:30]) or "(нет данных)"
    avoid = "\n".join(f"- {t}" for t in history[-80:]) or "(пока пусто)"

    pillars = ", ".join(knowledge.get("content_pillars", []) or [])
    clusters = "; ".join(
        f"{c.get('cluster', '')} ({', '.join(c.get('keywords', []) or [])})"
        for c in (knowledge.get("seo_clusters", []) or []) if isinstance(c, dict)
    )
    news = news_items or []
    news_block = "\n".join(
        f"[{i}] {n['title']} — {n.get('source', '')} ({n.get('published', '')[:10]})\n"
        f"    {n.get('summary', '')[:250]}\n    Ссылка: {n['link']}"
        for i, n in enumerate(news[:40], 1)
    ) or "(за последние дни релевантных новостей не нашлось)"

    news_instruction = (
        f"РЕЖИМ: НОВОСТИ. Ниже — реальные свежие материалы отрасли. Для КАЖДОЙ темы возьми ОДИН конкретный\n"
        f"пункт из списка (по номеру) и построй тему как новостной разбор: что произошло, почему это важно\n"
        f"аудитории, и как {project['name']} помогает в этом контексте. Не выдумывай событий, которых нет\n"
        f"в списке. Заполни source_title и source_link ТОЧНО как в выбранном пункте (title и Ссылка).\n"
        f"Каждый пункт использовать не больше одного раза. Если пунктов меньше, чем {count} — сделай темы\n"
        f"только по реально доступным пунктам, не растягивай на выдуманные, верни меньше тем, это нормально.\n\n"
        f"Материалы:\n{news_block}\n\n"
    ) if news else (
        "РЕЖИМ: evergreen (новостей по теме сейчас нет). source_title и source_link оставь пустыми строками.\n\n"
    )

    prompt = (
        f"Продвигаемый сервис (ЕДИНСТВЕННЫЙ, который можно рекламировать): {project['name']}\n"
        f"Сайт: {project.get('url', '')}\n"
        f"Суть: {knowledge.get('summary', '')}\n"
        f"Ценности: {', '.join(knowledge.get('value_props', []) or [])}\n"
        f"Аудитория: {', '.join(knowledge.get('audience', []) or [])}\n"
        f"Фичи: {', '.join(knowledge.get('features', []) or [])}\n"
        f"Какие боли решает: {'; '.join(knowledge.get('pains', []) or [])}\n"
        f"Возражения клиентов: {'; '.join(knowledge.get('objections', []) or [])}\n"
        f"Контентные направления: {pillars or '(не заданы)'}\n"
        f"SEO-кластеры: {clusters or '(не заданы)'}\n\n"
        f"{news_instruction}"
        f"Заголовки статей конкурентов и рынка — ТОЛЬКО как источник вдохновения по темам,\n"
        f"конкурентов по имени упоминать в статьях НЕЛЬЗЯ:\n{inspiration}\n\n"
        f"Уже использованные/предложенные темы — НЕ повторяй их и не перефразируй:\n{avoid}\n\n"
        f"Сгенерируй до {count} РАЗНЫХ тем статей для Яндекс Дзен, каждая продвигает {project['name']}.\n"
        "Требования к каждой теме:\n"
        "- конкретный, не шаблонный заголовок (без кликбейт-капса, без \"топ-10\" на автомате)\n"
        "- реальная боль аудитории, которую статья закрывает, и как именно сервис её решает\n"
        "- 4-7 SEO-ключевых слов/фраз, которые аудитория реально ищет\n"
        "- краткий outline из 4-6 пунктов (подзаголовков будущей статьи)\n"
        "- угол для призыва к действию (cta_angle) — что именно предложить в конце\n\n"
        "Верни JSON-массив объектов:\n"
        '[{"title":"...", "format":"новость", "pain":"...", "promise":"...", '
        '"keywords":["...","..."], "outline":["...","..."], "cta_angle":"...", '
        '"source_title":"...", "source_link":"...", "score": 0-100}]\n'
        "score — твоя оценка потенциала темы (охват+конверсия), 0-100."
    )
    raw = llm.json(
        "Ты новостной редактор и SEO-контент-стратег. Пишешь только по реальным материалам, которые тебе дали, "
        "никогда не выдумываешь события. Никогда не рекламируешь никого, кроме указанного сервиса.",
        prompt, purpose="topic_generation", max_tokens=16000,
    )
    items = raw if isinstance(raw, list) else raw.get("topics", []) if isinstance(raw, dict) else []

    valid_links = {n["link"] for n in news}
    out: list[dict] = []
    seen_local: list[str] = []
    used_links: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title or len(title) < 10:
            continue
        if _too_similar(title, history) or _too_similar(title, seen_local):
            continue
        source_link = str(item.get("source_link") or "").strip()
        if news:
            # In news mode a topic without a real, distinct source is dropped
            # rather than silently allowed to drift into invented "news".
            if source_link not in valid_links or source_link in used_links:
                continue
            used_links.add(source_link)
        seen_local.append(title)
        out.append({
            "title": title,
            "format": str(item.get("format") or "")[:60],
            "pain": str(item.get("pain") or "")[:400],
            "promise": str(item.get("promise") or "")[:400],
            "keywords": [str(k)[:60] for k in (item.get("keywords") or [])][:8],
            "outline": [str(o)[:200] for o in (item.get("outline") or [])][:8],
            "cta_angle": str(item.get("cta_angle") or "")[:300],
            "source_title": str(item.get("source_title") or "")[:250],
            "source_link": source_link[:500],
            "score": float(item.get("score") or 50) or 50.0,
        })
    return out


def ensure_topic_bank(llm: LLM, db, project: dict, knowledge: dict, mined_titles: list[str], *,
                      minimum: int = 6, batch: int = 12, news_items: list[dict] | None = None) -> int:
    """Top up a project's topic bank if it is running low. Returns topics added."""
    if db.count_topics(project["id"], status="new") >= minimum:
        return 0
    history = db.topic_titles(project["id"], limit=300)
    topics = generate_topics(llm, project, knowledge, mined_titles, history, count=batch, news_items=news_items)
    if not topics:
        return 0
    return db.add_topics(project["id"], topics)
