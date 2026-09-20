# Deterministic Crawler Contract

The core is Scrapy or an equivalent deterministic HTTP crawler, plus SQLite state and JSONL export.

## Non-negotiable defaults

- `ROBOTSTXT_OBEY = true` unless the owner gave documented permission.
- `CONCURRENT_REQUESTS_PER_DOMAIN = 1` by default.
- `DOWNLOAD_DELAY` 1–5 s, `RANDOMIZE_DOWNLOAD_DELAY = true`.
- `AUTOTHROTTLE_ENABLED = true`.
- `RETRY_TIMES` 3–4, limited to 408/429/500/502/503/504.
- `DOWNLOAD_TIMEOUT` 30–60 s.
- HTTP cache enabled during development.
- JOBDIR/resume always on.
- User-Agent carries project name and contact.
- `MAX_PAGES` and `MAX_DEPTH` always active until a confirmed full run.

## What stays out of the LLM

Downloads, hashing, MIME checks, file validation, SQLite writes, WARC writing, deduplication and coverage counting are deterministic-code work. The LLM plans, classifies, analyzes HTML/DOM, writes selectors/patches/tests and interprets errors — it never replaces the crawler.

## Optional tools stay optional

Missing tool → fall back and record the gap (see discovery reference). WARC/Browsertrix is a control snapshot; it does not replace SQLite/JSONL plus separately stored originals.