#!/usr/bin/env python3
"""
Wayback / Archive Recovery worker for the archivist skills.

Real recovery pipeline, not just a CDX ranker: for every invalid media URL it

  1. queries the CDX index (text output) for exact URL + http/https + www/non-www variants
  2. dedups captures by digest
  3. filters out non-200 statuses and non-media MIME types
  4. ranks captures by proximity to the publication-date window (never blind-newest)
  5. downloads the replay via `id_` for the top candidates
  6. validates the replay bytes (magic + structural decode from archivist_core)
  7. stores the file atomically and returns a full provenance record

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
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from archivist_core import atomic_store, fetch, sha256, validate_media  # shared layer lives next to this file

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
CDX_UA = "trehgranka-archivist-wayback/1.0 (+non-commercial research)"
MEDIA_MIMES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/x-icon",
    "application/pdf", "application/zip", "application/octet-stream",
}


def url_variants(url: str) -> list[str]:
    """URL variants the spec requires trying: http/https, www/non-www, no query.

    Strips userinfo (credentialed targets are invalid upstream and would be
    mangled into a fake www.host form)."""
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
            for query in (parsed.query, ""):  # include the no-query variant
                rebuilt = urllib.parse.urlunparse((scheme, h, parsed.path, parsed.params, query, ""))
                if rebuilt not in seen:
                    seen.add(rebuilt)
                    out.append(rebuilt)
    return out


def build_cdx_query(variant: str, since: str | None, until: str | None,
                    capture_limit: int = 500) -> str:
    q = f"{CDX_ENDPOINT}?url={urllib.parse.quote(variant, safe='')}&output=text&limit={capture_limit}"
    if since:
        q += f"&from={since.replace('-', '')}"
    if until:
        q += f"&to={until.replace('-', '')}"
    return q


def load_captures(cdx_url: str, timeout: int = 60) -> list[dict]:
    """Query the Wayback CDX API (text format) and return capture rows.

    Default CDX text rows: urlkey timestamp original mimetype statuscode
    digest length [redirect]. Handles the JSON response too, should an
    endpoint reply with output=json."""
    req = urllib.request.Request(cdx_url, headers={"User-Agent": CDX_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8", errors="replace")
    raw = raw.strip()
    if raw.startswith("["):  # JSON shape from output=json endpoints
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            rows = []
        return [
            {
                "timestamp": r[1] if len(r) > 1 else "",
                "original": r[2] if len(r) > 2 else "",
                "statuscode": r[4] if len(r) > 4 else "",
                "mimetype": r[3] if len(r) > 3 else "",
                "digest": r[5] if len(r) > 5 else "",
                "length": r[6] if len(r) > 6 else "",
            }
            for r in rows
        ]
    captures: list[dict] = []
    for line in raw.splitlines():
        parts = line.split(" ")
        if len(parts) < 5:
            continue
        captures.append(
            {
                "timestamp": parts[1],
                "original": parts[2],
                "statuscode": parts[4] if len(parts) > 4 else "",
                "mimetype": parts[3] if len(parts) > 3 else "",
                "digest": parts[5] if len(parts) > 5 else "",
                "length": parts[6] if len(parts) > 6 else "",
            }
        )
    return captures


def dedup_captures(captures: list[dict]) -> list[dict]:
    """Collapse captures with identical digest (same bytes, many timestamps).
    Revisit/redirect rows have digest '-' or empty — keep them only if they
    are the only row for that original."""
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


def rank_captures(captures: list[dict], since: str | None, until: str | None) -> list[dict]:
    """Rank by proximity to a date window; never blind-pick the newest.

    Policy (explicit): captures inside [since, until] first, then by distance
    from the window center; without a window, oldest-first (0-cache order) and
    documented as such — never the newest."""
    lo = hi = None
    if since and until:
        lo = int(datetime.strptime(since, "%Y-%m-%d").strftime("%Y%m%d"))
        hi = int(datetime.strptime(until, "%Y-%m-%d").strftime("%Y%m%d"))
    center = (lo + hi) // 2 if (lo and hi) else None

    def _key(cap: dict) -> tuple:
        stamp = int(cap.get("timestamp", "0")[:8] or "0")
        if center is None:
            return (stamp, cap.get("timestamp", ""))  # oldest-first, documented
        in_window = 0 if lo <= stamp <= hi else 1
        return (in_window, abs(stamp - center), cap.get("timestamp", ""))

    return sorted(captures, key=_key)


def replay_url(capture: dict, original: str) -> str:
    ts = capture.get("timestamp") or ""
    orig = capture.get("original") or original
    return f"https://web.archive.org/web/{ts}id_/{urllib.parse.quote(orig, safe='')}"


def recover_one(url: str, *, since: str | None = None, until: str | None = None,
                timeout: int = 60, capture_limit: int = 50,
                max_candidates: int = 8, offline: bool = False) -> dict:
    """Recover one media URL from the Wayback Machine.

    Returns a record:
      {source_url, since, until, variants_checked, captures_queried,
       capture_deduped, candidate_checked, recovered, capture: {...}|None,
       provenance: {...|None}, errors: [...]}
    When recovered, "body" carries the validated replay bytes (caller stores
    them) and "provenance" carries timestamp/replay/sha/digest/validation."""
    errors: list[str] = []
    if offline:
        return {
            "source_url": url, "since": since, "until": until, "recovered": False,
            "captures_queried": 0, "captures_deduped": 0, "candidates_checked": 0,
            "capture": None, "body": b"", "provenance": None,
            "errors": ["offline: recovery disabled"], "variant_hint": url_variants(url),
        }
    variants = url_variants(url)
    all_caps: list[dict] = []
    for variant in variants:
        try:
            all_caps += load_captures(build_cdx_query(variant, since, until, capture_limit),
                                      timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cdx {variant}: {type(exc).__name__}: {exc}")
    deduped = dedup_captures(all_caps)
    ranked = rank_captures(deduped, since, until)
    candidates_checked = 0
    for cap in ranked[:max_candidates]:
        candidates_checked += 1
        replay = replay_url(cap, url)
        try:
            result = fetch(replay, ua=CDX_UA, timeout=timeout, max_bytes=200 * 2 ** 20,
                           retries=2, delay=0.5)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"replay {replay}: {type(exc).__name__}: {exc}")
            continue
        if result.get("error") or result.get("status") != 200:
            errors.append(f"replay {cap.get('timestamp')}: {result.get('error') or result.get('status')}")
            continue
        body = result.get("body") or b""
        vr = validate_media(status=result.get("status"), content_type=cap.get("mimetype"), body=body)
        if vr["ok"]:
            capture = {
                "capture_timestamp": cap.get("timestamp"),
                "replay_url": replay,
                "status": 200,
                "content_type": vr["content_type"],
                "size": vr["size"],
                "sha256": vr["sha256"],
                "digest": cap.get("digest"),
                "validation": "passed",
                "warning": vr["reasons"],
                "confidence": "verified_original",
            }
            return {
                "source_url": url, "since": since, "until": until,
                "recovered": True, "variants_checked": variants,
                "captures_queried": len(all_caps), "captures_deduped": len(ranked),
                "candidates_checked": candidates_checked,
                "capture": capture, "body": body, "provenance": capture,
                "errors": errors,
            }
        else:
            errors.append(f"replay {cap.get('timestamp')}: invalid bytes ({'; '.join(vr['reasons'])})")
    return {
        "source_url": url, "since": since, "until": until,
        "recovered": False, "variants_checked": variants,
        "captures_queried": len(all_caps), "captures_deduped": len(ranked),
        "candidates_checked": candidates_checked,
        "capture": None, "body": b"", "provenance": None, "errors": errors,
    }


def recover_and_store(url: str, out_dir: Path, *, since: str | None = None,
                      until: str | None = None, timeout: int = 60,
                      capture_limit: int = 50, offline: bool = False) -> dict:
    """recover_one() + atomic storage of the validated bytes.

    Returns recover_one()'s record with an extra "stored" dict (or None)."""
    rec = recover_one(url, since=since, until=until, timeout=timeout,
                      capture_limit=capture_limit, offline=offline)
    rec["stored"] = None
    if rec.get("recovered") and rec.get("body"):
        name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "recovered.bin"
        stored = atomic_store(out_dir / name, rec["body"])
        rec["stored"] = stored
    return rec


def run_probe() -> int:
    """Offline self-check: variants, credentials handling, dedup, ranking."""
    v = url_variants("https://web.archive.org/a/b-master.jpg?x=1#frag")
    assert v[0] == "https://web.archive.org/a/b-master.jpg?x=1", v
    assert "https://web.archive.org/a/b-master.jpg" in v, v
    assert "http://web.archive.org/a/b-master.jpg" in v, v
    assert all("#" not in u for u in v), v
    cred = url_variants("http://user:pass@web.example.org/x/y.jpg")
    assert all("user" not in u for u in cred), cred  # credentials never survive
    caps = [
        {"timestamp": "20190301120000", "original": "u", "statuscode": "200", "mimetype": "image/jpeg", "digest": "x", "length": "1"},
        {"timestamp": "20170101120000", "original": "u", "statuscode": "200", "mimetype": "image/jpeg", "digest": "y", "length": "2"},
        {"timestamp": "20250101120000", "original": "u", "statuscode": "200", "mimetype": "image/jpeg", "digest": "z", "length": "3"},
        {"timestamp": "20160101120000", "original": "u", "statuscode": "301", "mimetype": "text/html", "digest": "-", "length": "4"},
    ]
    deduped = dedup_captures(caps)
    assert len(deduped) == 3, deduped
    assert all(c["statuscode"] == "200" for c in deduped)
    ranked = rank_captures(deduped, "2016-01-01", "2018-01-01")
    assert ranked[0]["timestamp"] == "20170101120000", ranked
    oldest = rank_captures(deduped, None, None)
    assert oldest[0]["timestamp"] == "20170101120000", oldest  # 301 row was dropped
    print("wayback probe ok: variants/dedup/ranking invariants hold")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Wayback recovery worker")
    parser.add_argument("--url", help="original media URL to recover")
    parser.add_argument("--output", help="directory to store recovered files")
    parser.add_argument("--offline", action="store_true", help="no network calls")
    parser.add_argument("--probe", action="store_true", help="offline self-check")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD publication-date lower bound")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD publication-date upper bound")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--limit", type=int, default=50, help="CDX capture limit")
    args = parser.parse_args()

    if args.probe:
        return run_probe()
    if not args.url:
        parser.error("--url is required unless --probe is used")

    out_dir = Path(args.output) if args.output else Path("recovered")
    rec = recover_and_store(args.url, out_dir, since=args.since, until=args.until,
                            timeout=args.timeout, capture_limit=args.limit,
                            offline=args.offline)
    print(json.dumps(
        {k: v for k, v in rec.items() if k != "body"},
        ensure_ascii=False, indent=2,
        default=lambda o: str(o)))
    return 0 if rec["recovered"] or args.offline else 3


if __name__ == "__main__":
    raise SystemExit(main())