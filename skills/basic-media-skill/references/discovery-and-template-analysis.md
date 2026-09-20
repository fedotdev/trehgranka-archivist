# Discovery & Template Analysis

## URL inventory

For every found URL record: raw URL, canonical URL, final URL, referrer, depth, HTTP status, content type, robots decision, classification, template type, JS-dependency flag, discovery time and last check time.

Core URL classes:

- `section_page`, `album_page`, `photo_page`, `generic_html_page`
- `direct_image`, `direct_document`, `thumbnail`, `navigation_asset`
- `api_endpoint`, `rss_feed`, `sitemap`
- `external_link`, `unsupported`, `failed`

## Discovery sources

- Firecrawl map (optional): broad site map and initial volume estimate.
- Allowed sitemap/RSS: authoritative URL lists, if present.
- Bounded Scrapy discovery: canonicalization, rate limiting, robots compliance.

Optional tools are never prerequisites:

| Missing tool | Fallback |
|---|---|
| Firecrawl | limited Scrapy discovery + sitemap/RSS |
| Browser renderer | raw HTTP only; record JS-dependent gaps |
| Forum extractor | compare a Scrapy control sample, then a site-specific Scrapy adapter |
| Browsertrix/WARC | keep raw responses + SQLite/JSONL; record the missing snapshot |
| OCR/embeddings/Qdrant | skip derived indexes; never touch originals |

## Template sampling

Pick at least one representative per unique template_type and record: title, breadcrumbs, album/section, page type, media cards, originals, previews, captions, pagination, next/previous, canonical URL, service graphics, dates/authors, entity IDs, lazy-load/JS flags.

If raw HTML and rendered DOM differ, fix only that template type in the browser toolchain — never the whole domain by default.