"""One-off diagnostic: open a Dzen draft in the REAL editor UI (same logged-in
profile) and capture the exact request body the UI itself sends when saving,
so we can compare it against our hand-built payload. Not part of the pipeline.

Usage: python -m autopilot.diag_capture <publication_id>
"""
from __future__ import annotations

import json
import sys

from .config import settings
from .db import Database
from .dzen_client import DzenClient, EDITOR


def run(publication_id: str) -> None:
    db = Database(settings.db_path)
    publisher_id = db.get_setting("dzen_publisher_id", "")
    print(f"[diag] publisher_id={publisher_id} publication_id={publication_id}")

    captured: list[dict] = []

    with DzenClient(settings) as client:
        page = client._page  # noqa: SLF001

        def on_request(req):
            if "update-publication-content" in req.url or "add-publication" in req.url:
                try:
                    captured.append({"url": req.url, "method": req.method, "post_data": req.post_data})
                except Exception as exc:  # noqa: BLE001
                    captured.append({"url": req.url, "error": str(exc)})

        page.on("request", on_request)

        url = f"{EDITOR}/id/{publisher_id}/{publication_id}/edit"
        print(f"[diag] navigating to {url}")
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(3000)

        print("[diag] page title:", page.title())
        print("[diag] current url:", page.url)

        # Dump interactive elements so we know real selectors for this build.
        try:
            editable = page.eval_on_selector_all(
                "[contenteditable='true']",
                "els => els.map(e => ({cls: e.className, text: e.innerText.slice(0,60)}))",
            )
            print("[diag] contenteditable elements:", json.dumps(editable, ensure_ascii=False)[:2000])
        except Exception as exc:  # noqa: BLE001
            print("[diag] contenteditable probe failed:", exc)

        try:
            buttons = page.eval_on_selector_all(
                "button",
                "els => els.map(e => e.innerText.trim()).filter(t => t.length && t.length < 30)",
            )
            print("[diag] buttons:", json.dumps(sorted(set(buttons)), ensure_ascii=False)[:2000])
        except Exception as exc:  # noqa: BLE001
            print("[diag] buttons probe failed:", exc)

        # Dismiss the first-run help popup overlay if present (blocks all clicks).
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            overlay = page.query_selector("[class*='help-popup__overlay']")
            if overlay:
                overlay.click(force=True, position={"x": 5, "y": 5})
                page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001
            print("[diag] overlay dismiss failed:", exc)

        # Try to nudge an autosave: click into the body contenteditable and type a space+backspace.
        try:
            editables = page.query_selector_all("[contenteditable='true']")
            print(f"[diag] found {len(editables)} contenteditable nodes")
            if len(editables) >= 2:
                editables[1].click(force=True, timeout=10000)
                page.keyboard.type(" ")
                page.wait_for_timeout(500)
                page.keyboard.press("Backspace")
                page.wait_for_timeout(4000)  # editors usually debounce autosave ~1-3s
        except Exception as exc:  # noqa: BLE001
            print("[diag] type probe failed:", exc)

        page.wait_for_timeout(3000)

    print(f"[diag] captured {len(captured)} relevant requests")
    for c in captured:
        print("----")
        print(c.get("url"))
        body = c.get("post_data")
        if body:
            try:
                parsed = json.loads(body)
                print(json.dumps(parsed, ensure_ascii=False, indent=2)[:6000])
            except json.JSONDecodeError:
                print(body[:2000])
        else:
            print("(no body)", c.get("error", ""))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m autopilot.diag_capture <publication_id>")
    run(sys.argv[1])
