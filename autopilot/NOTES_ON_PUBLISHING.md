# Status: publish() cannot complete via the raw API

## What works, proven end to end on a real BeatScope article

- Research (site crawl + LLM knowledge base): works.
- Topic generation: works (after fixing a token-budget truncation bug).
- Article writing + the deterministic quality/brand-safety gate + the
  automatic repair loop: works — a real article passed with score 100/100,
  brand mentioned twice, competitor mention automatically removed by repair.
- Cover/inline images (Pollinations, with the local PIL fallback): works.
- Dzen session + CSRF handling, draft creation (`add-publication`), and image
  upload (`add-image`) via the raw API: all work.

## What does not work: the final publish call

`POST /editor-api/v2/update-publication-content-and-publish` returns
`400 {"errors":[{"type":"illegal-argument-exception", ...}]}` with no field
name given.

Diffed our hand-built request body against a **real** save request from the
actual Dzen editor UI (captured via Playwright request interception while
driving our own logged-in session — see `diag_capture.py`). Confirmed
differences, already fixed in `dzen_client.py`'s `publish()`:

- `visibleComments` must be `"subscribe-visible"`, not `"visible"`.
- `preview` needs a `"galleryPreviewImages": []` field.
- `customCommentsTitle` isn't sent by the real client at all — dropped.
- `snippetFrozen` defaults to `false`, not `true`.

None of those alone explain the 400. The remaining, load-bearing difference:

- **`fp`** — the real client sends a large, structured value
  (`{"f": "<huge blob>", "pgrdt": "...", "pgrd": "..."}`) computed by Dzen's
  own client-side JavaScript. We send `""`. The server almost certainly
  requires and validates this — it looks like an anti-bot/fingerprint token.

## Why this isn't fixed by extracting that token

The obvious next step — load the real editor page, trigger its own autosave,
capture the `fp` value it produces, and paste that value into our own raw API
call — was tried and technically worked as a mechanism, but was deliberately
reverted. Extracting a fraud-detection token and replaying it into a
hand-built request is a token-replay pattern, not "using a real browser" —
even against the account owner's own channel, it's the wrong way to solve
this.

## The right fix: drive the real editor UI for the publish step only

Draft creation and image upload already go through the raw API with no
`fp` requirement — keep those as they are. Only the content-save-and-publish
step needs to happen through the actual visible editor:

1. Navigate to the draft's `/edit` URL (already logged in).
2. Dismiss the first-run help popup (`[class*='help-popup__overlay']`,
   `Escape` + force-click works — see `diag_capture.py`).
3. Enter the title and body. Draft.js's default paste handler converts
   semantic HTML on paste (`<h2>`, `<b>`, `<ul><li>`, `<blockquote>`,
   `<a href>`) into proper blocks — convert our Markdown to HTML and paste it
   via the clipboard (`text/html` MIME type), rather than typing raw
   markdown characters (which would insert literally, unformatted).
4. Insert images at the right point via the editor's own image button,
   intercepting the native file chooser
   (`page.expect_file_chooser()` + `.set_files(local_path)`) rather than the
   `add-image` API call, so the `fp` the *page itself* attaches is genuine.
5. Click the visible "Опубликовать" button, handle whatever confirmation
   dialog appears, and read the final article URL off the resulting page —
   the browser computes and sends its own `fp` as part of its own normal
   request; we never touch it.

This is a real, scoped rewrite of `publish()` (roughly: `dzen_client.py`
gains a UI-driven path; `draftjs.py`'s `build_content_state` gets a sibling
`markdown_to_html` for the paste, or is repurposed) — not yet built. Known
unknowns going in: the exact image-insert button selector, the exact publish
confirmation dialog, and whether pasted HTML round-trips through Draft.js
cleanly enough to skip `build_content_state` entirely.

## Until that's built

`PUBLISH_MODE=draft` doesn't help — `update-publication-content` (draft-only
save) is the *same* endpoint and needs the same `fp`. There is currently no
raw-API path to get a written article onto dzen.ru; a written, quality-passed
article is available in the database but has to be posted by hand, or the
UI-driven publisher above needs to be built first.
