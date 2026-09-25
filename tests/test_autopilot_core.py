"""Fast, no-network unit tests for the pure-logic parts of the autopilot."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopilot.db import Database
from autopilot.draftjs import build_content_state, snippet, utf16_len
from autopilot.llm import extract_json
from autopilot.quality import allowed_domains, check_article, plain_text, sanitize_markdown
from autopilot.topics import _too_similar

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


def test_too_similar_catches_near_duplicate_titles():
    history = ["Как настроить автоответчик в WhatsApp для бизнеса"]
    # Same content words, just reordered/reworded connectors -> caught.
    assert _too_similar("Автоответчик для бизнеса в WhatsApp: как настроить", history)
    # Genuinely different topic -> not flagged.
    assert not _too_similar("Сколько стоит доставка цветов в декабре", history)


def test_plain_text_drops_markup():
    text = plain_text("## Заголовок\n\n**Жирный** текст с [ссылкой](https://x.com) и `код`.")
    assert "#" not in text and "**" not in text and "[" not in text


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
