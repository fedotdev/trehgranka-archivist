#!/usr/bin/env python3
"""
wayback_url_parser.py — parse Wayback Machine replay URLs into seed records.

WAYBACK_PRIMARY_SITE_MODE starts here: the input is a replay URL of the form

    https://web.archive.org/web/<timestamp><mode>/<original-url>

and everything downstream (discovery, CDX, capture selection, provenance)
derives from this record. This module:

  * detects whether a URL is a web.archive.org replay
  * extracts the 14-digit timestamp (optional: /web/ without a timestamp
    redirects to the latest capture)
  * extracts the replay mode: page (default), id_, if_, im_, js_, cs_, ...
  * recovers the original URL, including inside already-rewritten links
    embedded in archived HTML
  * computes the canonical original origin (scheme://host)
  * builds a seed record fixed into the manifest

Usage:
    python3 scripts/wayback_url_parser.py            (self-check, offline)
    python3 scripts/wayback_url_parser.py --url https://web.archive.org/web/20040804234004id_/http://x/y.gif
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse

WAYBACK_HOST = "web.archive.org"
_REPLAY_PREFIX = re.compile(r"^https?://web\.archive\.org/web/")
# timestamp is exactly 14 digits; an optional replay mode is a suffix of
# lowercase letters/underscores ('id_', 'if_', 'im_', 'js_', 'cs_', ...)
_TS = r"(\d{14})"
_MODE = r"([a-z_]{0,12})"
_REPLAY_PATH = re.compile(r"^web/" + _TS + _MODE + r"/(.*)$", re.DOTALL)


def is_wayback_url(url: str) -> bool:
    """True when the URL is a web.archive.org replay (any mode)."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == WAYBACK_HOST \
        and parsed.path.startswith("/web/")


def is_wayback_origin(url: str) -> bool:
    """True when the URL's host is web.archive.org at all (incl. CDX)."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == WAYBACK_HOST


def parse_replay_url(url: str) -> dict | None:
    """Parse a replay URL into a seed record, or None when not replay-shaped.

    Returns:
        {"seed_wayback_url", "seed_timestamp"|None, "seed_replay_mode",
         "seed_original_url"|None, "seed_original_origin"|None}
    """
    if not is_wayback_url(url):
        return None
    parsed = urllib.parse.urlparse(url.strip())
    relative = parsed.path.lstrip("/")
    match = _REPLAY_PATH.match(relative)
    if not match:
        if relative.startswith("web/"):
            # '/web/' with nothing resolvable after it
            original = relative[4:]
            if not original:
                return None
            record = {
                "seed_wayback_url": url,
                "seed_timestamp": None,
                "seed_replay_mode": "page",
                "seed_original_url": original,
                "seed_original_origin": _origin(original),
            }
            return record
        return None
    timestamp, mode, original = match.group(1), match.group(2), match.group(3)
    if not original:
        return None
    mode = mode or "page"
    return {
        "seed_wayback_url": url,
        "seed_timestamp": timestamp,
        "seed_replay_mode": mode,
        "seed_original_url": original,
        "seed_original_origin": _origin(original),
    }


def restore_original_url(wayback_url: str) -> str | None:
    """Recover the original URL from an already-rewritten Wayback link.

    Works for both plain replay links and links with replay modifiers —
    including the ones Wayback injects into archived HTML.
    """
    if not is_wayback_url(wayback_url):
        return None
    record = parse_replay_url(wayback_url)
    if not record:
        return None
    original = record["seed_original_url"]
    if not original:
        return None
    # Defensive: Wayback can nest 'web/...' fragments inside query strings;
    # only URL-shaped originals are meaningful.
    if is_wayback_origin(original):
        return restore_original_url(original)
    return original


def _origin(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def origin_of(url: str) -> str | None:
    """Canonical scheme://host of any URL (original or archive), None if invalid."""
    return _origin(url)


def build_seed(*, wayback_url: str | None = None, timestamp: str | None = None,
               mode: str = "page", original_url: str | None = None) -> dict:
    """Assemble a seed record from parts (used when only the original URL and
    an explicit target timestamp are given, without a replay URL)."""
    if original_url is None and wayback_url:
        record = parse_replay_url(wayback_url)
        if record:
            timestamp = record["seed_timestamp"]
            mode = record["seed_replay_mode"]
            original_url = record["seed_original_url"]
    if not original_url:
        raise ValueError("build_seed requires an original_url")
    origin = _origin(original_url)
    if origin is None:
        raise ValueError(f"original_url is not a valid http(s) URL: {original_url!r}")
    replay = wayback_url
    if replay is None and timestamp:
        # Wayback keeps the original URL raw in the replay path (that is what
        # parse_replay_url expects back); only the fully-qualified 'page' mode
        # is written without a modifier.
        mode_suffix = "" if mode in ("page", "") else mode
        replay = f"https://{WAYBACK_HOST}/web/{timestamp}{mode_suffix}/{original_url}"
    return {
        "seed_wayback_url": replay,
        "seed_timestamp": timestamp,
        "seed_replay_mode": mode,
        "seed_original_url": original_url,
        "seed_original_origin": origin,
    }


def probe() -> int:
    """Offline self-check of every parse path."""
    # plain page replay
    r = parse_replay_url("https://web.archive.org/web/20040804234004/http://metro-net.da.ru/")
    assert r is not None and r["seed_timestamp"] == "20040804234004", r
    assert r["seed_replay_mode"] == "page", r
    assert r["seed_original_url"] == "http://metro-net.da.ru/", r
    assert r["seed_original_origin"] == "http://metro-net.da.ru", r
    # raw id_ mode
    r = parse_replay_url("https://web.archive.org/web/20040801124510id_/http://metro-net.da.ru/img/map.gif")
    assert r is not None and r["seed_replay_mode"] == "id_", r
    assert r["seed_timestamp"] == "20040801124510", r
    # if_ mode
    r = parse_replay_url("https://web.archive.org/web/20040804234004if_/http://metro-net.da.ru/")
    assert r is not None and r["seed_replay_mode"] == "if_", r
    # timestamp-less replay resolves to the latest capture
    r = parse_replay_url("https://web.archive.org/web/http://metro-net.da.ru/")
    assert r is not None and r["seed_timestamp"] is None, r
    assert r["seed_original_url"] == "http://metro-net.da.ru/", r
    # restore original from an embedded rewritten link
    orig = restore_original_url(
        "https://web.archive.org/web/20040804234004/http://metro-net.da.ru/img/map.gif")
    assert orig == "http://metro-net.da.ru/img/map.gif", orig
    orig2 = restore_original_url(
        "https://web.archive.org/web/20040801124510im_/http://metro-net.da.ru/img/map.gif")
    assert orig2 == "http://metro-net.da.ru/img/map.gif", orig2
    # plain GET requests to web.archive.org are not replay links
    assert not is_wayback_url("https://web.archive.org/cdx/search/cdx?url=x"), "cdx is not a replay"
    # build_seed from original + timestamp
    s = build_seed(timestamp="20040804234004", original_url="http://metro-net.da.ru/")
    assert s["seed_wayback_url"].endswith("/web/20040804234004/http://metro-net.da.ru/"), s
    # www-case canonical origin
    s2 = build_seed(wayback_url="https://web.archive.org/web/20100101000000id_/https://WWW.Example.org/a b.gif")
    assert s2["seed_original_origin"] == "https://www.example.org", s2
    print("wayback_url_parser probe ok: replay/id_/if_/restore/build_seed hold")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wayback replay URL parser (trehgranka-archivist)")
    parser.add_argument("--url", help="replay URL to parse")
    parser.add_argument("--probe", action="store_true", help="offline self-check")
    args = parser.parse_args(argv)
    if args.probe:
        return probe()
    if not args.url:
        parser.error("--url is required")
    record = parse_replay_url(args.url)
    if record is None:
        print(f"not a replay URL: {args.url}", file=sys.stderr)
        return 1
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())