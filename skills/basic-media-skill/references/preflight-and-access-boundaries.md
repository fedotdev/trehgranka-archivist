# Preflight & Access Boundaries

## Purpose

Never start download or discovery on a target without running this gate first.

## Preflight actions

1. **Normalize** `TARGET_URL` — resolve redirects, then use the final URL.
2. **Scope & allowed domains** — record scope (`site`, `section`, `gallery`, `forum`, `topics`, `urls`) and the exact `ALLOWED_DOMAINS` list.
3. **robots.txt** — fetch and record the outcome; honor it.
4. **Rules, licensing, copyright** — locate and record terms pages, license/copyright notices, contacts.
5. **API / RSS / sitemap** — identify read-only official feeds and endpoints that can replace crawling.
6. **Engine detection** — identify the blog/gallery/forum platform with a confidence level.
7. **Access boundaries** — record login, CAPTCHA, paywall, DRM, rate limits and closed areas.
8. **Risk map** — list the boundaries the run may hit and the chosen mitigation.

No mass requests during preflight.

## Never do

- Bypass CAPTCHA, login, paywall, DRM, robots.txt or rate limits — even for archiving.
- Crawl private messages, closed topics/profiles or personal data not visible to a normal public visitor.
- Publish or transmit archived data; keep source, author, license and copyright notices.
- Continue aggressively on HTTP 429/503 — lower concurrency, raise backoff, or stop and record state.

## Roles split

- **LLM**: plan, classify, analyze HTML/DOM, write selectors, interpret errors, prepare tests/patches, normalize and enrich thematically.
- **Deterministic code**: requests, URL queue, rate limiting, retries, binary storage, hashes, MIME checks, validation, SQLite, WARC, deduplication, coverage counting.