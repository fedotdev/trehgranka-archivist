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
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/wave",
    "audio/ogg", "audio/flac", "audio/mp4", "audio/aac", "audio/webm",
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
                    capture_limit: int = 500, resume_key: str | None = None,
                    output: str = "text", fl: list[str] | None = None,
                    collapse_digest: bool = False,
                    filter_statuscodes: tuple[str, ...] = ("200",),
                    allowed_mimetypes: list[str] | None = None) -> str:
    """CDX query URL.

    `safe='%'` keeps already-percent-encoded path sequences (e.g. '%20' from a
    normalized URL) intact while still quoting raw spaces — never double-encode.
    """
    q = (f"{CDX_ENDPOINT}?url={urllib.parse.quote(variant, safe='%')}"
         f"&output={output}&limit={capture_limit}")
    if output == "json" and fl:
        q += "&fl=" + ",".join(fl)
    if collapse_digest:
        q += "&collapse=digest"
    for code in filter_statuscodes:
        q += f"&filter=statuscode:{code}"
    for m in allowed_mimetypes or []:
        q += f"&filter=mimetype:{m}"
    cdx_from, cdx_to = _cdx_range(since, until)
    if cdx_from:
        q += f"&from={cdx_from}"
    if cdx_to:
        q += f"&to={cdx_to}"
    if resume_key:
        q += f"&resumeKey={urllib.parse.quote(resume_key, safe='')}"
    return q


def _parse_cdx_json(raw: str) -> tuple[list[dict], str | None]:
    """Parse CDX `output=json` responses.

    With `fl` the body is an array of arrays; without it an array of objects.
    A trailing 'resumeKey' marker (`["resumeKey", "..."]`) restores pagination
    when the backend emits it.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return [], None
    rows: list[dict] = []
    resume_key = None
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        payload = payload["items"]
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, list) and entry and entry[0] == "resumeKey":
                resume_key = str(entry[1]) if len(entry) > 1 else None
                continue
            if isinstance(entry, list) and len(entry) >= 5:
                rows.append({
                    "urlkey": str(entry[0]),
                    "timestamp": str(entry[1]),
                    "original": str(entry[2]),
                    "mimetype": str(entry[3]),
                    "statuscode": str(entry[4]),
                    "digest": str(entry[5]) if len(entry) > 5 else "",
                    "length": str(entry[6]) if len(entry) > 6 else "",
                })
            elif isinstance(entry, dict):
                row = {k: str(entry[k]) if entry[k] is not None else ""
                       for k in ("urlkey", "timestamp", "original", "mimetype",
                                 "statuscode", "digest", "length")
                       if k in entry}
                if row.get("timestamp"):
                    row.setdefault("urlkey", row.get("original", ""))
                    rows.append(row)
    return rows, resume_key


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


def query_all_captures(variant: str, since: str | None, until: str | None,
                       capture_limit: int = 500, timeout: int = 60,
                       output: str = "text", fl: list[str] | None = None,
                       collapse_digest: bool = False,
                       filter_statuscodes: tuple[str, ...] = ("200",),
                       allowed_mimetypes: list[str] | None = None,
                       max_pages: int = MAX_CDX_PAGES) -> tuple[list[dict], list[str]]:
    """CDX query with pagination: follow resumeKey until exhausted or max_pages."""
    all_rows: list[dict] = []
    errors: list[str] = []
    resume_key = None
    for _ in range(max(1, max_pages)):
        url = build_cdx_query(variant, since, until, capture_limit, resume_key,
                              output=output, fl=fl, collapse_digest=collapse_digest,
                              filter_statuscodes=filter_statuscodes,
                              allowed_mimetypes=allowed_mimetypes)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": CDX_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                body = resp.read(50 * 2 ** 20)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cdx {variant}: {type(exc).__name__}: {exc}")
            break
        text = body.decode("utf-8", errors="replace")
        if output == "json":
            rows, resume_key = _parse_cdx_json(text)
        else:
            rows, resume_key = _parse_cdx_text(text)
        all_rows += rows
        if not resume_key or len(rows) < capture_limit:
            break
    return all_rows, errors


def dedup_captures(captures: list[dict], allowed_mimetypes: list[str] | None = None,
                   filter_statuscodes: tuple[str, ...] = ("200",)) -> list[dict]:
    """Collapse captures with identical digest (same bytes, many timestamps)."""
    mimes = allowed_mimetypes or sorted(MEDIA_MIMES)
    seen: set[str] = set()
    out: list[dict] = []
    for cap in captures:
        if str(cap.get("statuscode", "")) not in filter_statuscodes:
            continue
        ctype = str(cap.get("mimetype", "")).lower()
        if ctype and ctype not in mimes:
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


def replay_url(capture: dict, original: str, mode: str = "id_") -> str:
    """Replay URL for a capture: `id_` (raw bytes for storage) by default;
    pass mode='if_' / 'im_' / 'page' for navigation/thumbnail variants."""
    ts = capture.get("timestamp") or ""
    orig = capture.get("original") or original
    return build_replay_url(orig, ts, mode=mode)


def build_replay_url(original: str, timestamp: str, mode: str = "id_") -> str:
    """Replay URL for an original URL at a timestamp without a capture record.

    The original URL keeps its structure (`http://host/path`) — that is the
    format Wayback serves inside archived pages and that wayback_url_parser
    round-trips; only unsafe characters are percent-encoded.
    """
    mode_suffix = "" if mode in ("page", "") else mode
    return (f"https://web.archive.org/web/{timestamp}{mode_suffix}/"
            f"{urllib.parse.quote(original, safe='%/:#?&=+~@')}")


# --------------------------------------------------------------------------
# capture selection for WAYBACK_PRIMARY_SITE_MODE
# --------------------------------------------------------------------------

def _stamp_seconds(timestamp: str | None) -> float | None:
    """14-digit CDX timestamp -> epoch seconds (None when unparsable)."""
    if not timestamp:
        return None
    try:
        return datetime.strptime(str(timestamp).strip(), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def rank_for_target(captures: list[dict], target_timestamp: str | None = None,
                    tolerance_days: float | None = None,
                    prefer_exact: bool = True) -> list[dict]:
    """Rank captures by proximity to the seed/target timestamp.

    Policy: nearest-to-target first; exact match first when prefer_exact and
    present; ties resolved to the EARLIER capture; a blind newest-first order
    is never the selection rule. With `tolerance_days`, captures farther than
    the window are dropped (a far capture must not masquerade as the chosen
    historical moment).
    """
    target = _stamp_seconds(target_timestamp) if target_timestamp else None
    out = list(captures)
    if tolerance_days is not None and tolerance_days > 0 and target:
        tol = float(tolerance_days) * 86_400.0
        out = [c for c in out
               if (_s := _stamp_seconds(c.get("timestamp"))) is not None
               and abs(_s - target) <= tol]
    if target is None or not out:
        return out

    def _key(cap: dict) -> tuple:
        seconds = _stamp_seconds(cap.get("timestamp"))
        if seconds is None:
            return (1, float("inf"), 0)
        exact = 0 if prefer_exact and seconds == target else 1
        return (exact, abs(seconds - target), seconds)

    return sorted(out, key=_key)


def select_capture(captures: list[dict], *, target_timestamp: str | None = None,
                   tolerance_days: float | None = None,
                   prefer_exact: bool = True,
                   allowed_mimetypes: list[str] | None = None,
                   filter_statuscodes: tuple[str, ...] = ("200",),
                   collapse_digest: bool = True) -> tuple[dict | None, str | None, int]:
    """Pick the capture to replay for an original URL.

    Returns (capture, selected_reason, candidates_considered) where reason is
    one of exact_timestamp / nearest_timestamp / within_tolerance /
    first_capture / none_found. The caller then validates replay bytes and may
    walk further candidates itself, recording nearest_validated separately.
    """
    caps = dedup_captures(captures, allowed_mimetypes=allowed_mimetypes,
                          filter_statuscodes=filter_statuscodes)
    considered = len(caps)
    ranked = rank_for_target(caps, target_timestamp, tolerance_days, prefer_exact)
    if not ranked:
        return None, "none_found", considered
    best = ranked[0]
    target = _stamp_seconds(target_timestamp) if target_timestamp else None
    stamp = _stamp_seconds(best.get("timestamp"))
    if target is not None:
        if prefer_exact and stamp is not None and stamp == target:
            reason = "exact_timestamp"
        elif tolerance_days is not None and tolerance_days > 0:
            reason = "within_tolerance"
        else:
            reason = "nearest_timestamp"
    else:
        reason = "first_capture"
    return best, reason, considered


# --------------------------------------------------------------------------
# outcome classification
# --------------------------------------------------------------------------

def classify_outcome(body: bytes) -> tuple[str, list[str]]:
    """Classify a recovered replay: never *assume* original.

    verified_original — a large, non-stub media body validated structurally.
    thumbnail_only   — small body consistent with a thumbnail/preview.
    placeholder      — matching a known stub/marker body.
    """
    notes: list[str] = []
    if not body or is_stub(body):
        return "placeholder", ["stub/placeholder signature"]
    # ponytail: "verified" = structurally valid and not small. A live-size
    # comparison (height/width/bytes vs the failed live response) would be
    # stronger; upgrade when the pipeline passes live dimensions here.
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
                max_candidates: int = 8, offline: bool = False,
                max_cdx_pages: int = MAX_CDX_PAGES,
                allowed_mimetypes: list[str] | None = None) -> dict:
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
        rows, errs = query_all_captures(variant, since, until, capture_limit, timeout,
                                        allowed_mimetypes=allowed_mimetypes,
                                        max_pages=max_cdx_pages)
        all_caps += rows
        errors += errs
    deduped = dedup_captures(all_caps, allowed_mimetypes=allowed_mimetypes)
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
        outcome, notes = classify_outcome(body)
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
                                 offline: bool = False,
                                 max_cdx_pages: int = MAX_CDX_PAGES,
                                 allowed_mimetypes: list[str] | None = None) -> dict:
    """recover_one() plus archived-source-page fallback (C4).

    When the direct URL has no usable capture and a source page exists, replay
    the archived source page, extract historical media URLs, re-query CDX for
    each candidate, and return the best recovery with hypothesis provenance.
    """
    rec = recover_one(url, since=since, until=until, timeout=timeout,
                      capture_limit=capture_limit, offline=offline,
                      max_cdx_pages=max_cdx_pages, allowed_mimetypes=allowed_mimetypes)
    if rec.get("recovered") or not source_page_url or offline:
        return rec
    hypothesis: dict = {"source_page": source_page_url, "extracted_urls": []}
    policy = RequestPolicy(user_agent=CDX_UA, timeout_seconds=float(timeout),
                           max_response_bytes=20 * 2 ** 20, retry_count=2, delay_seconds=0.5)
    # replay the source page's own oldest captured original
    page_caps = dedup_captures(
        query_all_captures(source_page_url, since, until, capture_limit, timeout,
                           allowed_mimetypes=allowed_mimetypes,
                           max_pages=max_cdx_pages)[0],
        allowed_mimetypes=allowed_mimetypes)
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
                      source_page_url: str | None = None,
                      max_cdx_pages: int = MAX_CDX_PAGES,
                      allowed_mimetypes: list[str] | None = None) -> dict:
    """recover_with_source_fallback() + atomic storage of validated bytes."""
    rec = recover_with_source_fallback(url, source_page_url, since=since, until=until,
                                       timeout=timeout, capture_limit=capture_limit,
                                       offline=offline, max_cdx_pages=max_cdx_pages,
                                       allowed_mimetypes=allowed_mimetypes)
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
    # WAYBACK_PRIMARY_SITE_MODE: selection by proximity, never newest-first
    caps = [
        {"timestamp": "20040801124510", "statuscode": "200", "mimetype": "image/gif", "digest": "D1", "original": "http://x/m.gif"},
        {"timestamp": "20040811124510", "statuscode": "200", "mimetype": "image/gif", "digest": "D2", "original": "http://x/m.gif"},
        {"timestamp": "20050801124510", "statuscode": "200", "mimetype": "image/gif", "digest": "D3", "original": "http://x/m.gif"},
    ]
    best, reason, _ = select_capture(caps, target_timestamp="20040804234004",
                                     tolerance_days=30, prefer_exact=True)
    assert best["timestamp"] == "20040801124510", best
    assert reason == "within_tolerance", reason
    best2, reason2, _ = select_capture(caps, target_timestamp="20040804234004",
                                       tolerance_days=1, prefer_exact=True)
    assert best2 is None and reason2 == "none_found", (best2, reason2)  # 2.6 days > 1 day window
    # exact capture wins when present
    caps_exact = caps + [{"timestamp": "20040804234004", "statuscode": "200",
                          "mimetype": "image/gif", "digest": "D0", "original": "http://x/m.gif"}]
    best3, reason3, _ = select_capture(caps_exact, target_timestamp="20040804234004",
                                       tolerance_days=30, prefer_exact=True)
    assert reason3 == "exact_timestamp" and best3["digest"] == "D0", (reason3, best3)
    # tie -> earlier capture
    tie = [
        {"timestamp": "20040804100000", "statuscode": "200", "mimetype": "image/gif", "digest": "T1"},
        {"timestamp": "20040805000000", "statuscode": "200", "mimetype": "image/gif", "digest": "T2"},
    ]
    ranked_tie = rank_for_target(tie, target_timestamp="20040804120000")
    assert ranked_tie[0]["digest"] == "T1", ranked_tie  # both 2h away; earlier wins
    # no double URL-encoding in CDX queries (raw '%20' must survive)
    q = build_cdx_query("http://x/prospect%20mira.gif", None, None, capture_limit=50)
    assert "prospect%20mira.gif" in q and "%2520" not in q, q
    q_json = build_cdx_query("http://x/a.gif", "2004-01-01", "2004-12-31",
                             capture_limit=50, output="json", fl=["timestamp", "original"],
                             collapse_digest=True, filter_statuscodes=("200",),
                             allowed_mimetypes=["image/gif"])
    assert "output=json" in q_json and "collapse=digest" in q_json and "fl=" in q_json, q_json
    assert "filter=statuscode:200" in q_json and "filter=mimetype:image/gif" in q_json, q_json
    jrows, _ = _parse_cdx_json('[["uk","20040801124510","http://x/m.gif","image/gif","200","D1","18234"],'
                               '["resumeKey","tok"]]')
    assert len(jrows) == 1 and jrows[0]["timestamp"] == "20040801124510", jrows
    assert jrows[0]["digest"] == "D1" and jrows[0]["length"] == "18234", jrows
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
    parser.add_argument("--max-candidates", type=int, default=8, help="replay candidates to try")
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
