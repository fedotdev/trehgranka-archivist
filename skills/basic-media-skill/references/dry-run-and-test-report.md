# Dry-run and Test Report

## Small test set

Before a full run:

- gallery: ≥ 3 candidates per original, 3 previews, 1 service asset, one file per significant type;
- documents/scans: one PDF/DjVu/Office file per type present;
- forum (in forum-media-skill): one section, one multi-page topic, one topic with attachments, one topic with quotes/reactions.

## Compare

- sizes, MIME types, dimensions, captions and provenance;
- pagination and absence of repeats;
- originals vs thumbnails (never substitute a thumbnail for an original);
- specialized forum extractor vs Scrapy controls when relevant.

## Verdict

`ready` / `needs changes`. Ask the user for explicit confirmation before the full run. Evidence for every finding: URL, selector/rule, HTTP status, counts, log/DB path.