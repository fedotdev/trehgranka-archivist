# AGENTS.md — forum-media-skill

Companion instruction file for the forum-media archive skill. Full details live in `SKILL.md`; this file is the cross-tool summary (read by Codex-CLI, Cursor, Augment, Continue.dev, Zed and others).

## Purpose

Preserve public web forums — topics, posts, quotes, reactions, attachments, author names, pagination — as a verifiable, reproducible archive: structure, provenance, hashes, validation state, coverage, and a mandatory Wayback recovery path. Same canonical workflow as `basic-media-skill`, extended with forum entities, engine detection and extractor comparison (phpBB/vBulletin/XenForo/Discourse plus a bundled deterministic Invision Community (IPS) extractor in `scripts/extractors/invision.py`).

## Activation triggers

- "archive this forum / форум / ветки форума / топики / вложения"
- forum scrape / phpBB / XenForo / Discourse boards
- "dry-run the board before the full run"
- "make a forum test report"
- "recover missing attachments via Wayback"
- any request to crawl forum topics/attachments where preflight and coverage matter

Also activates via `/forum-media-skill` invocation.

## Workflow (always in order)

1. **Preflight** — normalize URL, robots.txt, rules/licensing, API/RSS/sitemap, engine (phpBB/vBulletin/XenForo/Discourse), auth/CAPTCHA/paywall/DRM, private-area risk map. No mass requests.
2. **Discovery / dry-run** — inventory board structure (categories/forums/topics/posts), sample templates, pagination check, small test download set with attachments.
3. **Test Report** — per-entity coverage (topics, posts, quotes, reactions, attachments, authors), originals vs thumbnails, extractor comparison, verdict `ready`/`needs changes`.
4. **Full run** — only with `USER_CONFIRMED_FULL_RUN=true` and explicit user confirmation. Scrapy/core crawler, rate limited, cached, resumable (JOBDIR).
5. **Final Report** — discovered/processed/verified/failed/skipped per entity type, unresolved URLs, resume instructions.

## Hard rules

- Never bypass CAPTCHA, login, paywall, DRM, robots.txt or rate limits.
- Never crawl private messages, closed forums/topics/profiles, or non-public personal data.
- The LLM plans/analyzes; deterministic code downloads, hashes, validates, stores. Never let the LLM masquerade as the downloader/final authority/deleter.
- Do not overwrite or delete a live response, even when it is invalid.
- Do not publish or transmit archived data.
- Do not declare the archive complete while pagination, failures, retries, or recovery items remain.

## Mandatory Wayback recovery

Every missing, empty, corrupt, substituted or suspected-deleted media file gets a recovery pass: keep the live response and its failure record, query Wayback CDX (exact URL, then variants), rank captures by proximity to publication date (never newest-first), replay via `id_`, validate each candidate, keep all checked captures, store provenance. A file is recovered only after passing technical validation; the archive is complete only after the recovery queue is processed.

## Pipeline

Run `scripts/run_pipeline.py` with one command; it stages preflight → discovery → dry-run → media validation → recovery queue → test report → full-run gate and writes the canonical report to the exact `--output` path (archive artifacts live next to it in `reports/`, `data/`, `state.db`). The report carries stage, gate, decision, media valid/invalid, recovery queue size, coverage and the `forum` block (engine, entity counts, pagination marks, attachment coverage). Every run also writes a human-readable site-structure resume `RESUME_структура.txt` beside the test report (counts, formats, per-section distribution, archives, recovery state).

## Gotchas

- Windows: use `python scripts\run_pipeline.py ...` (multiple interpreters may be on PATH).
- A URL without a file extension can still be media — check Content-Type and magic bytes.
- `USER_CONFIRMED_FULL_RUN` must be boolean; `"true"` as a string fails validation.
- Never mix raw HTML, originals, thumbnails and failure records in one directory.
- No discovery manifest yet → plan offline first; do not guess the URL map.
- IPS boards: extract with `scripts/extractors/invision.py` (stdlib-only). In-post images are lazy-loaded via `data-src` on a `spacer.png` placeholder — take the `data-src` URL, never the placeholder. Record author `/profile/` links but do not crawl them.
- External photo hotlinks (radikal.ru, imageshack, photofile, …) inside posts are ordinary media: verify them, and every missing/empty/corrupt one goes through the mandatory Wayback recovery queue.