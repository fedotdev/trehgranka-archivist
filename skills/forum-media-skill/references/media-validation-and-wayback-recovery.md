# Media Validation & Wayback Recovery

## Validation rules

For every file record: request URL, final URL, source page URL, referrer, HTTP status, content type, content length, original filename, local path, SHA-256, download/validation status.

An image additionally records: Pillow verify(), width, height, format, mode, frame count, EXIF (read-only), pHash/dHash when needed, media role (`original` / `candidate_original` / `thumbnail` / `navigation_asset` / `document_scan` / `unknown`).

A PDF/DjVu/Office file additionally records: signature and decodability, metadata/text when applicable, page count when available, OCR-queue status for scans.

Never declare `candidate_original` a verified original without objective signs: wrapper link, resolution, size, DOM role, HTTP response or template rule.

## Invalid live file triggers

- persistent 404/410/403/5xx after retries;
- empty body;
- wrong Content-Type vs content;
- magic bytes not matching the format;
- undecodable image / failed Pillow verify();
- HTML or placeholder stub;
- loaded known placeholder;
- anomalous size relative to the presumed original;
- mass-identical stub file across URLs.

## Wayback/Archive Recovery (mandatory)

1. Never overwrite or delete the live response.
2. Record status, headers, bytes, SHA-256, failure reason.
3. Query Wayback CDX for the exact original URL.
4. Collect all captures with timestamp, original, statuscode, mimetype, digest, length.
5. Try URL variants: http/https, www/non-www, raw/final, encoded/decoded, without optional query params, historical domains.
6. Rank by proximity to the page/post publication date — never auto-pick the newest.
7. Download via raw replay with `id_`.
8. Run the candidate through full media validation; discard HTML, errors, empty, corrupt and placeholder responses.
9. Keep the full list of checked captures; stop only after a confirmed original is found.
10. If the direct URL fails, recover the archived source page and re-run CDX against the historical URLs found in it.
11. Only a thumbnail found: store it with `media_role=thumbnail` and `recovery_status=recovered_as_thumbnail_only`.
12. Multiple distinct valid versions: keep all, do not pick one without evidence.
13. Never construct or guess a historical URL without recording the rule and hypothesis source.
14. Respect Internet Archive rate limits and retries.
15. Save Page Now cannot restore an already-deleted file — do not use it for recovery.
16. Store provenance per capture: timestamp, original, replay URL, statuscode, mimetype, digest, length, local SHA-256, dimensions, validation result, recovery method/confidence.

A file is not recovered until it passes technical validation. The archive is not complete until the recovery queue is processed.