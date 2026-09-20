# AGENTS.md — Trehgranka Web/Media/Forum Archivist

## Repository status

Monorepo: canonical specification + the two skills that implement it.

- Canonical specification: `instructions/universal_web_forum_media_archivist_prompt.md` — the normative authority for all archiving work.
- Implemented skills live in `skills/`: `basic-media-skill` (websites, photo galleries, scans, documents) and `forum-media-skill` (public forums: topics, posts, quotes, reactions, attachments, authors).
- Keep changes to Markdown instruction/specification files and to skill files under `skills/`. Do not commit runtime state (SQLite, raw downloads, probe/recovery outputs, generated reports), credentials, keys, or archived site content — those belong in the run workspace (sibling `<run-workspace>\` directory), never in the repo.

## Source of truth for skills

Copies under `skills/` are the published representation of the skills. The working copies used by the daily pipeline live in the private skills worktree and are the ones edits are applied to in real archiving sessions; after a feature change the working copies are synced into `skills/` (see below). Both trees must stay byte-identical for files that ship in the repo.

## Skill layout

| Skill | Purpose | Layout |
|-------|---------|--------|
| `basic-media-skill` | Websites, photo galleries, scans, documents | SKILL.md + AGENTS.md + scripts + references + assets + evals |
| `forum-media-skill` | Public forums | same layout + forum entities/engine/extractor (`scripts/extractors/invision.py`) |

Each skill is self-contained: install scripts (`install.sh`/`install.ps1`), Claude Code plugin manifests (`.claude-plugin/`), a deterministic pipeline (`scripts/run_pipeline.py`), a Wayback recovery helper (`scripts/wayback.py`) and its own loss function (`evals/*.eval.md` with golden fixtures, run via `scripts/run_evals.py`).

Toolchain ownership: the maintenance/eval scripts that are byte-identical across both skills (`wayback.py`, `run_evals.py`, `evolve.py`, staleness/schema/dependency helpers) are maintained once in `basic-media-skill/scripts/`; `forum-media-skill` ships only its forum-specific scripts and its own `run_evals.py` and carries no duplicate copies of the shared layer.

- Skills are ruled by their own `SKILL.md`/`AGENTS.md`; the canonical spec remains normative for both.
- **No third-party scripts**: everything under `skills/*/scripts/` is original code written for this project, stdlib-only, no imports of external crawler/scraper libraries. Do not add scripts copied from other repositories.

## Commands

- Repo defines **no** build/test/lint/typecheck/CI commands and has no manifests or lockfiles. Do not invent or assert any.
- Validation happens by running the skills' own gates:
  - `python skills/basic-media-skill/scripts/run_evals.py --rollout --include-holdout`
  - `python skills/forum-media-skill/scripts/run_evals.py --rollout --include-holdout`
  - dry-run/test sample, Test Report, then explicit confirmation, per the canonical spec.

## Operating workflow (always in this order)

1. **Preflight** — normalize target URL, check `robots.txt`, site rules, licensing/copyright, API/RSS/sitemap; identify forum engine, auth/CAPTCHA/paywall/DRM, closed areas; produce risk map. No mass requests.
2. **Discovery / dry-run** — inventory URLs, classify them, sample each template type; analyze raw HTML vs rendered DOM differences; small test download set.
3. **Test Report** — sizes, MIME, dimensions, originals vs thumbnails, pagination, extractor comparison; verdict `ready` / `needs changes`.
4. **Full run** — only after explicit user confirmation. Mass crawl, download, dedup, validate, save, coverage report.
5. **Final Report** — discovered/processed/verified/failed/skipped counts, per-entity coverage, unresolved URLs, resume instructions.

Never start a full run without `USER_CONFIRMED_FULL_RUN = true`.

## Safety and access boundaries

- Do **not** bypass: CAPTCHA, login, paywall, DRM, `robots.txt`, rate limits; do not crawl private messages, closed topics/profiles, or personal data not visible to a normal public visitor.
- On HTTP 429/503 or degraded availability: lower concurrency, increase delay/backoff, or stop the run — record state and report, do not continue aggressively.
- Do not publish or transmit archived data. Keep source, author, license, copyright notice where available.
- Do not run the full archive through the LLM as downloader/final authority/deleter; LLM plans, classifies, analyzes HTML/DOM, writes selectors/patches/tests — deterministic code downloads, hashes, validates, stores.
- Do not commit archived content, personal data, or run workspace state to this repository.

## Deterministic core and optional tools

- **Minimum core:** Scrapy (or equivalent deterministic HTTP crawler) + SQLite state + JSONL export. Rate limit (1 concurrent per domain), `DOWNLOAD_DELAY` 1–5 s, AutoThrottle, retry only 408/429/500/502/503/504, HTTP cache in dev, JOBDIR/resume always, `ROBOTSTXT_OBEY = true` unless documented permission.
- Browser rendering (Crawl4AI/scrapy-playwright) applies **only** to confirmed JS-dependent templates — never the whole domain by default. First sample one page per problem template.
- WARC/Browsertrix is a control snapshot; it does not replace SQLite/JSONL + separately stored originals.
- Optional tools never become prerequisites. When one is missing, fall back and record the gap (Firecrawl, browser tool, dedicated forum extractor, Browsertrix/WARC, OCR/embeddings/Qdrant).

## Mandatory Wayback / Archive Recovery

Archive Recovery is **mandatory** for every missing, empty, corrupt, substituted, or suspected-deleted media file. Treat a live file as invalid on: persistent 404/410/403/5xx, empty body, mismatched Content-Type or magic bytes, undecodable image, failed Pillow `verify()`, HTML/placeholder stub, anomalous size, or mass-identical stub across URLs.

Recovery rules:
1. Never overwrite or delete the received (live) response — record status, headers, bytes, SHA-256, failure reason.
2. Query Wayback CDX API for the exact original URL; try URL variants (HTTP/HTTPS, www/non-www, raw/final, encoded/decoded, without optional query params, historical domains).
3. Rank snapshots by proximity to the page/post publication date; **do not** auto-pick the newest.
4. Download via raw replay with `id_`; run the candidate through full media validation; discard HTML, errors, empty, corrupt, and placeholder responses.
5. Stop only after a confirmed original is found, but keep the list of all checked captures. If the direct URL fails, recover the archived source page and re-run CDX for historical URLs found in it.
6. Store provenance per capture: timestamp, original, replay URL, statuscode, mimetype, digest, length, local SHA-256, dimensions, validation result, recovery method/confidence.
7. A file is not recovered until it passes technical validation; the archive is not complete until the recovery queue is processed. Never construct/guess a historical URL without recording the rule and hypothesis source.

## Evidence and completeness

- Keep raw HTML and derived representations separate; do not replace post HTML with Markdown/text; never alter source files.
- Do not mark `candidate_original` as a verified original without objective signs (wrapper link, resolution, size, DOM role, HTTP response, template rule).
- Deduplication: exact by SHA-256, probable visual by pHash/dHash; keep all URL sources; never auto-delete probable duplicates; different resolutions are variants, not duplicates; entities dedup by stable IDs/canonical permalinks, not text equality.
- LLM output is hypotheses (tags, inferred quotes, thematic classes) — store with confidence, never as source facts; do not merge user identities.
- Report coverage per entity type: `coverage = verified_in_scope / uniquely_discovered_in_scope`. Never call the archive complete while pagination, failed URLs, retries, and discovered→processed→verified reconciliation are unhandled.

## Format of agent responses

Every stage reply must follow the spec's fixed structure: Stage → Goal → Scope → Tooling used → Actions performed → Findings → Evidence (URL, selector/rule, HTTP status, counts, log/DB paths) → Decisions and rationale → Risks and limitations → Next action → `User confirmation required: yes/no`. Do not present unconfirmed numbers as facts; do not hide errors; do not declare completion without validation.