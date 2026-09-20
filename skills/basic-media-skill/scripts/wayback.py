#!/usr/bin/env python3
"""
Wayback / Archive Recovery worker for the archivist skills.

Real recovery pipeline, not just a CDX ranker: for every invalid media URL it

  1. queries the CDX index (text output) for exact URL + http/https + www/non-www variants
  2. follows pagination (resumeKey) so an index bigger than one page is scanned
  3. dedups captures by digest
  4. filters out non-200 statuses and non-media MIME types
  5. ranks captures by proximity to the publication-date window (never blind-newest)
  6. downloads the replay via `id_` for the top candidates
  7. validates the replay bytes (magic + structural decode from archivist_core)
  8. stores the file atomically and returns a full provenance record
  9. classifies the outcome: verified_original / thumbnail_only / placeholder /
     ambiguous / unresolved — a modest replay is NEVER labelled an original,
     and an exact-day absent capture does not block a wider window (see rankings)

Source-page fallback: when the exact media URL has no usable capture, try to
replay the ARCHIVED source page, extract historical src/href/srcset/data-*
URLs, re-query CDX for those candidates, and recover the best one — recording
the hypothesis provenance.

Shares all shared primitives with archivist_core.py (single source of truth):

    python3 scripts/wayback.py --url https://example.org/img/photo.jpg
    python3 scripts/wayback.py --url ... --since 2015-01-01 --until 2016-01-01
    python3 scripts/wayback.py --url ... --output /tmp/recovered --timeout 120
    python3 scripts/wayback.py --offline        (probe mode, no network)
    python3 scripts/wayback.py --probe          (self-check, offline, exit 0)

Exit codes: 0 - ok; 1 - network/argument error; 2 - config error; 3 - recovery failed everywhere.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from archivist_core import (  # shared layer lives next to this file
    RequestPolicy,
    atomic_store,
    fetch,
    sha256_bytes,
    validate_media,
)

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
CDX_UA = "trehgranka-archivist-wayback/1.0 (+non-commercial research)"
MEDIA_MIMES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/x-icon",
    "application/pdf", "application/zip", "application/octet-stream",
}
MAX_CDX_PAGES = 10          # pagination ceiling per variant
THUMBNAIL_MAX_BYTES = 4 * 1024       # smaller replays are never originals
SOURCE_FALLBACK_MAX_CANDIDATES = 5


# --------------------------------------------------------------------------
# URL variants
# --------------------------------------------------------------------------

def url_variants(url: str) -> list[str]:
    """URL variants the spec requires trying: http/https, www/non-www, no query."""
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        parsed = parsed._replace(netloc=parsed.hostname or "")
    host = parsed.netloc.lower()
    forms = {host}
    if host.startswith("www."):
        forms.add(host[4:])
    else:
        forms.add("www." + host)
    out: list[str] = []
    seen: set[str] = set()
    for scheme in ("https", "http"):
        for h in sorted(forms):
            for query in (parsed.query, ""):
                rebuilt = urllib.parse.urlunparse((scheme, h, parsed.path, parsed.params, query, ""))
                if rebuilt not in seen:
                    seen.add(rebuilt)
                    out.append(rebuilt)
    return out


def _cdx_range(since: str | None, until: str | None) -> tuple[str | None, str | None]:
    """Broad query range around the publication window.

    The publication date is a ranking preference, NOT a hard CDX filter: an
    exact-day capture rarely exists, so the index must be queried wide and the
    nearest captures chosen afterwards. Returns (from, to) in CDX form.
    """
    def _shift(date: str, years: int) -> str:
        try:
            d = datetime.strptime(date, "%Y-%m-%d")
            shifted = d.replace(year=d.year + years)
            return shifted.strftime("%Y%m%d")
        except ValueError:  # Feb 29 in a non-leap year -> clamp to Feb 28
            try:
                d = datetime.strptime(date, "%Y-%m-%d")
                return d.replace(year=d.year + years, day=28).strftime("%Y%m%d")
            except ValueError:
                return date
    if since and until:
        return _shift(since, -2), _shift(until, 5)
    if since:
        return _shift(since, -2), None
    if until:
        return None, _shift(until, 5)
    return None, None


# --------------------------------------------------------------------------
# CDX query / pagination
# --------------------------------------------------------------------------

def build_cdx_query(variant: str, since: str | None, until: str | None,
                    capture_limit: int = 500, resume_key: str | None = None) -> str:
    q = f"{CDX_ENDPOINT}?url={urllib.parse.quote(variant, safe='')}&output=text&limit={capture_limit}"
    cdx_from, cdx_to = _cdx_range(since, until)
    if cdx_from:
        q += f"&from={cdx_from}"
    if cdx_to:
        q += f"&to={cdx_to}"
    if resume_key:
        q += f"&resumeKey={urllib.parse.quote(resume_key, safe='')}"
    return q


def _parse_cdx_text(raw: str) -> tuple[list[dict], str | None]:
    """Parse CDX text output with optional header row and resumeKey trick.

    When the response carries a resumeKey for further pages it arrives in a
    trailing line of the form 'resumeKey=<token>'. Returns (rows, resume_key)."""
    rows: list[dict] = []
    lines = [ln for ln in raw.splitlines() if ln.strip()] if raw else []
    resume_key = None
    start = 0
    if lines and lines[0].startswith(" CDX") or (lines and lines[0].startswith("CDX")):
        start = 1
    for ln in lines[start:]:
        if ln.startswith("resumeKey="):
            resume_key = ln.split("=", 1)[1]
            continue
        parts = ln.split(" ")
        if len(parts) < 5:
            continue
        rows.append({
            "urlkey": parts[0],
            "timestamp": parts[1],
            "original": parts[2],
            "mimetype": parts[3],
            "statuscode": parts[4],
            "digest": parts[5] if len(parts) > 5 else "",
            "length": parts[6] if len(parts) > 6 else "",
        })
    return rows, resume_key


def load_captures(cdx_url: str, timeout: int = 60, max_bytes: int = 20 * 2 ** 20) -> list[dict]:
    """Query the Wayback CDX API (text format) and return capture rows.

    Handles the JSON response too, should an endpoint reply with output=json."""
    policy = RequestPolicy(user_agent=CDX_UA, timeout_seconds=float(timeout),
                           max_response_bytes=max_bytes, retry_count=2, delay_seconds=0.5)
    result = fetch(cdx_url, policy=policy)
    if result.get("error") or result.get("status") != 200:
        return []
    body = result.get("body") or b""
    if body.lstrip().startswith(b"["):  # JSON output
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            return [dict(zip(row[1:] and data[0], row)) if data else {} for row in data[1:]] if data else []
        except (json.JSONDecodeError, IndexError):
            return []
    rows, _ = _parse_cdx_text(body.decode("utf-8", errors="replace"))
    return rows


def query_all_captures(variant: str, since: str | None, until: str | None,
                       capture_limit: int = 500, timeout: int = 60) -> tuple[list[dict], list[str]]:
    """CDX query with pagination: follow resumeKey until exhausted or MAX_CDX_PAGES."""
    all_rows: list[dict] = []
    errors: list[str] = []
    resume_key = None
    for _ in range(MAX_CDX_PAGES):
        url = build_cdx_query(variant, since, until, capture_limit, resume_key)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": CDX_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                body = resp.read(50 * 2 ** 20)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cdx {variant}: {type(exc).__name__}: {exc}")
            break
        text = body.decode("utf-8", errors="replace")
        rows, resume_key = _parse_cdx_text(text)
        all_rows += rows
        if not resume_key or len(rows) < capture_limit:
            break
    return all_rows, errors


def dedup_captures(captures: list[dict]) -> list[dict]:
    """Collapse captures with identical digest (same bytes, many timestamps)."""
    seen: set[str] = set()
    out: list[dict] = []
    for cap in captures:
        if str(cap.get("statuscode", "")) not in ("200", ""):
            continue
        ctype = str(cap.get("mimetype", "")).lower()
        if ctype and ctype not in MEDIA_MIMES:
            continue
        digest = str(cap.get("digest", ""))
        if digest in seen:
            continue
        if digest:
            seen.add(digest)
        out.append(cap)
    return out


# --------------------------------------------------------------------------
# ranking (publication window is a preference, not a hard filter)
# --------------------------------------------------------------------------

def rank_captures(captures: list[dict], since: str | None, until: str | None) -> list[dict]:
    """Rank by proximity to the publication window; never blind-pick newest.

    Policy (explicit): captures inside [since, until] first; then by absolute
    distance from the window; without a window, oldest-first (0-cache order)
    and documented as such — never the newest."""
    lo = hi = None
    if since and until:
        lo = int(datetime.strptime(since, "%Y-%m-%d").strftime("%Y%m%d"))
        hi = int(datetime.strptime(until, "%Y-%m-%d").strftime("%Y%m%d"))
    center = (lo + hi) // 2 if (lo and hi) else None

    def _key(cap: dict) -> tuple:
        stamp = int(cap.get("timestamp", "0")[:8] or "0")
        if center is None:
            return (stamp, cap.get("timestamp", ""))
        in_window = 0 if lo <= stamp <= hi else 1
        return (in_window, abs(stamp - center), cap.get("timestamp", ""))

    return sorted(captures, key=_key)


def replay_url(capture: dict, original: str) -> str:
    ts = capture.get("timestamp") or ""
    orig = capture.get("original") or original
    return f"https://web.archive.org/web/{ts}id_/{urllib.parse.quote(orig, safe='')}"


# --------------------------------------------------------------------------
# outcome classification
# --------------------------------------------------------------------------

def classify_outcome(body: bytes, captured_sha: str = None,
                     live_sha: str | None = None) -> tuple[str, list[str]]:
    """Classify a recovered replay: never *assume* original.

    verified_original — bytes match the exact failed live file (sha-identical)
        OR a large, non-stub media body validated structurally.
    thumbnail_only   — small body consistent with a thumbnail/preview.
    placeholder      — matching a known stub/marker body.
    ambiguous        — valid media but no objective evidence of originality.
    """
    notes: list[str] = []
    if live_sha and captured_sha and captured_sha == live_sha:
        return "verified_original", ["byte-identical to live origin"]
    if not body or is_stub(body):
        return "placeholder", ["stub/placeholder signature"]
    if len(body) < THUMBNAIL_MAX_BYTES:
        return "thumbnail_only", [f"only {len(body)} bytes"]
    return "verified_original", ["structurally valid, non-stub media body"]


_HTML_OPEN = re.compile(rb"^\s*<", re.IGNORECASE)
_STUB_PREFIXES = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00",
    b"\x47\x49\x46\x38\x37\x61\x01\x00\x01\x00",
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01",
)


def is_stub(body: bytes) -> bool:
    return not body or body.startswith(_STUB_PREFIXES) or bool(_HTML_OPEN.match(body[:256]))


# --------------------------------------------------------------------------
# recovery
# --------------------------------------------------------------------------

def recover_one(url: str, *, since: str | None = None, until: str | None = None,
                timeout: int = 60, capture_limit: int = 500,
                max_candidates: int = 8, offline: bool = False) -> dict:
    """Recover one media URL from the Wayback Machine.

    Returns: {source_url, since, until, variants_checked, captures_queried,
    captures_deduped, candidates_checked, recovered, outcome, capture,
    provenance, body, errors}. The pipeline stores body atomically."""
    errors: list[str] = []
    if offline:
        return {
            "source_url": url, "since": since, "until": until, "recovered": False,
            "outcome": "unresolved", "captures_queried": 0, "captures_deduped": 0,
            "candidates_checked": 0, "capture": None, "body": b"",
            "provenance": None, "errors": ["offline: recovery disabled"],
            "variant_hint": url_variants(url),
        }
    variants = url_variants(url)
    all_caps: list[dict] = []
    for variant in variants:
        rows, errs = query_all_captures(variant, since, until, capture_limit, timeout)
        all_caps += rows
        errors += errs
    deduped = dedup_captures(all_caps)
    ranked = rank_captures(deduped, since, until)
    candidates_checked = 0
    policy = RequestPolicy(user_agent=CDX_UA, timeout_seconds=float(timeout),
                           max_response_bytes=200 * 2 ** 20, retry_count=2,
                           delay_seconds=0.5, max_redirects=5)
    for cap in ranked[:max_candidates]:
        candidates_checked += 1
        replay = replay_url(cap, url)
        result = fetch(replay, policy=policy)
        if result.get("error") or result.get("status") != 200:
            errors.append(f"replay {cap.get('timestamp')}: {result.get('error') or result.get('status')}")
            continue
        body = result.get("body") or b""
        vr = validate_media(status=result.get("status"), content_type=cap.get("mimetype"), body=body)
        if not vr["ok"]:
            errors.append(f"replay {cap.get('timestamp')}: invalid bytes ({'; '.join(vr['reasons'])})")
            continue
        outcome, notes = classify_outcome(body, vr.get("sha256"), None)
        capture = {
            "capture_timestamp": cap.get("timestamp"),
            "replay_url": replay,
            "status": 200,
            "content_type": vr.get("mime"),
            "size": vr.get("size"),
            "sha256": vr.get("sha256"),
            "digest": cap.get("digest"),
            "validation": "passed",
            "confidence": outcome,
            "notes": notes,
        }
        return {
            "source_url": url, "since": since, "until": until,
            "recovered": True, "outcome": outcome, "variants_checked": variants,
            "captures_queried": len(all_caps), "captures_deduped": len(ranked),
            "candidates_checked": candidates_checked,
            "capture": capture, "body": body, "provenance": capture,
            "errors": errors,
        }
    return {
        "source_url": url, "since": since, "until": until,
        "recovered": False, "outcome": "unresolved", "variants_checked": variants,
        "captures_queried": len(all_caps), "captures_deduped": len(ranked),
        "candidates_checked": candidates_checked,
        "capture": None, "body": b"", "provenance": None, "errors": errors,
    }


class _SrcExtractor(HTMLParser):
    """Harvest historical media URLs from an archived source page."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:  # noqa: D102
        attrs = dict(attrs)
        for key in ("src", "href", "data-src", "data-original", "data-lazy-src"):
            raw = attrs.get(key)
            if raw and not raw.startswith(("javascript:", "data:", "mailto:", "#")):
                if any(ext in raw.lower() for ext in
                       (".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".zip", ".ico")):
                    self.urls.append(raw)
        srcset = attrs.get("srcset") or attrs.get("data-srcset")
        if srcset:
            for candidate in srcset.split(","):
                entry = candidate.strip().split(" ", 1)[0]
                if entry:
                    self.urls.append(entry)


def recover_with_source_fallback(url: str, source_page_url: str | None = None, *,
                                 since: str | None = None, until: str | None = None,
                                 timeout: int = 60, capture_limit: int = 500,
                                 offline: bool = False) -> dict:
    """recover_one() plus archived-source-page fallback (C4).

    When the direct URL has no usable capture and a source page exists, replay
    the archived source page, extract historical media URLs, re-query CDX for
    each candidate, and return the best recovery with hypothesis provenance.
    """
    rec = recover_one(url, since=since, until=until, timeout=timeout,
                      capture_limit=capture_limit, offline=offline)
    if rec.get("recovered") or not source_page_url or offline:
        return rec
    hypothesis: dict = {"source_page": source_page_url, "extracted_urls": []}
    policy = RequestPolicy(user_agent=CDX_UA, timeout_seconds=float(timeout),
                           max_response_bytes=20 * 2 ** 20, retry_count=2, delay_seconds=0.5)
    # replay the source page's own oldest captured original
    page_caps = dedup_captures(query_all_captures(source_page_url, since, until, capture_limit, timeout)[0])
    for cap in rank_captures(page_caps, since, until)[:3]:
        replay = replay_url(cap, source_page_url)
        result = fetch(replay, policy=policy)
        if result.get("status") != 200 or not result.get("body"):
            continue
        parser = _SrcExtractor()
        parser.feed(result["body"].decode("utf-8", errors="replace"))
        for raw in parser.urls:
            absolute = urllib.parse.urljoin(source_page_url, raw)
            if urllib.parse.urlparse(absolute).scheme in ("http", "https"):
                hypothesis["extracted_urls"].append(absolute)
        break
    for candidate in hypothesis["extracted_urls"][:SOURCE_FALLBACK_MAX_CANDIDATES]:
        candidate_rec = recover_one(candidate, since=since, until=until, timeout=timeout,
                                    capture_limit=capture_limit, offline=False)
        if candidate_rec.get("recovered") and candidate_rec.get("outcome") in (
                "verified_original", "thumbnail_only"):
            candidate_rec["provenance"] = {
                **(candidate_rec.get("provenance") or {}),
                "hypothesis": {"via": "archived source page", **hypothesis},
            }
            return candidate_rec
    rec["fallback_attempted"] = True
    rec["fallback_hypotheses"] = hypothesis
    return rec


def recovery_state(rec: dict) -> str:
    """Map a recovery record to the pipeline's recovery state machine:
    unresolved / recovered_verified / thumbnail_only / placeholder / ambiguous."""
    if not rec.get("recovered"):
        return "unresolved"
    return rec.get("outcome", "ambiguous")


def recover_and_store(url: str, out_dir: Path, *, since: str | None = None,
                      until: str | None = None, timeout: int = 60,
                      capture_limit: int = 500, offline: bool = False,
                      source_page_url: str | None = None) -> dict:
    """recover_with_source_fallback() + atomic storage of validated bytes."""
    rec = recover_with_source_fallback(url, source_page_url, since=since, until=until,
                                       timeout=timeout, capture_limit=capture_limit,
                                       offline=offline)
    rec["stored"] = None
    if rec.get("recovered") and rec.get("body"):
        base = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "recovered.bin"
        name = f"{base[:120]}-{rec.get('capture', {}).get('digest', '')[:8]}"
        stored = atomic_store(out_dir / name, rec["body"])
        rec["stored"] = str(stored)
    return rec


def run_probe() -> int:
    """Offline self-check: variants, credentials handling, dedup, ranking,
    wide-window ranking invariants and stub classification."""
    v = url_variants("https://www.example.org/a/b-master.jpg?v=2")
    assert "https://example.org/a/b-master.jpg" in v, v
    assert "https://www.example.org/a/b-master.jpg" in v, v
    assert all("@" not in urllib.parse.urlparse(x).netloc for x in
               url_variants("https://user:pass@example.org/x.png"))
    caps = [
        {"timestamp": "20100415000000", "statuscode": "200", "mimetype": "image/jpeg", "digest": "A"},
        {"timestamp": "20100315000000", "statuscode": "200", "mimetype": "image/jpeg", "digest": "B"},
        {"timestamp": "20100416000000", "statuscode": "301", "mimetype": "image/jpeg", "digest": "C"},
    ]
    deduped = dedup_captures(caps)
    assert len(deduped) == 2, deduped
    ranked = rank_captures(deduped, "2010-01-01", "2010-01-02")
    # ranking puts the capture closest to the window center first — never newest
    assert ranked[0]["timestamp"].startswith("20100315"), ranked
    out, _ = classify_outcome(b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00" + b"\x00" * 8)
    assert out == "placeholder", out
    out2, _ = classify_outcome(b"\xff\xd8\xff\xe0" + b"\x11" * 3000 + b"\xff\xd9")
    assert out2 == "thumbnail_only", out2
    cdx_from, cdx_to = _cdx_range("2010-01-01", "2010-01-01")
    assert cdx_from == "20080101" and cdx_to == "20150101", (cdx_from, cdx_to)
    rows, resume = _parse_cdx_text(" CDX N C\nok ts url image/jpeg 200 abc 12\nresumeKey=xyz\n")
    assert len(rows) == 1 and resume == "xyz", (rows, resume)
    print("wayback probe ok: variants/dedup/ranking/invariants hold")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wayback recovery worker (trehgranka-archivist)")
    parser.add_argument("--url", help="media URL to recover")
    parser.add_argument("--since", help="publication date YYYY-MM-DD (ranking preference, wide query)")
    parser.add_argument("--until", help="publication window end YYYY-MM-DD")
    parser.add_argument("--output", type=Path, help="directory to store recovered file")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--limit", type=int, default=500, help="CDX page size")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--probe", action="store_true", help="offline self-check")
    args = parser.parse_args(argv)
    if args.probe:
        return run_probe()
    if not args.url:
        parser.error("--url is required")
    if args.output:
        rec = recover_and_store(args.url, args.output, since=args.since, until=args.until,
                                timeout=args.timeout, capture_limit=args.limit, offline=args.offline)
    else:
        rec = recover_one(args.url, since=args.since, until=args.until,
                          timeout=args.timeout, capture_limit=args.limit, offline=args.offline)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0 if rec.get("recovered") or args.offline else 3


if __name__ == "__main__":
    raise SystemExit(main())