"""Markdown -> Draft.js contentState used by the Dzen article editor.

Block types observed in the editor payload: ``unstyled``, ``header-two``,
``header-three``, ``blockquote``, ``unordered-list-item``, ``ordered-list-item``
and ``atomic:image`` (``data: {"image": {"id": <uploaded image id>}}``).
Inline styles: ``BOLD`` / ``ITALIC`` ranges; links are ``LINK`` entities.
Offsets are UTF-16 code units, as Draft.js (JavaScript) counts them.
"""
from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field
from typing import Any

_INLINE_RE = re.compile(r"(\*\*(?P<b>.+?)\*\*|__(?P<b2>.+?)__|\*(?P<i>[^*\n]+?)\*|_(?P<i2>[^_\n]+?)_|"
                        r"\[(?P<lt>[^\]]+)\]\((?P<lu>https?://[^)\s]+)\)|`(?P<c>[^`]+)`)")


def _key() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass
class Block:
    type: str
    text: str = ""
    inline: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


def parse_inline(raw: str, entity_map: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert inline markdown to plain text + style/entity ranges."""
    out = ""
    styles: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE_RE.finditer(raw):
        out += raw[pos:m.start()]
        start = utf16_len(out)
        if m.group("b") is not None or m.group("b2") is not None:
            inner = m.group("b") or m.group("b2") or ""
            out += inner
            styles.append({"offset": start, "length": utf16_len(inner), "style": "BOLD"})
        elif m.group("i") is not None or m.group("i2") is not None:
            inner = m.group("i") or m.group("i2") or ""
            out += inner
            styles.append({"offset": start, "length": utf16_len(inner), "style": "ITALIC"})
        elif m.group("lt") is not None:
            label, url = m.group("lt"), m.group("lu").rstrip(".,;")
            out += label
            key = str(len(entity_map))
            entity_map[key] = {"type": "LINK", "mutability": "MUTABLE", "data": {"url": url, "href": url}}
            entities.append({"offset": start, "length": utf16_len(label), "key": int(key)})
        elif m.group("c") is not None:
            out += m.group("c")
        pos = m.end()
    out += raw[pos:]
    return out, styles, entities


def markdown_to_blocks(markdown: str, entity_map: dict[str, Any]) -> list[Block]:
    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            text, styles, ents = parse_inline(" ".join(s.strip() for s in para), entity_map)
            if text.strip():
                blocks.append(Block("unstyled", text.strip(), styles, ents))
            para.clear()

    for line in markdown.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush()
            level = len(m.group(1))
            text, styles, ents = parse_inline(m.group(2).strip(), entity_map)
            blocks.append(Block("header-two" if level <= 2 else "header-three", text, styles, ents))
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush()
            continue
        m = re.match(r"^[-+*]\s+(.*)$", stripped)
        if m:
            flush()
            text, styles, ents = parse_inline(m.group(1).strip(), entity_map)
            blocks.append(Block("unordered-list-item", text, styles, ents))
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if m:
            flush()
            text, styles, ents = parse_inline(m.group(1).strip(), entity_map)
            blocks.append(Block("ordered-list-item", text, styles, ents))
            continue
        if stripped.startswith(">"):
            flush()
            text, styles, ents = parse_inline(stripped.lstrip("> ").strip(), entity_map)
            blocks.append(Block("blockquote", text, styles, ents))
            continue
        para.append(stripped)
    flush()
    return blocks


def image_anchor_positions(blocks: list[Block], count: int) -> list[int]:
    """Indices (into ``blocks``) *before which* inline images are inserted.

    First image goes after the first section heading's first paragraph; the
    rest are spread over the remaining body, always right before a heading so
    an image never splits a list.
    """
    if count <= 0 or not blocks:
        return []
    headings = [i for i, b in enumerate(blocks) if b.type in ("header-two", "header-three")]
    if len(headings) < 2:
        return [min(len(blocks), 2)] if count else []
    anchors: list[int] = []
    # Candidate slots: right before each heading except the very first block.
    candidates = [h for h in headings if h > 1]
    if not candidates:
        return [min(len(blocks), 2)]
    for n in range(count):
        ratio = (n + 1) / (count + 1)
        target = round(len(blocks) * ratio)
        best = min(candidates, key=lambda h: abs(h - target))
        if best not in anchors:
            anchors.append(best)
            candidates = [c for c in candidates if c != best]
        if not candidates:
            break
    return sorted(anchors)


def build_content_state(markdown: str, image_ids: list[str] | None = None) -> dict[str, Any]:
    entity_map: dict[str, Any] = {}
    blocks = markdown_to_blocks(markdown, entity_map)
    image_ids = [x for x in (image_ids or []) if x]
    anchors = image_anchor_positions(blocks, len(image_ids))
    out: list[dict[str, Any]] = []
    img_iter = iter(image_ids)
    anchor_set = set(anchors)
    for idx, block in enumerate(blocks):
        if idx in anchor_set:
            img = next(img_iter, None)
            if img:
                out.append(_atomic_image(img))
        out.append({
            "key": _key(), "text": block.text, "type": block.type, "depth": 0,
            "inlineStyleRanges": block.inline, "entityRanges": block.entities, "data": block.data,
        })
    for img in img_iter:  # leftovers (short article) go to the end
        out.append(_atomic_image(img))
    return {"blocks": out, "entityMap": entity_map}


def _atomic_image(image_id: str) -> dict[str, Any]:
    return {"key": _key(), "text": " ", "type": "atomic:image", "depth": 0, "inlineStyleRanges": [],
            "entityRanges": [], "data": {"image": {"id": image_id}}}


def _inline_html(raw: str) -> str:
    """Inline markdown -> inline HTML (bold/italic/links/code), HTML-escaped."""
    import html as _html

    out = ""
    pos = 0
    for m in _INLINE_RE.finditer(raw):
        out += _html.escape(raw[pos:m.start()])
        if m.group("b") is not None or m.group("b2") is not None:
            out += f"<b>{_html.escape(m.group('b') or m.group('b2') or '')}</b>"
        elif m.group("i") is not None or m.group("i2") is not None:
            out += f"<i>{_html.escape(m.group('i') or m.group('i2') or '')}</i>"
        elif m.group("lt") is not None:
            url = _html.escape(m.group("lu").rstrip(".,;"), quote=True)
            out += f'<a href="{url}">{_html.escape(m.group("lt"))}</a>'
        elif m.group("c") is not None:
            out += f"<code>{_html.escape(m.group('c'))}</code>"
        pos = m.end()
    out += _html.escape(raw[pos:])
    return out


def markdown_to_html(markdown: str) -> str:
    """Markdown -> semantic HTML for CLIPBOARD PASTE into Draft.js. Draft.js's
    default paste handler parses h1-h6/b/i/ul/ol/li/blockquote/a on paste into
    real blocks — this is how content reaches the editor when driving the
    real UI (as opposed to build_content_state, which builds the raw block
    JSON directly for API calls)."""
    html_parts: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            text = _inline_html(" ".join(s.strip() for s in para))
            if text.strip():
                html_parts.append(f"<p>{text}</p>")
            para.clear()

    list_buffer: list[str] = []
    list_tag: str | None = None

    def flush_list() -> None:
        nonlocal list_tag
        if list_buffer and list_tag:
            items = "".join(f"<li>{item}</li>" for item in list_buffer)
            html_parts.append(f"<{list_tag}>{items}</{list_tag}>")
        list_buffer.clear()
        list_tag = None

    for line in markdown.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            flush()
            flush_list()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush()
            flush_list()
            level = len(m.group(1))
            tag = "h2" if level <= 2 else "h3"
            html_parts.append(f"<{tag}>{_inline_html(m.group(2).strip())}</{tag}>")
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush()
            flush_list()
            continue
        m = re.match(r"^[-+*]\s+(.*)$", stripped)
        if m:
            flush()
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            list_buffer.append(_inline_html(m.group(1).strip()))
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if m:
            flush()
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            list_buffer.append(_inline_html(m.group(1).strip()))
            continue
        if stripped.startswith(">"):
            flush()
            flush_list()
            html_parts.append(f"<blockquote>{_inline_html(stripped.lstrip('> ').strip())}</blockquote>")
            continue
        flush_list()
        para.append(stripped)
    flush()
    flush_list()
    return "\n".join(html_parts)


def snippet(markdown: str, limit: int = 200) -> str:
    text = re.sub(r"^#{1,6}\s+.*$", "", markdown, flags=re.M)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`>#-]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > 60 else limit].rstrip(".,;:") + "…"
