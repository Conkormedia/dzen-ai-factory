"""One-off: reproduce the repeated 'publish click did not navigate to a
published article' failure with screenshots at each step, on a fresh throwaway
draft (never touches a real article's content).

Usage: python -m autopilot.diag_publish_fail
"""
from __future__ import annotations

import json

from .config import settings
from .db import Database
from .dzen_client import DzenClient, EDITOR


def shot(page, name: str) -> None:
    path = f"/tmp/pf-{name}.png"
    page.screenshot(path=path)
    print(f"[diag] screenshot -> {path}")


def run() -> None:
    db = Database(settings.db_path)
    publisher_id = db.get_setting("dzen_publisher_id", "")
    print(f"[diag] publisher_id={publisher_id}")

    with DzenClient(settings) as client:
        publication_id = client.create_draft(publisher_id)
        print(f"[diag] draft={publication_id}")
        page = client._page  # noqa: SLF001
        client._ctx.grant_permissions(["clipboard-read", "clipboard-write"])  # noqa: SLF001

        url = f"{EDITOR}/id/{publisher_id}/{publication_id}/edit"
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2000)
        client._dismiss_help_overlay()  # noqa: SLF001
        shot(page, "01-loaded")

        editables = page.query_selector_all("[contenteditable='true']")
        print(f"[diag] contenteditable count={len(editables)}")
        client._paste_html(editables[0], "<p>Диагностика паблиша</p>", "Диагностика паблиша")  # noqa: SLF001
        client._paste_html(editables[1], "<p>Тестовый текст для диагностики, не публикуется по-настоящему если что-то пойдёт не так.</p>",  # noqa: SLF001
                          "Тестовый текст для диагностики.")
        page.wait_for_timeout(1500)
        shot(page, "02-content-pasted")

        print("[diag] clicking Опубликовать (first)...")
        page.get_by_role("button", name="Опубликовать").first.click(force=True, timeout=10000)
        page.wait_for_timeout(2000)
        shot(page, "03-after-first-click")
        print("[diag] url after first click:", page.url)

        try:
            buttons = page.eval_on_selector_all(
                "button, [role='button']",
                "els => els.map(e => ({text: e.innerText.trim(), visible: e.offsetParent !== null})).filter(b => b.text)",
            )
            print("[diag] buttons after first click:", json.dumps(buttons, ensure_ascii=False)[:3000])
        except Exception as exc:  # noqa: BLE001
            print("[diag] button probe failed:", exc)

        try:
            checkboxes = page.eval_on_selector_all(
                "input[type=checkbox]", "els => els.map(e => ({checked: e.checked, visible: e.offsetParent !== null}))"
            )
            print("[diag] checkboxes:", json.dumps(checkboxes, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print("[diag] checkbox probe failed:", exc)

        print("[diag] page text snapshot:", page.inner_text("body")[:1500])

        # Try clicking any second/other visible "Опубликовать"-ish or "Продолжить" button.
        for label in ("Продолжить", "Опубликовать", "Подтвердить", "Да, опубликовать"):
            try:
                btn = page.get_by_role("button", name=label).last
                if btn.is_visible(timeout=1500):
                    print(f"[diag] clicking '{label}'...")
                    btn.click(force=True, timeout=5000)
                    page.wait_for_timeout(2000)
                    shot(page, f"04-after-{label}")
                    print("[diag] url now:", page.url)
            except Exception as exc:  # noqa: BLE001
                print(f"[diag] '{label}' not clickable:", type(exc).__name__)

        page.wait_for_timeout(2000)
        shot(page, "05-final")
        print("[diag] FINAL url:", page.url)
        print("[diag] FINAL text tail:", page.inner_text("body")[-1000:])


if __name__ == "__main__":
    run()
