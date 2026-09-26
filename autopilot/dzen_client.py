"""Dzen editor client: a persistent Chromium profile (Playwright) + the editor's own JSON API.

All API calls are executed *inside* the logged-in page with ``fetch`` so cookies,
fingerprint headers and TLS fingerprint are the real browser's. The UI is not
clicked, which keeps the flow stable across editor redesigns.

Endpoints (observed in the editor / Prozen extension):
  GET  /media-api/csrf-token
  POST /editor-api/v2/add-publication?publisherId=..&clientRid=..&clid=320
  POST /editor-api/v2/add-image?publicationId=..&publisherId=..   (multipart)
  POST /editor-api/v2/update-publication-content[-and-publish]?publisherId=..&clientRid=..
  GET  /editor-api/v2/publisher/{id}/stats2?...
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .draftjs import markdown_to_html

log = logging.getLogger(__name__)

BASE = "https://dzen.ru"
EDITOR = f"{BASE}/profile/editor"
PUBLISHER_RE = re.compile(r"/profile/editor/id/([0-9a-f]{24})")


class DzenError(RuntimeError):
    pass


class SessionExpired(DzenError):
    pass


class CaptchaRequired(DzenError):
    pass


class NoChannel(DzenError):
    pass


def client_rid() -> str:
    return secrets.token_hex(7)


class DzenClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._csrf: str = ""
        self.captured_headers: dict[str, str] = {}
        self.publisher_id: str = settings.dzen_publisher_id

    # ------------------------------------------------------------- lifecycle
    def open(self) -> "DzenClient":
        from playwright.sync_api import sync_playwright

        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = dict(
            user_data_dir=str(self.settings.profile_dir),
            headless=self.settings.headless,
            locale="ru-RU",
            timezone_id=self.settings.timezone,
            viewport={"width": 1366, "height": 860},
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage",
                  "--lang=ru-RU"],
            ignore_default_args=["--enable-automation"],
        )
        if self.settings.http_proxy:
            launch_kwargs["proxy"] = {"server": self.settings.http_proxy}
        self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.set_default_timeout(45000)
        self._page.on("request", self._capture)
        if not self._page.url.startswith(BASE):
            # A fresh persistent-context page starts on about:blank. api()'s
            # in-page fetch() calls need to run FROM dzen.ru or the browser
            # blocks them as cross-origin (TypeError: Failed to fetch) even
            # though the profile's session cookies are already there.
            self.goto(f"{BASE}/profile/editor", wait_ms=2000)
        return self

    def close(self) -> None:
        for closer in (lambda: self._ctx and self._ctx.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = self._pw = self._page = None

    def __enter__(self) -> "DzenClient":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _capture(self, request: Any) -> None:
        url = request.url
        if "editor-api" in url or "media-api" in url:
            for name, value in request.headers.items():
                lname = name.lower()
                if lname in ("x-csrf-token", "x-fp-token", "x-yandex-uid"):
                    self.captured_headers[lname] = value
                    if lname == "x-csrf-token":
                        self._csrf = value

    # --------------------------------------------------------------- session
    def goto(self, url: str, wait_ms: int = 2500) -> str:
        self._page.goto(url, wait_until="domcontentloaded")
        self._page.wait_for_timeout(wait_ms)
        return self._page.url

    def _interpret(self, url: str, body: str) -> dict[str, Any]:
        info: dict[str, Any] = {"logged_in": False, "publisher_id": "", "need_channel": False, "captcha": False,
                                "url": url}
        if "showcaptcha" in url or "Подтвердите, что запросы отправляли вы" in body:
            info["captcha"] = True
            return info
        if "passport.yandex" in url or "passport.ya" in url or "/auth" in url.split("?")[0]:
            return info
        m = PUBLISHER_RE.search(url)
        if m:
            info["logged_in"] = True
            info["publisher_id"] = m.group(1)
            self.publisher_id = self.publisher_id or m.group(1)
            return info
        # Logged in but no channel yet (creation wizard), or an unknown page.
        # Specific channel-creation-wizard copy only — "editor" or "канал"
        # appearing anywhere (true on nearly every dzen.ru page) is not
        # evidence of anything; a slow SPA redirect must not read as this.
        if "dzen.ru" in url and ("Войти" not in body[:800]):
            info["logged_in"] = True
            info["need_channel"] = any(
                phrase in body for phrase in ("Название канала", "Создать канал", "Заведите канал", "Создайте канал")
            )
        return info

    def check_session(self, publisher_id: str = "") -> dict[str, Any]:
        """Force-navigate to the editor and interpret the result. Only safe to call
        when nothing else may be driving the SAME page (e.g. headless re-checks) —
        for polling *during* a human-driven login, use peek_session() instead, which
        never navigates and so can't race the person's own clicks/redirects.

        Pass a known publisher_id when you have one: navigating straight to
        that channel's editor URL confirms the session directly, instead of
        relying on the bare /profile/editor page redirecting there itself
        (that redirect can be slow enough to time out our own wait loop,
        which reads as "no channel" even though the session is fine)."""
        target = f"{EDITOR}/id/{publisher_id}" if publisher_id else EDITOR
        try:
            url = self.goto(target, wait_ms=4000)
            for _ in range(8):  # SPA redirects can take a moment
                m = PUBLISHER_RE.search(url)
                if m or "passport" in url or "showcaptcha" in url:
                    break
                self._page.wait_for_timeout(1500)
                url = self._page.url
            body = ""
            try:
                body = self._page.inner_text("body")[:5000]
            except Exception:  # noqa: BLE001
                pass
            return self._interpret(url, body)
        except Exception as exc:  # noqa: BLE001
            return {"logged_in": False, "publisher_id": "", "need_channel": False, "captcha": False,
                    "url": "", "error": str(exc)[:300]}

    def peek_session(self) -> dict[str, Any]:
        """Read whatever page is currently loaded WITHOUT navigating — safe to poll
        every few seconds while a human is actively logging in on the same page."""
        try:
            url = self._page.url
            try:
                body = self._page.inner_text("body")[:5000]
            except Exception:  # noqa: BLE001
                body = ""
            return self._interpret(url, body)
        except Exception as exc:  # noqa: BLE001
            return {"logged_in": False, "publisher_id": "", "need_channel": False, "captcha": False,
                    "url": "", "error": str(exc)[:300]}

    def require_session(self) -> str:
        info = self.check_session()
        if info.get("captcha"):
            raise CaptchaRequired("Дзен показал капчу — нужен вход через браузер (/login)")
        if not info.get("logged_in"):
            raise SessionExpired("Сессия Дзена не активна — нужен вход через браузер (/login)")
        pid = self.publisher_id or info.get("publisher_id", "")
        if not pid:
            raise NoChannel("Аккаунт залогинен, но канал не найден — создайте канал в Дзене")
        return pid

    # ------------------------------------------------------------------- api
    def _js_fetch(self, method: str, url: str, *, body: str | None = None, headers: dict[str, str] | None = None,
                  form_b64: dict[str, Any] | None = None) -> dict[str, Any]:
        script = """
        async ({method, url, body, headers, form}) => {
          const init = {method, credentials: 'include', headers: Object.assign({}, headers || {})};
          if (form) {
            const bin = atob(form.b64); const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            const fd = new FormData();
            fd.append(form.field, new Blob([arr], {type: form.type}), form.name);
            for (const [k, v] of Object.entries(form.extra || {})) fd.append(k, v);
            init.body = fd;
          } else if (body !== null && body !== undefined) {
            init.body = body; init.headers['Content-Type'] = 'application/json';
          }
          const r = await fetch(url, init);
          const text = await r.text();
          return {status: r.status, text, ctype: r.headers.get('content-type') || ''};
        }
        """
        return self._page.evaluate(script, {"method": method, "url": url, "body": body, "headers": headers or {},
                                            "form": form_b64})

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        h = {"Accept": "application/json", "X-Csrf-Token": self._csrf or self.csrf(),
             "Referer": referer or f"{EDITOR}/id/{self.publisher_id}"}
        fp = self.captured_headers.get("x-fp-token")
        if fp:
            h["X-FP-Token"] = fp
        return h

    def api(self, method: str, path: str, *, body: dict[str, Any] | None = None, referer: str | None = None,
            form_b64: dict[str, Any] | None = None, retry_csrf: bool = True) -> Any:
        url = path if path.startswith("http") else BASE + path
        resp = self._js_fetch(method, url, body=json.dumps(body, ensure_ascii=False) if body is not None else None,
                              headers=self._headers(referer), form_b64=form_b64)
        status, text = int(resp["status"]), str(resp["text"])
        if "captcha" in text.lower()[:3000] or "showcaptcha" in text[:3000]:
            raise CaptchaRequired(f"captcha on {path}")
        if status in (401, 403) and retry_csrf:
            self._csrf = ""
            self.csrf()
            return self.api(method, path, body=body, referer=referer, form_b64=form_b64, retry_csrf=False)
        if status in (401, 403):
            raise SessionExpired(f"{status} on {path}: {text[:200]}")
        if status >= 400:
            raise DzenError(f"{status} on {path}: {text[:400]}")
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if "<html" in text[:500].lower():
                raise SessionExpired(f"HTML instead of JSON on {path} (login page?)")
            raise DzenError(f"non-JSON on {path}: {text[:200]}")

    def csrf(self) -> str:
        resp = self._js_fetch("GET", f"{BASE}/media-api/csrf-token", headers={"Accept": "application/json"})
        if int(resp["status"]) >= 400:
            raise SessionExpired(f"csrf-token {resp['status']}")
        try:
            data = json.loads(resp["text"])
        except json.JSONDecodeError:
            raise SessionExpired("csrf-token returned non-JSON")
        token = str(data.get("result") or data.get("token") or data.get("csrfToken") or "")
        if not token:
            raise DzenError(f"csrf-token payload: {resp['text'][:200]}")
        self._csrf = token
        return token

    # ------------------------------------------------------------ publishing
    def create_draft(self, publisher_id: str, publication_type: str = "article") -> str:
        data = self.api("POST", f"/editor-api/v2/add-publication?publisherId={publisher_id}&clientRid={client_rid()}&clid=320",
                        body={"title": "", "publisherId": publisher_id, "publicationType": publication_type, "fp": ""})
        pub_id = str(data.get("id") or data.get("publicationId") or data.get("_id") or
                     (data.get("publication") or {}).get("id") or "")
        if not pub_id:
            raise DzenError(f"add-publication without id: {json.dumps(data)[:300]}")
        return pub_id

    def upload_image(self, publisher_id: str, publication_id: str, path: str | Path) -> str:
        data = Path(path).read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        referer = f"{EDITOR}/id/{publisher_id}/{publication_id}/edit"
        last_error = ""
        for field in ("image", "file", "upload"):
            try:
                resp = self.api("POST", f"/editor-api/v2/add-image?publicationId={publication_id}&publisherId={publisher_id}&clientRid={client_rid()}",
                                referer=referer, form_b64={"b64": b64, "field": field, "name": Path(path).name,
                                                           "type": "image/jpeg", "extra": {"publicationId": publication_id}})
                image_id = str(resp.get("id") or resp.get("imageId") or resp.get("_id") or
                               (resp.get("image") or {}).get("id") or (resp.get("result") or {}).get("id") or "")
                if image_id:
                    return image_id
                last_error = f"no id in {json.dumps(resp)[:200]}"
            except DzenError as exc:
                if isinstance(exc, (SessionExpired, CaptchaRequired)):
                    raise
                last_error = str(exc)
                if "400" not in last_error and "415" not in last_error and "422" not in last_error:
                    break
        raise DzenError(f"add-image failed: {last_error}")

    def publish(self, publisher_id: str, publication_id: str, *, title: str, snippet: str,
                content_state: dict[str, Any], cover_image_id: str = "", tags: list[str] | None = None,
                mode: str = "publish") -> str:
        endpoint = "update-publication-content-and-publish" if mode == "publish" else "update-publication-content"
        # Field names/values below were diffed against a real save request from
        # the actual Dzen editor UI (captured via request interception on our
        # own logged-in session) and corrected to match exactly.
        preview: dict[str, Any] = {"title": title[:140], "snippet": snippet[:300], "galleryPreviewImages": []}
        if cover_image_id:
            preview["image"] = {"id": cover_image_id}
        body = {
            "id": publication_id,
            "preview": preview,
            "snippetFrozen": False,
            "hasNativeAds": False,
            "commentsFlagState": "on",
            "delayedPublicationFlagState": "off",
            "visibleComments": "subscribe-visible",
            "visibilityType": "all",
            "premiumTariffs": [],
            "articleContent": {"contentState": json.dumps(content_state, ensure_ascii=False)},
            "tagsInput": {"tags": [str(t)[:40] for t in (tags or [])][:8], "detectedTagsShown": False},
            # NOTE: the real client also sends a non-empty "fp" (an anti-bot
            # fingerprint its own JS computes) here; the server 400s without
            # one. That token isn't something to extract-and-replay - see
            # NOTES_ON_PUBLISHING.md. This call will 400 until publish() is
            # driven from the real editor UI instead of a raw API call.
            "fp": "",
        }
        referer = f"{EDITOR}/id/{publisher_id}/{publication_id}/edit"
        self.api("POST", f"/editor-api/v2/{endpoint}?publisherId={publisher_id}&clientRid={client_rid()}",
                 body=body, referer=referer)
        return f"{BASE}/a/{publication_id}"

    # ----------------------------------------------------------------- stats
    def publication_stats(self, publisher_id: str, publication_ids: list[str]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for i in range(0, len(publication_ids), 50):
            chunk = publication_ids[i:i + 50]
            qs = "&".join(f"publicationIds={pid}" for pid in chunk)
            fields = "&".join(f"fields={f}" for f in ("views", "typeSpecificViews", "likes", "comments", "shares",
                                                       "deepViews", "impressions"))
            data = self.api("GET", f"/editor-api/v2/publisher/{publisher_id}/stats2?publisherId={publisher_id}&{qs}&{fields}&pageSize=50&page=0")
            for item in self._iter_items(data):
                pid = str(item.get("publicationId") or (item.get("publication") or {}).get("id") or item.get("id") or "")
                stats = item.get("stats") if isinstance(item.get("stats"), dict) else item
                if pid:
                    out[pid] = {
                        "views": int(stats.get("views") or stats.get("typeSpecificViews") or stats.get("deepViews") or 0),
                        "likes": int(stats.get("likes") or 0),
                        "comments": int(stats.get("comments") or 0),
                        "shares": int(stats.get("shares") or 0),
                    }
        return out

    def channel_stats(self, publisher_id: str) -> dict[str, Any]:
        fields = "&".join(f"fields={f}" for f in ("views", "likes", "comments", "shares", "subscribers",
                                                   "subscribersDiff", "impressions"))
        data = self.api("GET", f"/editor-api/v2/publisher/{publisher_id}/stats2?publisherId={publisher_id}&allPublications=true&{fields}&groupBy=flight&sortBy=addTime&sortOrderDesc=true&total=true&pageSize=1&page=0")
        total = (data.get("total") or {}).get("stats") if isinstance(data.get("total"), dict) else {}
        return {"raw": data, "total": total or {}, "subscribers": self._dig(data, "subscribers")}

    @staticmethod
    def _iter_items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("publications", "items", "data", "results", "stats"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        return []

    @staticmethod
    def _dig(data: Any, key: str) -> Any:
        if isinstance(data, dict):
            if key in data and not isinstance(data[key], (dict, list)):
                return data[key]
            for v in data.values():
                found = DzenClient._dig(v, key)
                if found is not None:
                    return found
        elif isinstance(data, list):
            for v in data:
                found = DzenClient._dig(v, key)
                if found is not None:
                    return found
        return None

    def screenshot(self, path: str | Path) -> None:
        try:
            self._page.screenshot(path=str(path), full_page=False)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------- UI-driven publish
    # update-publication-content(-and-publish) needs a real anti-bot "fp"
    # token that only Dzen's own client-side JS computes (confirmed by
    # diffing our raw request against a real one, see NOTES_ON_PUBLISHING.md).
    # Rather than extract-and-replay that token, drive the actual editor UI —
    # paste rich content, upload images through its own upload panel, click
    # its own Опубликовать button — so the browser sends its own genuine
    # request. Selectors below were found by inspecting screenshots of the
    # real editor (see diag_ui_publish.py) and may need updating if Dzen
    # redesigns the editor.
    def _dismiss_help_overlay(self) -> None:
        try:
            self._page.keyboard.press("Escape")
            overlay = self._page.query_selector("[class*='help-popup__overlay']")
            if overlay and overlay.is_visible():
                overlay.click(force=True, position={"x": 5, "y": 5}, timeout=5000)
                self._page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass

    def _paste_html(self, element: Any, html: str, plain: str) -> None:
        element.click(force=True, timeout=10000)
        self._page.keyboard.press("Control+a")
        self._page.evaluate(
            """async ({html, text}) => {
                const item = new ClipboardItem({
                  'text/html': new Blob([html], {type: 'text/html'}),
                  'text/plain': new Blob([text], {type: 'text/plain'}),
                });
                await navigator.clipboard.write([item]);
            }""",
            {"html": html, "text": plain},
        )
        self._page.keyboard.press("Control+v")
        self._page.wait_for_timeout(800)

    def _find_add_media_icon(self) -> Any:
        """The add-media affordance is a distinctive 28x28 icon that tracks the
        current trailing empty paragraph; other icons on the page are header
        controls (different sizes) or off-screen menu items (negative y)."""
        for el in self._page.query_selector_all("svg, [class*='icon'], [class*='Icon']"):
            box = el.bounding_box()
            if box and 26 <= box["width"] <= 32 and 26 <= box["height"] <= 32 and 100 < box["y"] < 4000:
                return el
        return None

    def _insert_image_via_ui(self, path: Path) -> bool:
        icon = self._find_add_media_icon()
        if not icon:
            return False
        icon.click(force=True, timeout=5000)
        self._page.wait_for_timeout(600)
        upload_btn = self._page.get_by_text("Загрузите файл", exact=False)
        try:
            with self._page.expect_file_chooser(timeout=8000) as fc_info:
                upload_btn.click(force=True, timeout=5000)
            fc_info.value.set_files(str(path))
        except Exception:
            log.exception("image upload panel did not behave as expected for %s", path)
            return False
        self._page.wait_for_timeout(4000)
        try:
            close_x = self._page.query_selector("[class*='close']")
            if close_x and close_x.is_visible():
                close_x.click(force=True, timeout=2000)
                self._page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass
        return True

    def publish_via_ui(self, publisher_id: str, publication_id: str, *, title: str, html_body: str,
                       plain_body: str, image_paths: list[Path], mode: str = "publish") -> str:
        """Returns the final article URL (for mode="publish") or the edit URL
        (for mode="draft", left saved-but-unpublished by the editor's own
        autosave). Raises DzenError if the publish click didn't land."""
        self._ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        edit_url = f"{EDITOR}/id/{publisher_id}/{publication_id}/edit"
        self._page.goto(edit_url, wait_until="networkidle", timeout=45000)
        self._page.wait_for_timeout(2000)
        self._dismiss_help_overlay()

        editables = self._page.query_selector_all("[contenteditable='true']")
        if len(editables) < 2:
            raise DzenError("editor page did not render the expected title/body fields")
        self._paste_html(editables[0], f"<p>{title}</p>", title)
        self._paste_html(editables[1], html_body, plain_body)
        self._page.wait_for_timeout(1000)

        for path in image_paths:
            if Path(path).is_file() and not self._insert_image_via_ui(Path(path)):
                log.warning("could not insert image via UI: %s", path)

        self._page.wait_for_timeout(1500)  # let the trailing autosave land

        if mode != "publish":
            return edit_url

        self._page.get_by_role("button", name="Опубликовать").first.click(force=True, timeout=10000)
        self._page.wait_for_timeout(1500)
        try:
            again = self._page.get_by_role("button", name="Опубликовать").last
            if again.is_visible(timeout=3000):
                again.click(force=True, timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        self._page.wait_for_timeout(2500)
        final_url = self._page.url
        if "/a/" not in final_url:
            raise DzenError(f"publish click did not navigate to a published article (url={final_url})")
        return final_url


def publish_full_article(client: "DzenClient", publisher_id: str, *, title: str, markdown: str,
                         description: str, tags: list[str], cover_path: Path | None,
                         inline_image_paths: list[Path], mode: str = "publish") -> dict[str, Any]:
    """Creates a draft via the API (no fp needed there), then drives the real
    editor UI for content + images + the publish click itself (fp IS needed
    there — see publish_via_ui). Returns {"publication_id", "url", "mode"}.
    Raises SessionExpired/CaptchaRequired/NoChannel/DzenError — callers decide
    how to react (retry, pause, alert).
    """
    publication_id = client.create_draft(publisher_id)
    log.info("Dzen draft created id=%s title=%r", publication_id, title)

    images = [p for p in ([cover_path] if cover_path else []) + list(inline_image_paths) if p and Path(p).is_file()]
    html_body = markdown_to_html(markdown)
    plain_body = re.sub(r"<[^>]+>", " ", html_body)

    url = client.publish_via_ui(
        publisher_id, publication_id, title=title, html_body=html_body, plain_body=plain_body,
        image_paths=images, mode=mode,
    )
    log.info("Dzen publish ok id=%s mode=%s url=%s", publication_id, mode, url)
    return {"publication_id": publication_id, "url": url, "mode": mode}
