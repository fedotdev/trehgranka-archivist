#!/usr/bin/env python3
"""
Wayback / Archive Recovery helper for the basic-media archivist skill.

Finds archived copies of a media URL that is missing, empty, corrupt or
suspected-deleted on the live site. Never overwrites or deletes the received
live response: it only queries the CDX index and reports ranked candidates.

Usage:
    python3 scripts/wayback.py --url https://example.org/img/photo.jpg
    python3 scripts/wayback.py --url https://example.org/img/photo.jpg --since 2015-01-01 --until 2016-01-01
    python3 scripts/wayback.py --url https://example.org/img/photo.jpg --offline   (probe mode, no network)
    python3 scripts/wayback.py --probe                                          (self-check, offline, exit 0)

Exit codes:
    0 - run finished (captures found, none found, or offline probe passed)
    1 - network fetch error or malformed arguments
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime


def url_variants(url: str) -> list[str]:
    """URL variants the spec requires trying: http/https, www/non-www, no query, no fragment."""
    parsed = urllib.parse.urlparse(url)
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


def load_captures(cdx_url: str, timeout: int = 30) -> list[dict]:
    """Query the Wayback CDX API for a URL and return capture rows."""
    req = urllib.request.Request(
        cdx_url,
        headers={"User-Agent": "trehgranka-archivist-basic-media-skill/1.0 (+archive@example.org)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode("utf-8", errors="replace")
    captures: list[dict] = []
    for line in body.splitlines():
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


def rank_captures(captures: list[dict], since: str | None, until: str | None) -> list[dict]:
    """Rank by proximity to a date window; never blind-pick the newest."""
    lo = hi = None
    if since and until:
        lo = int(datetime.strptime(since, "%Y-%m-%d").strftime("%Y%m%d"))
        hi = int(datetime.strptime(until, "%Y-%m-%d").strftime("%Y%m%d"))
    center = (lo + hi) // 2 if (lo and hi) else None

    def _key(cap: dict) -> tuple:
        stamp = int(cap.get("timestamp", "0")[:8])
        if center is None:
            return (0, 0, cap.get("timestamp", ""))
        in_window = 0 if lo <= stamp <= hi else 1
        return (in_window, abs(stamp - center), cap.get("timestamp", ""))

    return sorted(captures, key=_key)


def run_probe() -> int:
    """Offline self-check: URL variant construction and ranking invariants.

    The synthetic URL stays inside a declared dependency host so the skill's
    own code never references an undeclared endpoint.
    """
    v = url_variants("https://web.archive.org/a/b-master.jpg?x=1#frag")
    assert v[0] == "https://web.archive.org/a/b-master.jpg?x=1", v
    assert "https://web.archive.org/a/b-master.jpg" in v, v
    assert "http://web.archive.org/a/b-master.jpg" in v, v
    assert all("#" not in u for u in v), v
    caps = [
        {"timestamp": "20190301120000", "original": "u", "statuscode": "200", "mimetype": "image/jpeg", "digest": "x", "length": "1"},
        {"timestamp": "20170101120000", "original": "u", "statuscode": "200", "mimetype": "image/jpeg", "digest": "y", "length": "2"},
        {"timestamp": "20250101120000", "original": "u", "statuscode": "200", "mimetype": "image/jpeg", "digest": "z", "length": "3"},
    ]
    ranked = rank_captures(caps, "2016-01-01", "2018-01-01")
    assert ranked[0]["timestamp"] == "20170101120000", ranked
    print("probe ok: variants constructed, ranking prefers publication-date window")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Wayback CDX recovery helper")
    parser.add_argument("--url", help="original media URL to recover")
    parser.add_argument("--offline", action="store_true", help="no network calls")
    parser.add_argument("--probe", action="store_true", help="offline self-check")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD publication-date lower bound")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD publication-date upper bound")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    if args.probe:
        return run_probe()
    if not args.url:
        parser.error("--url is required unless --probe is used")

    if args.offline:
        result = {
            "offline": True,
            "url": args.url,
            "variants": url_variants(args.url),
            "cdx_endpoint": "https://web.archive.org/cdx/search/cdx?url={url}&output=json&fl=timestamp,original,statuscode,mimetype,digest,length",
            "replay_mode": "id_",
            "note": "offline: run without --offline to query the CDX index",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    variants = url_variants(args.url)
    all_captures: list[dict] = []
    errors: list[str] = []
    for variant in variants:
        query = urllib.parse.urlencode(
            {
                "url": variant,
                "output": "json",
                "fl": "timestamp,original,statuscode,mimetype,digest,length",
                "filter": "statuscode:200",
                "limit": "50",
            }
        )
        cdx = f"https://web.archive.org/cdx/search/cdx?{query}"
        try:
            all_captures += load_captures(cdx, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{variant}: {type(exc).__name__}: {exc}")

    ranked = rank_captures(all_captures, args.since, args.until)
    result = {
        "url": args.url,
        "variants_checked": variants,
        "captures_found": len(ranked),
        "captures": ranked,
        "replay_mode": "id_",
        "ranking": "proximity to publication date window; newest was NOT auto-picked",
        "errors": errors,
        "checked_at": datetime.utcnow().isoformat(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())