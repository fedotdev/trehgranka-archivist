# AGENTS.md — basic-media-skill

Companion instruction file for the basic-media archive skill. Full details live in `SKILL.md`; this file is the cross-tool summary (read by Codex-CLI, Cursor, Augment, Continue.dev, Zed and others).

## Purpose

Preserve public websites, photo galleries, scans and documents as a verifiable, reproducible archive: structure, provenance, hashes, validation state, coverage, and a mandatory Wayback recovery path.

## Activation triggers

- "archive this gallery / сайта / сканы / фотогалерею"
- "dry-run the site before the full run"
- "make a test report"
- "recover missing media via Wayback"
- any request to crawl media/documents where preflight and coverage matter

Also activates via `/basic-media-skill` invocation.

## Workflow (always in order)

1. **Preflight** — normalize URL, robots.txt, rules/licensing, API/RSS/sitemap, engine, auth/CAPTCHA/paywall/DRM risk map. No mass requests.
2. **Discovery / dry-run** — inventory URLs, sample templates, analyze raw HTML vs rendered DOM, small test download set.
3. **Test Report** — sizes, MIME, dimensions, originals vs thumbnails, pagination, verdict `ready`/`needs changes`.
4. **Full run** — only with `USER_CONFIRMED_FULL_RUN=true` and explicit user confirmation. Scrapy/core crawler, rate limited, cached, resumable (JOBDIR).
5. **Final Report** — discovered/processed/verified/failed/skipped, per-entity coverage, unresolved URLs, resume instructions.

## Hard rules

- Never bypass CAPTCHA, login, paywall, DRM, robots.txt or rate limits.
- Never crawl private messages, closed areas, or non-public personal data.
- The LLM plans/analyzes; deterministic code downloads, hashes, validates, stores. Never let the LLM masquerade as the downloader/final authority/deleter.
- Do not overwrite or delete a live response, even when it is invalid.
- Do not publish or transmit archived data.
- Do not declare the archive complete while pagination, failures, retries, or recovery items remain.

## Mandatory Wayback recovery

Every missing, empty, corrupt, substituted or suspected-deleted media file gets a recovery pass: keep the live response and its failure record, query Wayback CDX (exact URL, then variants), rank captures by proximity to publication date (never newest-first), replay via `id_`, validate each candidate, keep all checked captures, store provenance. A file is recovered only after passing technical validation; the archive is complete only after the recovery queue is processed.

## Pipeline

Run `scripts/run_pipeline.py` with one command; it stages preflight → discovery → dry-run → media validation → recovery queue → test report → full-run gate. Reports land in `reports/` under the output directory; `reports/test_report.json` carries stage, gate, decision, media valid/invalid, recovery queue size and coverage. Every run also writes a human-readable site-structure summary (`RESUME_структура.txt` next to the test report: counts, formats, sections, archives, recovery state).

## Gotchas

- Windows: use `py -3 scripts\run_pipeline.py ...` (multiple interpreters may be on PATH).
- A URL without a file extension can still be media — check Content-Type and magic bytes.
- `USER_CONFIRMED_FULL_RUN` must be boolean; `"true"` as a string fails validation.
- Never mix raw HTML, originals, thumbnails and failure records in one directory.
- No discovery manifest yet → plan offline first; do not guess the URL map.