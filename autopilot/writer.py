"""LLM article writer + deterministic repair loop, gated by autopilot.quality."""
from __future__ import annotations

import logging

from .llm import LLM, LLMError
from .quality import check_article, sanitize_markdown

log = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2

SYSTEM_PROMPT = """Ты — опытный редактор-копирайтер, пишущий статьи для Яндекс.Дзен на русском языке.

ЖЁСТКИЕ ПРАВИЛА:
1. Ты продвигаешь РОВНО ОДИН сервис — тот, что указан в задании. Никогда не хвалишь, не советуешь
   и не упоминаешь по имени никакие другие сервисы/бренды/приложения, даже вскользь и даже в примерах.
2. Все ссылки в тексте — только на официальный сайт продвигаемого сервиса (указанный домен) или на
   Дзен/Телеграм самого проекта. Не выдумывай ссылки на другие ресурсы.
3. Не выдумывай цифры, отзывы, кейсы, даты выхода функций — если не уверен, пиши обобщённо.
4. Статья должна реально решать боль читателя, а не быть рекламной листовкой: минимум 70% текста —
   полезная, самостоятельно ценная информация; сервис органично появляется как способ решить проблему.
5. Структура: без H1 (заголовок передаётся отдельно), 4-6 подзаголовков H2/H3, минимум один список,
   один блок из 1-2 вопросов-ответов (FAQ) ближе к концу, естественные абзацы 2-5 предложений.
6. Никаких канцеляризмов и ИИ-штампов ("в современном мире", "давайте разберёмся", "в заключение",
   "не секрет, что" и т.п.). Пиши как живой профильный автор, разговорно-деловой тон.
7. Упомяни продвигаемый сервис по имени 2-4 раза органично (не как рекламный слоган).
8. В конце — мягкий, но конкретный призыв к действию по заданному углу (cta_angle).

Отвечай СТРОГО валидным JSON без пояснений и markdown-ограждений, со следующими полями:
{
  "title": "заголовок статьи, 40-90 символов, без точки в конце, без КАПСА",
  "description": "SEO-описание для Дзена, 120-200 символов, раскрывает суть без кликбейта",
  "tags": ["3-6 тегов, каждый до 25 символов, по-русски, без решёток"],
  "markdown": "текст статьи в Markdown: ## и ### заголовки, списки -, обычные абзацы, [текст](url) для ссылок"
}"""


def _topic_brief(project: dict, topic: dict, knowledge: dict) -> str:
    links = knowledge.get("internal_links", []) or []
    links_text = "\n".join(
        f"  - {l.get('url', '')} — {l.get('title', '')} (когда уместна: {l.get('use_for', '')})"
        for l in links if isinstance(l, dict) and l.get("url")
    ) or "  (кроме главной страницы сайта, других страниц не знаю — используй только главный URL)"
    facts = "; ".join(knowledge.get("facts", []) or []) or "(нет проверенных фактов — пиши обобщённо, без цифр)"
    cta_variants = ", ".join(knowledge.get("cta_variants", []) or [])
    return (
        f"ПРОДВИГАЕМЫЙ СЕРВИС: {project['name']}\n"
        f"Официальный сайт (единственный разрешённый домен для ссылок, помимо строк ниже): {project.get('url', '')}\n"
        f"Суть сервиса: {knowledge.get('summary', '')}\n"
        f"Ценности: {', '.join(knowledge.get('value_props', []) or [])}\n"
        f"Фичи, которые можно упоминать: {', '.join(knowledge.get('features', []) or [])}\n"
        f"Проверенные факты (используй ТОЛЬКО их для цифр/конкретики, не выдумывай новые): {facts}\n"
        f"Тон: {knowledge.get('tone') or project.get('tone') or 'дружелюбно-экспертный'}\n"
        f"Страницы сайта, на которые уместно ссылаться (используй 1-2 по месту, точный URL):\n{links_text}\n"
        f"Варианты CTA на выбор: {cta_variants or 'придумай уместный по контексту темы'}\n\n"
        f"ТЕМА СТАТЬИ: {topic['title']}\n"
        f"Формат: {topic.get('format', '')}\n"
        f"Боль читателя: {topic.get('pain', '')}\n"
        f"Обещание статьи: {topic.get('promise', '')}\n"
        f"Ключевые слова для SEO (используй естественно, не переспамь): {', '.join(topic.get('keywords', []) or [])}\n"
        f"Примерный план: {'; '.join(topic.get('outline', []) or [])}\n"
        f"Угол призыва к действию в конце: {topic.get('cta_angle', '')}\n"
    )


def write_article(llm: LLM, project: dict, topic: dict, knowledge: dict, *,
                  min_chars: int, target_chars: int, max_chars: int) -> dict:
    """Generate an article and push it through the quality gate with repairs.

    Returns {"title","description","tags","markdown","quality"}; raises LLMError
    if it still fails the gate after MAX_REPAIR_ATTEMPTS repairs.
    """
    brief = _topic_brief(project, topic, knowledge)
    user_prompt = (
        f"{brief}\n"
        f"Целевой объём текста (без учёта markdown-разметки): примерно {target_chars} знаков "
        f"(не менее {min_chars}, не более {max_chars})."
    )
    data = llm.json(SYSTEM_PROMPT, user_prompt, purpose="write_article", max_tokens=16000)
    data = _coerce(data)

    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        clean_md = sanitize_markdown(data["markdown"], project)
        quality = check_article(
            title=data["title"], description=data["description"], tags=data["tags"], markdown=clean_md,
            project=project, knowledge=knowledge, min_chars=min_chars, max_chars=max_chars,
            primary_keyword=(topic.get("keywords") or [""])[0],
        )
        data["markdown"] = clean_md
        if quality.ok or attempt == MAX_REPAIR_ATTEMPTS:
            return {**data, "quality": quality.as_dict()}
        log.info("Article failed quality gate (attempt %s): %s", attempt, quality.problems)
        data = _repair(llm, project, data, quality.problems + quality.warnings, min_chars, max_chars)

    raise LLMError("unreachable")  # pragma: no cover


def _repair(llm: LLM, project: dict, data: dict, problems: list[str], min_chars: int, max_chars: int) -> dict:
    prompt = (
        f"Вот черновик статьи для {project['name']} в формате JSON:\n"
        f"{{\"title\": {data['title']!r}, \"description\": {data['description']!r}, "
        f"\"tags\": {data['tags']!r}, \"markdown\": {data['markdown']!r}}}\n\n"
        f"Редактор нашёл проблемы, исправь ВСЕ, не потеряв объём (мин. {min_chars}, макс. {max_chars} знаков):\n"
        + "\n".join(f"- {p}" for p in problems)
        + "\n\nВерни исправленный JSON той же структуры (title/description/tags/markdown), только JSON."
    )
    fixed = llm.json(SYSTEM_PROMPT, prompt, purpose="repair_article", max_tokens=16000)
    return _coerce(fixed)


def _coerce(data: object) -> dict:
    # Free models sometimes wrap the single requested object in an array
    # despite the schema — unwrap rather than fail a whole article over it.
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise LLMError(f"Ответ модели не является объектом со статьёй: {type(data).__name__} {str(data)[:200]!r}")
    title = str(data.get("title") or "").strip()
    description = str(data.get("description") or "").strip()
    tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
    markdown = str(data.get("markdown") or "").strip()
    if not title or not markdown:
        raise LLMError("В ответе модели нет title или markdown")
    return {"title": title, "description": description, "tags": tags, "markdown": markdown}
