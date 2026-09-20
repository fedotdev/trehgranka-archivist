# Completeness & Coverage

## Measurable completeness

Compare independent sources:

- Firecrawl map (if present);
- Scrapy graph;
- sitemap;
- RSS;
- rendered DOM samples;
- WARC index (if present);
- pagination/next/previous;
- srcset, data-attributes, JSON-LD, Open Graph, iframe.

Build a difference report between URL sets and explain each row:

- out of scope;
- duplicate canonical URL;
- blocked by robots;
- requires login;
- failed;
- unsupported;
- parser gap;
- intentionally skipped;
- unresolved.

## Coverage formula

```
coverage = verified_in_scope_resources / uniquely_discovered_in_scope_resources
```

Report coverage separately for: HTML, original media, thumbnails, documents, forum topics, forum posts, attachments.

## Never call the archive complete while

- pagination is unhandled;
- unique templates are untested;
- failed URLs are unprocessed;
- retries are unresolved;
- discovered vs processed vs verified reconciliation is missing;
- the recovery queue is not processed.