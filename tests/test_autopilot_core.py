"""Fast, no-network unit tests for the pure-logic parts of the autopilot."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopilot.db import Database
from autopilot.draftjs import build_content_state, markdown_to_html, snippet, utf16_len
from autopilot.images import collect_real_image_urls
from autopilot.llm import extract_json
from autopilot.quality import allowed_domains, check_article, plain_text, sanitize_markdown
from autopilot.research import _real_images
from autopilot.topics import _too_similar, generate_topics
from bs4 import BeautifulSoup

PROJECT = {"name": "BizGateWay", "slug": "bizgateway", "url": "https://bizgateway.pro", "extra_domains": ""}


def _article_markdown() -> str:
    return (
        "# Заголовок\n\n"
        "## Почему это важно\n\n"
        + ("Текст BizGateWay помогает бизнесу отвечать быстрее клиентам в WhatsApp. " * 20)
        + "\n\n- пункт один\n- пункт два\n\n## Как это работает\n\n"
        + ("Ещё текст про сценарии работы и интеграции с CRM. " * 20)
        + "\n\n## Опыт клиентов\n\n"
        + ("История клиента и измеримый результат внедрения. " * 15)
        + "\n\n### Сколько это стоит?\n\nОтвет по тарифам. "
          "Подробнее на [сайте BizGateWay](https://bizgateway.pro/pricing) "
          "и у [конкурента](https://wazzup24.com).\n"
    )


def test_sanitize_strips_h1_and_foreign_links():
    clean = sanitize_markdown(_article_markdown(), PROJECT)
    assert not clean.lstrip().startswith("# ")
    assert "wazzup24" not in clean
    assert "bizgateway.pro/pricing" in clean


def test_allowed_domains_includes_extra_and_dzen():
    domains = allowed_domains({**PROJECT, "extra_domains": "shop.bizgateway.pro, other.example.com"})
    assert "bizgateway.pro" in domains
    assert "shop.bizgateway.pro" in domains
    assert "dzen.ru" in domains


def test_check_article_passes_clean_article_and_flags_competitor():
    clean = sanitize_markdown(_article_markdown(), PROJECT)
    ok_result = check_article(
        title="Как быстро отвечать клиентам в WhatsApp и не терять заявки",
        description="Разбираем, как настроить единый инбокс и автоответы для WhatsApp через API и не терять заявки.",
        tags=["whatsapp", "api", "бизнес", "поддержка"],
        markdown=clean, project=PROJECT, knowledge={"competitors": [{"name": "Wazzup"}]},
        min_chars=1500, max_chars=9000, primary_keyword="whatsapp для бизнеса",
    )
    assert ok_result.ok, ok_result.problems

    with_competitor = clean.replace("Опыт клиентов", "Опыт клиентов Wazzup")
    bad_result = check_article(
        title="Как быстро отвечать клиентам в WhatsApp и не терять заявки",
        description="Разбираем, как настроить единый инбокс и автоответы для WhatsApp через API и не терять заявки.",
        tags=["whatsapp", "api", "бизнес"],
        markdown=with_competitor, project=PROJECT, knowledge={"competitors": [{"name": "Wazzup"}]},
        min_chars=1500, max_chars=9000,
    )
    assert not bad_result.ok
    assert any("Wazzup" in p for p in bad_result.problems)


def test_check_article_rejects_too_short():
    result = check_article(title="Короткий заголовок статьи про сервис", description="Описание статьи " * 5,
                           tags=["a", "b", "c"], markdown="## Раздел\n\nПара предложений.\n",
                           project=PROJECT, knowledge={}, min_chars=3000, max_chars=9000)
    assert not result.ok
    assert any("Слишком коротко" in p for p in result.problems)


def test_draftjs_inline_styles_links_and_images():
    md = "## Раздел\n\nТекст с **жирным** и [ссылкой](https://bizgateway.pro/x) 😀.\n\n- один\n- два\n\n## Второй\n\nЕщё."
    cs = build_content_state(md, ["img1", "img2"])
    types = [b["type"] for b in cs["blocks"]]
    assert types.count("atomic:image") == 2
    text_block = next(b for b in cs["blocks"] if b["inlineStyleRanges"])
    assert text_block["inlineStyleRanges"][0]["style"] == "BOLD"
    entity_key = str(text_block["entityRanges"][0]["key"])
    assert cs["entityMap"][entity_key]["data"]["url"] == "https://bizgateway.pro/x"


def test_markdown_to_html_converts_semantic_blocks_for_paste():
    md = "## Раздел\n\nТекст с **жирным** и [ссылкой](https://x.com/a).\n\n- один\n- два\n\n> цитата\n\n1. раз\n2. два"
    html = markdown_to_html(md)
    assert "<h2>Раздел</h2>" in html
    assert "<b>жирным</b>" in html
    assert '<a href="https://x.com/a">ссылкой</a>' in html
    assert "<ul><li>один</li><li>два</li></ul>" in html
    assert "<blockquote>цитата</blockquote>" in html
    assert "<ol><li>раз</li><li>два</li></ol>" in html


def test_markdown_to_html_escapes_special_characters():
    html = markdown_to_html("Текст с <тегом> и & амперсандом")
    assert "&lt;тегом&gt;" in html and "&amp;" in html


def test_draftjs_utf16_offsets_count_surrogate_pairs():
    assert utf16_len("😀") == 2
    assert utf16_len("привет") == 6


def test_snippet_strips_markdown_and_truncates():
    text = snippet("## H\n\nСлово " * 60, limit=50)
    assert len(text) <= 51
    assert "#" not in text


def test_extract_json_tolerates_fences_and_trailing_prose():
    assert extract_json('```json\n{"a": 1,}\n```') == {"a": 1}
    assert extract_json("Вот ответ: [1,2,3] и всё") == [1, 2, 3]


def test_extract_json_finds_real_object_past_a_stray_bracket_list():
    # Regression: a live free-model response opened with a keyword list in
    # its preamble before the actual answer object; naive first-'{'-to-
    # last-'}' matching grabbed nothing valid and fell back to that list.
    text = (
        'Ключевые слова: ["BeatScope", "офлайн анализ", "SPL метр"]\n\n'
        'Вот статья:\n{"title": "Заголовок статьи", "tags": ["a", "b"], "markdown": "## Текст"}'
    )
    result = extract_json(text)
    assert isinstance(result, dict) and result["title"] == "Заголовок статьи"


def test_extract_json_prefers_largest_balanced_object():
    text = 'Пример: {"x": 1}\n\nОтвет: {"title": "T", "markdown": "## body text here", "tags": []}'
    result = extract_json(text)
    assert result["title"] == "T"


def test_too_similar_catches_near_duplicate_titles():
    history = ["Как настроить автоответчик в WhatsApp для бизнеса"]
    # Same content words, just reordered/reworded connectors -> caught.
    assert _too_similar("Автоответчик для бизнеса в WhatsApp: как настроить", history)
    # Genuinely different topic -> not flagged.
    assert not _too_similar("Сколько стоит доставка цветов в декабре", history)


def test_plain_text_drops_markup():
    text = plain_text("## Заголовок\n\n**Жирный** текст с [ссылкой](https://x.com) и `код`.")
    assert "#" not in text and "**" not in text and "[" not in text


def test_real_images_skips_icons_and_tiny_images():
    html = """
    <html><body>
      <img src="/logo.png" width="600" height="400">
      <img src="/content/photo.jpg" width="800" height="500">
      <img src="/icons/favicon.ico" width="800" height="500">
      <img src="/tiny.jpg" width="50" height="50">
      <img src="/no-size.jpg">
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    urls = _real_images(soup, "https://bizgateway.pro/blog/post")
    assert urls == [
        "https://bizgateway.pro/content/photo.jpg",
        "https://bizgateway.pro/no-size.jpg",
    ]


def test_collect_real_image_urls_dedupes_across_pages_and_respects_limit():
    pages = [
        {"images": ["https://x.com/a.jpg", "https://x.com/b.jpg"]},
        {"images": ["https://x.com/b.jpg", "https://x.com/c.jpg"]},
    ]
    assert collect_real_image_urls(pages) == ["https://x.com/a.jpg", "https://x.com/b.jpg", "https://x.com/c.jpg"]
    assert collect_real_image_urls(pages, limit=2) == ["https://x.com/a.jpg", "https://x.com/b.jpg"]


class _StubLLM:
    """Minimal stand-in for autopilot.llm.LLM — generate_topics only calls .json()."""

    def __init__(self, response):
        self._response = response

    def json(self, *args, **kwargs):
        return self._response


def test_generate_topics_news_mode_drops_topics_without_a_real_source():
    news = [{"title": "New WhatsApp API limits announced", "link": "https://techcrunch.com/a",
            "summary": "...", "source": "techcrunch.com", "published": "2026-09-20T00:00:00+00:00"}]
    llm = _StubLLM([
        {"title": "Что значат новые лимиты WhatsApp API для бизнеса", "format": "новость",
         "source_title": news[0]["title"], "source_link": news[0]["link"], "score": 80},
        {"title": "Придуманная тема без реального источника", "format": "новость",
         "source_title": "Придумано", "source_link": "https://not-a-real-source.example/x", "score": 90},
    ])
    result = generate_topics(llm, {"name": "BizGateWay", "url": "https://bizgateway.pro"}, {}, [], [],
                             count=5, news_items=news)
    assert len(result) == 1
    assert result[0]["source_link"] == news[0]["link"]


def test_generate_topics_evergreen_mode_allows_empty_source():
    llm = _StubLLM([{"title": "Эволюционная тема без новостного повода", "format": "боль-решение", "score": 70}])
    result = generate_topics(llm, {"name": "BeatScope", "url": "https://beatscope.pro"}, {}, [], [], count=5,
                             news_items=[])
    assert len(result) == 1 and result[0]["source_link"] == ""


def test_db_round_trip_project_topic_article():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        project = db.upsert_project("demo", "Demo", "https://demo.test", "brief", daily_quota=10)
        db.add_topics(project["id"], [{"title": "Тема раз", "score": 5}, {"title": "Тема два", "score": 9}])
        topic = db.claim_topic(project["id"])
        assert topic["title"] == "Тема два"
        article_id = db.add_article(project["id"], topic["id"], title="X", description="d", tags=["x"],
                                    markdown="## x\n\ntext", quality={}, cover_path="", images=[])
        claimed = db.claim_article(project["id"])
        assert claimed and claimed["id"] == article_id
        db.update_article(article_id, status="published", dzen_url="https://dzen.ru/a/1",
                          published_at="2026-09-25T10:00:00+00:00")
        assert db.published_links(project["id"])[0]["dzen_url"] == "https://dzen.ru/a/1"
        assert db.count_topics(project["id"]) == 1
