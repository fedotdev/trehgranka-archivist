# Dry-run and Test Report (forum)

## Small test set

Before a full run:

- one board section (forum listing) and its pagination;
- one multi-page topic (2-3 pages minimum) plus one single-page topic;
- one topic with attachments, one topic with quotes/reactions;
- one public author page;
- one attachment file per present type (PDF, DjVu, Office, images).

## Compare

- sizes, MIME types, dimensions, captions and provenance;
- pagination and absence of repeats across topic pages;
- originals vs thumbnails (never substitute a thumbnail for an original);
- specialized forum extractor vs Scrapy controls when relevant;
- per-entity counts: categories, forums, topics, posts, quotes, reactions, authors, attachments.

## Verdict

`ready` / `needs changes`. Ask the user for explicit confirmation before the full run. Evidence for every finding: URL, selector/rule, HTTP status, counts, log/DB path.

## IPS pagination (Invision Community)

IPS topics paginate as `/forums/topic/<slug>/page/N/`; the page advertises the range in `<head>` via `<link rel="next">` / `<link rel="last">` and in `ul.ipsPagination` via `data-pages="N"` (plus `data-ipsPagination-pages`). `scripts/extractors/invision.py --html <page>` reports `current`, `next`, `last`, `total_pages` from those markers.

Dry-run boundary rule: sample the first page and the **last** page of one multi-page topic only (both carry `data-pages`), verify the last page's post count is the expected short tail, and never walk every page in dry-run — pagination is expansion, not crawl. Record `next`/`last` as discovered URLs so the full run resumes from a complete page range without revisiting page 1.