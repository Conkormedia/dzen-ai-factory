"""One-off discovery/attempt: drive the REAL Dzen editor UI to paste content,
insert an image, and click publish - so the browser computes its own real
fp token instead of us fabricating one. Screenshots go to /tmp/diag-*.png
for visual inspection. Not part of the pipeline yet - see NOTES_ON_PUBLISHING.md.

Usage: python -m autopilot.diag_ui_publish <publication_id> <image_path>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import settings
from .db import Database
from .draftjs import markdown_to_html
from .dzen_client import EDITOR, DzenClient

SHOT_DIR = Path("/tmp")


def shot(page, name: str) -> None:
    path = SHOT_DIR / f"diag-{name}.png"
    page.screenshot(path=str(path))
    print(f"[diag] screenshot -> {path}")


def run(publication_id: str, image_path: str) -> None:
    db = Database(settings.db_path)
    publisher_id = db.get_setting("dzen_publisher_id", "")
    print(f"[diag] publisher_id={publisher_id} publication_id={publication_id}")

    title = "UI-тест публикации BeatScope"
    markdown = (
        "## Проверка автопубликации\n\n"
        "Это технический тест **автопилота**, вставленный через буфер обмена.\n\n"
        "- пункт один\n- пункт два\n\n"
        "> контрольная цитата\n\n"
        "[Сайт BeatScope](https://beatscope.pro)"
    )
    html_body = markdown_to_html(markdown)
    print("[diag] html_body:\n", html_body)

    with DzenClient(settings) as client:
        page = client._page  # noqa: SLF001
        client._ctx.grant_permissions(["clipboard-read", "clipboard-write"])  # noqa: SLF001

        url = f"{EDITOR}/id/{publisher_id}/{publication_id}/edit"
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)
        shot(page, "01-loaded")

        try:
            page.keyboard.press("Escape")
            overlay = page.query_selector("[class*='help-popup__overlay']")
            if overlay:
                overlay.click(force=True, position={"x": 5, "y": 5}, timeout=5000)
                page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001
            print("[diag] overlay dismiss:", exc)
        shot(page, "02-overlay-dismissed")

        editables = page.query_selector_all("[contenteditable='true']")
        print(f"[diag] contenteditable count={len(editables)}")
        if len(editables) < 2:
            print("[diag] not enough editable nodes, aborting")
            return

        def paste_html(el, html: str, plain: str) -> None:
            el.click(force=True, timeout=10000)
            page.keyboard.press("Control+a")
            page.evaluate(
                """async ({html, text}) => {
                    const item = new ClipboardItem({
                      'text/html': new Blob([html], {type: 'text/html'}),
                      'text/plain': new Blob([text], {type: 'text/plain'}),
                    });
                    await navigator.clipboard.write([item]);
                }""",
                {"html": html, "text": plain},
            )
            page.keyboard.press("Control+v")
            page.wait_for_timeout(800)

        print("[diag] pasting title...")
        paste_html(editables[0], f"<p>{title}</p>", title)
        shot(page, "03-title-pasted")

        print("[diag] pasting body...")
        paste_html(editables[1], html_body, markdown)
        page.wait_for_timeout(1000)
        shot(page, "04-body-pasted")

        body_text = page.inner_text("body")
        print("[diag] body contains 'пункт один':", "пункт один" in body_text)
        print("[diag] body contains cited link text 'Сайт BeatScope':", "Сайт BeatScope" in body_text)

        # Look for an image-insertion control near the toolbar.
        try:
            buttons_info = page.eval_on_selector_all(
                "button, [role='button']",
                """els => els.map(e => ({
                    text: e.innerText.trim(),
                    aria: e.getAttribute('aria-label') || '',
                    title: e.getAttribute('title') || '',
                    cls: e.className,
                })).filter(b => b.text || b.aria || b.title)""",
            )
            print("[diag] labeled buttons:", json.dumps(buttons_info, ensure_ascii=False)[:3000])
        except Exception as exc:  # noqa: BLE001
            print("[diag] button probe failed:", exc)

        file_inputs: list = []
        try:
            file_inputs = page.eval_on_selector_all(
                "input[type=file]", "els => els.map(e => ({accept: e.accept, cls: e.className}))"
            )
            print("[diag] file inputs:", json.dumps(file_inputs, ensure_ascii=False)[:1000])
        except Exception as exc:  # noqa: BLE001
            print("[diag] file input probe failed:", exc)

        shot(page, "05-before-image")

        if file_inputs:
            try:
                fi = page.query_selector("input[type=file]")
                fi.set_input_files(image_path)
                page.wait_for_timeout(3000)
                shot(page, "06-after-image-set-files")
            except Exception as exc:  # noqa: BLE001
                print("[diag] set_input_files failed:", exc)

        print("[diag] done - inspect screenshots and the button/file-input dump above")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m autopilot.diag_ui_publish <publication_id> <image_path>")
    run(sys.argv[1], sys.argv[2])
