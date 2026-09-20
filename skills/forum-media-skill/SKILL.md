---
name: forum-media-skill
description: >-
  Archive a public web forum (phpBB, vBulletin, XenForo, Discourse, Invision
  Community (IPS) and similar),
  preserving topics, posts, quotes, reactions, attachments, author names and
  pagination with the same verifiable pipeline family as basic-media-skill:
  preflight, dry-run, deterministic crawl, media validation, mandatory Wayback
  recovery queue and per-entity coverage. Applies a forum-specific extractor
  comparison where relevant and never bypasses CAPTCHA/login/paywall/DRM/robots.
  Triggers: forum archive, forum scrape, web forum, phpBB, XenForo, Discourse,
  форум, архивация топиков, темы, ветки форума, dry-run форума.
activation: /forum-media-skill
license: MIT
metadata:
  author: trehgranka-archivist
  version: 1.0.0
  created: 2026-09-18
  last_reviewed: 2026-09-18
  review_interval_days: 90
  dependencies:
    - url: https://web.archive.org/cdx/search/cdx
      name: Wayback Machine CDX API
      type: api
    - url: https://web.archive.org
      name: Internet Archive raw replay (id_ modifier)
      type: host
    - url: https://community.example.org
      name: Example/test host used by golden fixtures only
      type: host
    - url: https://example.org
      name: Example/documentation host used in code and documentation examples
      type: host
  schema_expectations:
    - url: https://web.archive.org/cdx/search/cdx
      method: GET
      expected_keys:
        - timestamp
        - original
        - statuscode
        - mimetype
        - digest
        - length
      notes: One capture per line for the plain-text output; replay uses the id_ modifier.
provenance:
  maintainer: trehgranka-archivist
  skill_family: trehgranka-archivist
  source_references:
    - title: Universal Web/Forum/Media Archivist prompt (normative)
      url: ../instructions/universal_web_forum_media_archivist_prompt.md
  sibling_skills:
    - basic-media-skill
---
# /forum-media-skill — Forum, Topic & Attachment Archivist

Public web forum archivist: preserve topics, posts, quotes, reactions, attachments, author names, pagination and provenance. Same canonical workflow as `basic-media-skill`, extended with forum entities, engine detection and extractor comparison.

## Trigger

```
/forum-media-skill archive https://forum.example.org/viewforum.php?f=2
forum archival dry-run for a phpBB board
Архивируй ветки форума и вложения
Сделай preflight и test report для форума
Реконструируй удалённые вложения через Wayback
```

Maybe also activate without the prefix:

```
Archive this forum with topics, quotes and attachments
Dry-run the board first, show pagination gaps
Recover missing attachments from the Wayback Machine
```

## Strict boundaries

Identical to `basic-media-skill`:

- never bypass CAPTCHA, login, paywall, DRM, robots.txt or rate limits;
- never crawl private messages, closed forums/topics/profiles, or non-public personal data;
- LLM plans and classifies; deterministic code downloads, hashes, validates, stores;
- never overwrite or delete a live response;
- never declare the archive complete while pagination, failures, retries or recovery items remain;
- LLM output (tags, inferred topic classes, extractor diff) is hypothesis with confidence, never source fact.

## Forum entities

The pipeline tallies these entity kinds independently of pages and media:

- `category`, `forum` — board structure;
- `topic` — thread pages;
- `post` — individual posts (including multi-page topic tail pages);
- `quote`, `reaction` — content-level markers (when the engine distinguishes them);
- `author` — public author profiles (action as public visitors see them, never private data);
- attachments and `document`/`media` — files, validated separately.

Coverage is reported per entity type, and pagination is checked explicitly: multi-page topics are expansion candidates, never auto-followed in dry-run.

## Pipeline command

```bash
python scripts/run_pipeline.py --config path/to/forum.yaml --output forum_report.json --offline
```

## Report structure

Same as basic-media plus a `forum` block:

```
forum:
  engine: {name, confidence, evidence}
  entity_counts: {category: N, forum: N, topic: N, post: N, ...}
  extractor_plan: scrapy-adapter (or forumscraper / forum-dl, advisory)
  paginated_posts_seen: N
  attachment_verified: N
  attachment_total: N
  attachment_coverage: N.NN | "n/a"
```

## Extractor comparison

When a forum-specific extractor is available, compare it against Scrapy controls on a small sample to verify post body, author, quote, reaction and attachment counts before promoting it to the full run. When it is missing, record the gap and use a Scrapy control run plus a site-specific adapter.

### Invision Community (IPS)

For IPS boards (URL pattern `/forums/topic/`; DOM markers `article#elComment_*` with classes `cPost`/`ipsComment`, `ul.ipsPagination`, `data-pages`, `data-controller="core.front.core.comment"`) use the bundled deterministic extractor `scripts/extractors/invision.py` (stdlib-only, no dependencies; self-check via `--probe`). It extracts posts, authors, profile URLs, dates, quotes, reactions, attachments and in-post images; pagination comes from `<link rel="next|last">` and `data-pages`.

IPS-specific rules the extractor encodes:

- images are lazy-loaded: placeholders show `spacer.png` and the real URL lives in `data-src` (with a `srcset` fallback) — never download the placeholder, never treat the placeholder as the original;
- attachments are `a.ipsAttachLink` anchors (often wrapping the same lazy image);
- external photo hotlinks (radikal.ru, imageshack, photofile and similar) appear as plain `<a href="...jpg|jpeg|png|gif">` anchors without an `<img>` child and on non-forum hosts — treat them as ordinary media: verify, and send missing ones through the mandatory Wayback recovery queue;
- robots/access boundaries: record author profile URLs (`/profile/`) but do not crawl them; do not follow `?do=` actions, login pages, or pagination query variants beyond recording `next`/`last`; respect `robots.txt` and rate limits like any other domain.

## Wayback / Archive Recovery

Same mandatory recovery as `basic-media-skill`: attachments and images are recovered via CDX rank-by-publication-date, raw replay with `id_`, full validation of every candidate, provenance recorded, queue processed before declaring completion.

## Fixed response format

Stage → Goal → Scope → Tooling used → Actions performed → Findings → Evidence → Decisions and rationale → Risks and limitations → Next action → User confirmation required: yes/no

## Gotchas

- phpBB URLs (`viewtopic.php?t=42&page=2`) are the most common post/pagination pattern; engine detection uses URL markers plus sampled HTML.
- IPS pages (`/forums/topic/…/page/N/`) lazy-load post images via `data-src` on a `spacer.png` placeholder and mark attachments as `ipsAttachLink`; engine detection also upgrades on DOM markers via `_detect_engine_dom` in `run_pipeline.py`.
- Large topics may exist; dry-run samples one page per template type and never mass-follows.
- Private messages stay out of scope even when archived by a third-party tool.
- Attachment URLs may need cookies/redirects — record every final URL and its redirect chain.
- The extractor is advisory: the deterministic core orchestrates the run.

## References

| File | When to read it |
|------|----------------|
| `references/preflight-and-access-boundaries.md` | Before any forum run: robots, licensing, paywall/CAPTCHA, private-area risks |
| `references/discovery-and-template-analysis.md` | When inventorying board structure and topic pages |
| `references/dry-run-and-test-report.md` | When choosing the small test set for topics/posts/attachments |
| `references/deterministic-crawler-contract.md` | For Scrapy / HTTP defaults, pagination, retries, cache, resume |
| `references/media-validation-and-wayback-recovery.md` | For attachment validation and CDX recovery rules |
| `references/completeness-and-coverage.md` | Before declaring a board complete |
| `references/troubleshooting.md` | When config, fixtures or pipeline outputs are unexpected |

Run `scripts/validate_report.py --check forum-payload` against a produced forum report.