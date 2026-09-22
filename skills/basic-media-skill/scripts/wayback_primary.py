#!/usr/bin/env python3
"""
wayback_primary.py — WAYBACK_PRIMARY_SITE_MODE orchestrator.

When the input is a Wayback Machine replay URL (or the target site exists only
in web.archive.org), Internet Archive is the primary source of truth:

    parse seed -> archived-HTML discovery -> CDX per original URL ->
    capture selection by timestamp proximity -> id_ storage fetch ->
    per-response validation -> raw + rewritten local copy -> provenance ->
    SQLite manifest -> coverage report

Invariants (verified by tests):
  * no live fetch from the original domain unless allow_live_fallback=true;
  * embedded resources never share the seed timestamp by assumption — every
    URL gets its own capture selection;
  * replay HTML (navigation) is never stored as the raw artifact — id_ is;
  * a resource is only 'verified' after body validation, never by CDX rows;
  * unresolved / invalid resources are counted, not silently dropped.

Offline mode (fixtures from config, no network) drives the test suite.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import archivist_core as core
import wayback
import wayback_url_parser as wup
from link_rewriter import LinkRewriter, resolve_link
from run_pipeline import LinkExtractor


# ---------------------------------------------------------------------------
# config normalization
# ---------------------------------------------------------------------------

_WB_DEFAULTS = {
    "SEED_URL": None,
    "TARGET_TIMESTAMP": None,
    "ORIGINAL_URL": None,
    "ALLOW_LIVE_FALLBACK": False,
    "PREFER_EXACT_TIMESTAMP": True,
    "TIMESTAMP_TOLERANCE_DAYS": 30.0,
    "USE_REPLAY_FOR_DISCOVERY": True,
    "USE_ID_RAW_FOR_STORAGE": True,
    "COLLAPSE_DIGEST": True,
    "STORE_RAW_HTML": True,
    "EMIT_PROVENANCE_JSONL": True,
    "FILTER_STATUSCODE": ["200"],
    "ALLOWED_MIMETYPES": None,
    "MAX_CDX_RESULTS_PER_URL": 500,
    "MAX_CDX_PAGES": wayback.MAX_CDX_PAGES,
    "MAX_CANDIDATES": 8,
    "MAX_PAGES": 2000,
    "MAX_DEPTH": 12,
    "OFFLINE_HTML": None,
    "OFFLINE_CDX": None,
    "OFFLINE_REPLAY": None,
}

_LOWER = {"source_mode", "seed_url", "target_timestamp", "original_url",
          "allow_live_fallback", "prefer_exact_timestamp",
          "timestamp_tolerance_days", "use_replay_for_discovery",
          "use_id_raw_for_storage", "collapse_digest", "store_raw_html",
          "emit_provenance_jsonl", "filter_statuscode", "allowed_mimetypes",
          "max_cdx_results_per_url", "max_cdx_pages", "max_candidates",
          "max_pages", "max_depth", "offline_html", "offline_cdx",
          "offline_replay"}

# default MIME allow-list for wayback mode: media PLUS the page-level types
# (text/html, css, js) that a static site needs; the strict media-only default
# belongs to the legacy recovery worker, not to full-site reconstruction.
_DEFAULT_ALLOWED_MIMES = sorted(set(wayback.MEDIA_MIMES) | {
    "text/html", "text/css", "application/javascript", "text/javascript"})


def normalize_wayback_config(cfg: dict) -> dict:
    """Merge flat legacy keys plus the lowercase `wayback_primary:` block into
    one uppercase-named option dict for this mode."""
    block = cfg.get("WAYBACK_PRIMARY") or cfg.get("wayback_primary") or {}
    if not isinstance(block, dict):
        block = {}
    merged: dict = dict(_WB_DEFAULTS)
    for key, value in block.items():
        if key in _LOWER or key.isupper():
            merged[key.upper()] = value
    for key, value in cfg.items():
        if key in _LOWER or key.isupper():
            merged[key.upper()] = value
    merged["USER_CONFIRMED_FULL_RUN"] = bool(cfg.get("USER_CONFIRMED_FULL_RUN", False))
    merged["ALLOWED_DOMAINS"] = cfg.get("ALLOWED_DOMAINS") or []
    merged["SOURCE_MODE"] = (cfg.get("SOURCE_MODE") or cfg.get("source_mode")
                             or "wayback_primary")
    return merged


def resolve_seed(wb: dict) -> dict:
    """Seed record from config: replay URL first, else original + timestamp."""
    if wb.get("SEED_URL"):
        record = wup.parse_replay_url(wb["SEED_URL"])
        if not record:
            raise core.ConfigError(f"SEED_URL is not a Wayback replay URL: {wb['SEED_URL']!r}")
        if wb.get("TARGET_TIMESTAMP"):
            record["seed_timestamp"] = wb["TARGET_TIMESTAMP"]
            record["timestamp_source"] = "config"
        else:
            record["timestamp_source"] = "seed_url"
        return record
    if wb.get("ORIGINAL_URL"):
        record = wup.build_seed(timestamp=wb.get("TARGET_TIMESTAMP"),
                                original_url=wb["ORIGINAL_URL"])
        record["timestamp_source"] = "config" if wb.get("TARGET_TIMESTAMP") else "none"
        return record
    raise core.ConfigError(
        "WAYBACK_PRIMARY requires SEED_URL (replay) or ORIGINAL_URL (+ target timestamp)")


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 90, max_bytes: int = 40 * 2 ** 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": wayback.CDX_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                return {"status": 0, "content_type": "", "bytes": b"",
                        "headers": {}, "error": "response exceeds byte budget"}
            return {"status": resp.status, "content_type": resp.headers.get("Content-Type", ""),
                    "bytes": body, "headers": dict(resp.headers.items()), "error": None}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code,
                "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
                "bytes": b"",
                "headers": dict(exc.headers.items()) if exc.headers else {},
                "error": f"http {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "content_type": "", "bytes": b"", "headers": {},
                "error": f"{type(exc).__name__}: {exc}"}


def _b32_sha1(body: bytes) -> str:
    return base64.b32encode(hashlib.sha1(body).digest()).decode().lower()


_HTML_STUB_MARKERS = (
    "the wayback machine has not archived",
    "this page is not available on the wayback machine",
    "page cannot be displayed",
    "it looks like this page is missing from our crawls",
    "sorry, the page you requested cannot be found",
    "blocked site error",
)


def validate_archived_html(status: int, content_type: str, body: bytes) -> dict:
    """HTML-specific validation: useful body, no Wayback service page, charset.

    media bytes still go through core.validate_media; this is additive for
    text/html responses.
    """
    result = {"ok": False, "errors": [], "charset": None, "length": len(body)}
    if status != 200:
        result["errors"].append(f"status {status}")
        return result
    if len(body) < 500:
        result["errors"].append("body too small to be a real archived page")
        return result
    low = body[:8192].decode("latin-1", errors="ignore").lower()
    for marker in _HTML_STUB_MARKERS:
        if marker in low:
            result["errors"].append(f"wayback service/stub page marker: {marker!r}")
            return result
    charset = _detect_charset(content_type, body)
    result["charset"] = charset
    text = body.decode(charset, errors="replace")
    if re.search(r"<(?:html|body|div|table|a\s|img\s)", text[:65536], re.IGNORECASE) is None:
        result["errors"].append("no document structure markers in body")
        return result
    result["ok"] = True
    return result


def validate_text_resource(status: int, content_type: str, body: bytes, kind: str) -> dict:
    """CSS/JS/text validation: non-empty, not an HTML page, sanity length."""
    result = {"ok": False, "errors": [], "length": len(body)}
    if status != 200:
        result["errors"].append(f"status {status}")
        return result
    if not body:
        result["errors"].append("empty body")
        return result
    if len(body) < 20:
        result["errors"].append("body too small for a stylesheet/script")
        return result
    low = body.decode("latin-1", errors="ignore").lower()
    if re.search(r"<(?:html|!doctype|head|body|script)", low[:4096]):
        result["errors"].append(f"html content instead of {kind}")
        return result
    result["ok"] = True
    return result


def _detect_charset(content_type: str, body: bytes) -> str:
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if match:
        return match.group(1)
    head = body[:4096].decode("latin-1", errors="ignore")
    for pattern in (r'<meta[^>]+charset=["\']?([\w-]+)', r'xml\s+encoding=["\']([\w-]+)'):
        found = re.search(pattern, head, re.IGNORECASE)
        if found:
            return found.group(1)
    return "utf-8"


# ---------------------------------------------------------------------------
# deterministic local paths
# ---------------------------------------------------------------------------

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._/-]")
_MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "application/pdf": ".pdf", "text/css": ".css",
    "application/javascript": ".js", "text/javascript": ".js",
    "application/json": ".json", "text/plain": ".txt", "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico", "application/zip": ".zip",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/ogg": ".ogg",
    "audio/flac": ".flac", "audio/mp4": ".m4a", "audio/aac": ".aac",
    "audio/webm": ".weba",
}


def sanitize_path_component(path: str) -> str:
    decoded = urllib.parse.unquote(path)
    cleaned = decoded.replace("%", "_")
    cleaned = _SAFE_NAME.sub("_", cleaned)
    return cleaned.strip("/")


def local_path_for(original_url: str, kind: str, mimetype: str,
                   existing: set[str]) -> str:
    """Deterministic site-relative storage path; colliding paths get a
    numeric suffix (never overwrite). Type folders apply only to root-level
    files without their own directory: /img/map.gif stays img/map.gif,
    /banner.gif becomes media/banner.gif."""
    parsed = urllib.parse.urlsplit(original_url)
    path = parsed.path or "/"
    if not path.strip("/"):
        base = f"index{_ext(path, mimetype, kind)}"
    else:
        base = sanitize_path_component(path) or "index"
        if kind == "html":
            base = base if base.endswith((".html", ".htm")) else base + ".html"
        else:
            base = base + _ext(path, mimetype, kind)
    if parsed.query:
        digest = hashlib.md5(parsed.query.encode()).hexdigest()[:8]
        stem, dot, ext = base.rpartition(".")
        base = f"{stem}_q{digest}.{ext}" if dot else f"{base}_q{digest}"
    if "/" not in base:
        root = {"html": "", "css": "css/", "js": "js/", "document": "documents/",
                "audio": "audio/"}.get(kind, "media/")
    else:
        root = ""
    candidate, counter = base, 2
    while candidate in existing:
        stem, dot, ext = base.rpartition(".")
        candidate = f"{stem}-{counter}.{ext}" if dot else f"{base}-{counter}"
        counter += 1
    existing.add(candidate)
    return root + candidate


def _ext(path: str, mimetype: str, kind: str) -> str:
    last = path.rsplit("/", 1)[-1]
    if "." in last:
        return ""
    mapped = _MIME_EXT.get((mimetype or "").split(";")[0].strip().lower())
    if mapped:
        return mapped
    return {"html": ".html", "css": ".css", "js": ".js"}.get(kind, ".bin")


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS seeds(
  seed_id INTEGER PRIMARY KEY AUTOINCREMENT,
  wayback_url TEXT, timestamp TEXT, replay_mode TEXT, original_url TEXT, origin TEXT);
CREATE TABLE IF NOT EXISTS pages(
  url TEXT PRIMARY KEY, kind TEXT, local_path TEXT, capture_timestamp TEXT,
  status TEXT, validation_status TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS resources(
  url TEXT PRIMARY KEY, kind TEXT, local_path TEXT, capture_timestamp TEXT,
  status TEXT, validation_status TEXT, source TEXT, cdx_error TEXT);
CREATE TABLE IF NOT EXISTS captures(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT, requested_timestamp TEXT, capture_timestamp TEXT, replay_mode TEXT,
  wayback_url TEXT, statuscode TEXT, mimetype TEXT, digest TEXT, length TEXT,
  selected_reason TEXT, candidate_rank INTEGER, validation_status TEXT);
CREATE TABLE IF NOT EXISTS validations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT, status TEXT, sha256 TEXT, cdx_digest_match INTEGER,
  mimetype TEXT, magic_mime_match INTEGER, width INTEGER, height INTEGER,
  errors TEXT);
CREATE TABLE IF NOT EXISTS rewrites(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  page_url TEXT, attr TEXT, raw_value TEXT, status TEXT, target TEXT);
CREATE TABLE IF NOT EXISTS unresolved(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT, reason TEXT, discovered_from TEXT);
"""


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

class WaybackPrimaryPipeline:
    """End-to-end WAYBACK_PRIMARY_SITE_MODE run (discovery, CDX, storage,
    validation, rewriting, provenance, coverage)."""

    def __init__(self, cfg: dict, output: Path, run_full_flag: bool = False,
                 offline: bool = False) -> None:
        core.validate_config(cfg)
        self.cfg = cfg
        self.wb = normalize_wayback_config(cfg)
        self.output = Path(output)
        self.root = self.output.parent
        self.offline = offline or False
        self.run_full_flag = bool(run_full_flag)
        self.confirmed = self.wb.get("USER_CONFIRMED_FULL_RUN", False)
        self.dry_run = not (self.run_full_flag and self.confirmed)
        self.seed = resolve_seed(self.wb)
        self.origin = self.seed["seed_original_origin"] or ""
        self.target_ts = self.seed.get("seed_timestamp")
        self.delay = max(0.0, float(cfg.get("REQUEST_DELAY_SECONDS", 0.0)))
        self.seen_pages: set[str] = set()
        self.seen_resources: dict[str, dict] = {}
        self.local_paths: set[str] = set()
        self.captures_log: list[dict] = []
        self.provenance: list[dict] = []
        self.rewrites_log: list[dict] = []
        self.unresolved_log: list[dict] = []
        self._cdx_cache: dict[str, dict] = {}
        self._discovery_bodies: dict[str, dict] = {}
        self._validation_rows: list[dict] = []
        self._validations: dict[str, list[dict]] = {}
        self.counts = {
            "discovered_html": 0, "fetched_html": 0, "valid_html": 0,
            "discovered_media": 0, "fetched_media": 0, "valid_media": 0,
            "discovered_audio": 0, "fetched_audio": 0, "valid_audio": 0,
            "exact_timestamp": 0, "nearest_timestamp": 0, "unresolved": 0,
            "placeholder_or_corrupt": 0, "skipped_out_of_scope": 0,
            "live_fallbacks": 0, "rewritten_links": 0,
            "cdx_errors": 0, "replay_id_divergences": 0,
        }

    # -- sqlite ----------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.root / "database.sqlite")
        conn.executescript(_DDL)
        return conn

    def _in_scope(self, canonical_url: str) -> bool:
        origin = wup.origin_of(canonical_url)
        if not origin:
            return False
        if origin == self.origin:
            return True
        allowed = self.wb.get("ALLOWED_DOMAINS") or []
        return any(origin == f"http://{d}" or origin == f"https://{d}"
                   or origin.endswith(f".{d.lstrip('.')}") for d in allowed)

    def _sleep(self) -> None:
        if self.delay > 0 and not self.offline:
            import time as _time
            _time.sleep(self.delay)

    def _allowed_mimes(self) -> list[str]:
        configured = self.wb.get("ALLOWED_MIMETYPES")
        return list(configured) if configured else list(_DEFAULT_ALLOWED_MIMES)

    # -- discovery -------------------------------------------------------
    def _fetch_page_html(self, original_url: str, ts: str) -> dict:
        """Navigation fetch: plain replay at the requested timestamp."""
        replay = wayback.build_replay_url(original_url, ts, mode="")
        if self.offline:
            fixture = (self.wb.get("OFFLINE_HTML") or {}).get(original_url)
            if fixture is None:
                return {"ok": False, "body": b"", "content_type": "text/html",
                        "status": 0, "error": "offline: no html fixture", "url": replay}
            body = _hex_bytes(fixture)
            return {"ok": bool(body), "body": body,
                    "content_type": "text/html; charset=utf-8", "status": 200,
                    "error": None if body else "empty fixture", "url": replay}
        resp = _http_get(replay)
        self._sleep()
        return {"ok": resp["status"] == 200 and not resp["error"] and resp["bytes"],
                "body": resp["bytes"], "content_type": resp["content_type"],
                "status": resp["status"], "error": resp["error"], "url": replay}

    def _discover(self) -> None:
        """BFS over archived pages. Media/documents are recorded, pages are
        queued. The live origin is never touched."""
        wb, seed = self.wb, self.seed
        max_pages = int(wb.get("MAX_PAGES", 2000))
        max_depth = int(wb.get("MAX_DEPTH", 12))
        if not self.target_ts:
            raise core.ConfigError(
                "TARGET_TIMESTAMP is required: without it the requested "
                "historical moment is undefined")
        origin_parsed = urllib.parse.urlsplit(self.origin)
        queue: list[tuple[str, int]] = [
            (urllib.parse.urlsplit(seed["seed_original_url"]).path or "/", 0)]
        while queue and len(self.seen_pages) < max_pages:
            path, depth = queue.pop(0)
            page_url = urllib.parse.urlunsplit(
                (origin_parsed.scheme, origin_parsed.netloc, path, "", ""))
            if page_url in self.seen_pages:
                continue
            self.seen_pages.add(page_url)
            self.counts["discovered_html"] += 1
            if depth > max_depth:
                continue
            fetched = self._fetch_page_html(page_url, self.target_ts)
            self._discovery_bodies[page_url] = fetched
            if not fetched["ok"]:
                self.counts["unresolved"] += 1
                self.unresolved_log.append(
                    {"url": page_url, "reason": f"discovery fetch failed: {fetched['error']}",
                     "discovered_from": None})
                continue
            body = fetched["body"]
            text = body.decode(_detect_charset(fetched["content_type"], body),
                               errors="replace")
            extractor = LinkExtractor()
            try:
                extractor.feed(text)
                extractor.close()
            except Exception:  # malformed HTML never kills the crawl
                extractor.links = []
            base_href = extractor.base_href or page_url
            for raw in extractor.links:
                canonical, status = resolve_link(raw, base_href)
                if canonical is None or canonical == page_url:
                    continue
                if not self._in_scope(canonical):
                    self.counts["skipped_out_of_scope"] += 1
                    continue
                path_only = urllib.parse.urlsplit(canonical).path
                kind = _looks_like_kind(canonical)
                if kind == "html":
                    if path_only not in {urllib.parse.urlsplit(p).path
                                         for p in self.seen_pages}:
                        queue.append((path_only, depth + 1))
                else:
                    if canonical not in self.seen_resources:
                        self.seen_resources[canonical] = {
                            "discovered_from": page_url, "kind": kind}
                        self.counts["discovered_media"] += 1
        self.counts["discovered_html"] = len(self.seen_pages)

    # -- CDX + selection -------------------------------------------------
    def _select(self, url: str) -> dict:
        """CDX + capture selection with caching; a temporary CDX error is
        recorded, never treated as 'no captures'."""
        if url in self._cdx_cache:
            return self._cdx_cache[url]
        wb = self.wb
        if self.offline:
            fixture = (wb.get("OFFLINE_CDX") or {}).get(url)
            if isinstance(fixture, list):
                rows, errors = list(fixture), []
            elif isinstance(fixture, dict) and "error" in fixture:
                # explicit offline simulation of a CDX 503 / backend failure
                rows, errors = [], [str(fixture["error"])]
            elif isinstance(fixture, dict):
                rows, errors = list(fixture.get("captures", [])), []
            else:
                rows, errors = [], []
        else:
            rows, errors = wayback.query_all_captures(
                url, None, None,
                capture_limit=int(wb.get("MAX_CDX_RESULTS_PER_URL", 500)),
                output="text",
                collapse_digest=bool(wb.get("COLLAPSE_DIGEST", True)),
                filter_statuscodes=tuple(str(s) for s in (wb.get("FILTER_STATUSCODE") or ["200"])),
                allowed_mimetypes=self._allowed_mimes(),
                max_pages=int(wb.get("MAX_CDX_PAGES", wayback.MAX_CDX_PAGES)))
            self._sleep()
        capture, reason, considered = wayback.select_capture(
            rows,
            target_timestamp=self.target_ts,
            tolerance_days=float(wb.get("TIMESTAMP_TOLERANCE_DAYS", 30.0)),
            prefer_exact=bool(wb.get("PREFER_EXACT_TIMESTAMP", True)),
            allowed_mimetypes=self._allowed_mimes(),
            filter_statuscodes=tuple(str(s) for s in (wb.get("FILTER_STATUSCODE") or ["200"])))
        if errors:
            # a temporary backend failure is never 'no captures'
            capture, reason, considered = None, "cdx_error", 0
        entry = {"capture": capture, "reason": reason, "rows": rows,
                 "considered": considered, "errors": errors}
        self._cdx_cache[url] = entry
        return entry

    def _storage_fetch(self, capture: dict, original_url: str) -> dict:
        """Storage fetch via id_ (raw archived bytes, never replay HTML)."""
        replay = wayback.replay_url(capture, original_url, mode="id_")
        if self.offline:
            fixture = (self.wb.get("OFFLINE_REPLAY") or {}).get(replay)
            if fixture is None:
                return {"ok": False, "status": 0, "content_type": "", "body": b"",
                        "error": "offline: no replay fixture", "url": replay}
            body = _hex_bytes(fixture.get("body_hex", "")) if isinstance(fixture, dict) else b""
            return {"ok": bool(body), "status": fixture.get("status", 200),
                    "content_type": fixture.get("content_type", ""),
                    "body": body, "error": None if body else "empty fixture", "url": replay}
        resp = _http_get(replay)
        self._sleep()
        return {"ok": resp["status"] == 200 and not resp["error"],
                "status": resp["status"], "content_type": resp["content_type"],
                "body": resp["bytes"], "error": resp["error"], "url": replay}

    def _live_fallback(self, url: str) -> dict | None:
        """Explicit opt-in only; fallback bytes get separate provenance."""
        if not self.wb.get("ALLOW_LIVE_FALLBACK") or self.offline:
            return None
        response = core.fetch(url, policy=core.RequestPolicy.from_config(self.cfg))
        kind = _kind_of(response.get("content_type", ""))
        fetched = {"status": response.get("status") or 0,
                   "content_type": response.get("content_type", ""),
                   "body": response.get("body") or b"", "url": url}
        validation = self._validate(kind, fetched, None)
        if not validation.get("ok"):
            return None
        self.counts["live_fallbacks"] += 1
        self.counts["fetched_media"] += 1
        self.counts["valid_media"] += 1
        self.counts["unresolved"] = max(0, self.counts["unresolved"] - 1)
        return {"kind": kind, "body": fetched["body"],
                "content_type": fetched["content_type"], "validation": validation,
                "capture": None, "replay_url": None, "reason": "live_fallback",
                "source": "live_fallback"}

    # -- validation ------------------------------------------------------
    def _validate(self, kind: str, resp: dict, capture: dict | None) -> dict:
        if kind == "html" and "text/html" in resp["content_type"].lower():
            result = validate_archived_html(resp["status"], resp["content_type"], resp["body"])
            result["mime_match"] = True
        elif kind in ("css", "js"):
            result = validate_text_resource(resp["status"], resp["content_type"],
                                            resp["body"], kind)
            result["mime_match"] = True
        elif (kind == "document"
              and resp["content_type"].lower().split(";")[0].strip()
              in ("text/plain", "application/json", "application/xml",
                  "application/javascript", "text/javascript")):
            result = validate_text_resource(resp["status"], resp["content_type"],
                                            resp["body"], kind)
            result["mime_match"] = True
        else:
            result = core.validate_media(status=resp["status"],
                                         content_type=resp["content_type"],
                                         body=resp["body"])
            result["errors"] = list(result.pop("reasons", []))
        result["sha256"] = hashlib.sha256(resp["body"]).hexdigest()
        result["cdx_digest_match"] = bool(
            capture and capture.get("digest")
            and _b32_sha1(resp["body"]) == str(capture["digest"]).lower())
        return result

    def _log_validation(self, url: str, validation: dict, capture: dict | None) -> None:
        self._validations.setdefault(url, []).append(validation)
        self._validation_rows.append({
            "url": url,
            "status": "verified" if validation.get("ok") else "invalid",
            "sha256": validation.get("sha256"),
            "cdx_digest_match": int(bool(validation.get("cdx_digest_match"))),
            "mimetype": (capture or {}).get("mimetype", ""),
            "magic_mime_match": int(bool(validation.get("mime_match"))),
            "width": validation.get("width"),
            "height": validation.get("height"),
            "errors": json.dumps(validation.get("errors", []), ensure_ascii=False),
        })

    # -- storage / provenance -------------------------------------------
    def _store_raw(self, kind: str, local_path: str, body: bytes) -> Path:
        target = self.root / "raw" / kind / local_path
        target.parent.mkdir(parents=True, exist_ok=True)
        core.atomic_store(target, body)
        return target

    def _write_site(self, local: str, data: bytes) -> Path:
        target = self.root / "site" / local
        target.parent.mkdir(parents=True, exist_ok=True)
        core.atomic_store(target, data)
        return target

    def _provenance(self, *, kind: str, original_url: str, capture: dict | None,
                    resp: dict, validation: dict, local_path: str | None,
                    selected_reason: str | None, source: str = "wayback_primary") -> dict:
        rec = {
            "entity_type": kind,
            "original_url": original_url,
            "seed_timestamp": self.target_ts,
            "requested_timestamp": self.target_ts,
            "selected_capture_timestamp": capture.get("timestamp") if capture else None,
            "selected_reason": selected_reason,
            "replay_mode": "id_",
            "wayback_replay_url": resp.get("url"),
            "statuscode": str(resp["status"]) if resp and resp.get("status") is not None else None,
            "mimetype": resp.get("content_type"),
            "digest": capture.get("digest") if capture else None,
            "length": len(resp.get("body") or b""),
            "local_path": local_path,
            "validation_status": "verified" if validation.get("ok") else "invalid",
            "source": source,
        }
        self.provenance.append(rec)
        return rec

    # -- db helpers ------------------------------------------------------
    def _upsert_resource(self, conn, url, kind, local_path, capture_ts,
                         status, validation_status, cdx_error=None,
                         source="wayback_primary"):
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO resources (url, kind, local_path, capture_timestamp,"
                " status, validation_status, source, cdx_error) VALUES (?,?,?,?,?,?,?,?)",
                (url, kind, local_path, capture_ts, status, validation_status,
                 source, cdx_error))

    def _upsert_page(self, conn, url, local_path, capture_ts, status, validation_status):
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO pages (url, kind, local_path, capture_timestamp,"
                " status, validation_status, source) VALUES (?,?,?,?,?,?,?)",
                (url, "html", local_path, capture_ts, status, validation_status,
                 "wayback_primary"))

    # -- run -------------------------------------------------------------
    def run(self) -> dict:
        seed = self.seed
        for d in ("raw/html", "raw/resources", "site", "reports"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        conn = self._db()
        with conn:
            conn.execute(
                "INSERT INTO seeds (wayback_url, timestamp, replay_mode, original_url, origin)"
                " VALUES (?,?,?,?,?)",
                (seed["seed_wayback_url"], seed["seed_timestamp"],
                 seed["seed_replay_mode"], seed["seed_original_url"],
                 seed["seed_original_origin"]))
        self._discover()

        # CDX + selection for every discovered resource (always: cheap,
        # offline-safe, feeds the dry-run picture).
        for url, info in list(self.seen_resources.items()):
            entry = self._select(url)
            self._log_captures(url, entry)
            if entry["capture"] is None:
                reason = entry["reason"]
                if reason == "cdx_error":
                    self.counts["cdx_errors"] += 1
                    self._upsert_resource(conn, url, info.get("kind", "resource"),
                                          None, None, "cdx_error", "cdx_error",
                                          cdx_error=reason)
                else:
                    self.counts["unresolved"] += 1
                    self.unresolved_log.append(
                        {"url": url, "reason": reason,
                         "discovered_from": info.get("discovered_from")})
                    self._upsert_resource(conn, url, info.get("kind", "resource"),
                                          None, None, "unresolved", "unresolved")
                continue
            if entry["reason"] == "exact_timestamp":
                self.counts["exact_timestamp"] += 1
            else:
                self.counts["nearest_timestamp"] += 1
            if str((entry["capture"] or {}).get("mimetype", "")).startswith("audio/"):
                self.counts["discovered_audio"] += 1
            self._upsert_resource(conn, url, info.get("kind", "resource"), None,
                                  entry["capture"].get("timestamp"), "selected", "selected")

        # CDX + selection for every discovered page (same proximity policy;
        # keeps the dry-run picture accurate without any byte transfer).
        for url in sorted(self.seen_pages):
            entry = self._select(url)
            self._log_captures(url, entry)
            if entry["capture"] is None:
                if entry["reason"] != "cdx_error":
                    self.counts["unresolved"] += 1
                    self.unresolved_log.append(
                        {"url": url, "reason": entry["reason"], "discovered_from": None})
            elif entry["reason"] == "exact_timestamp":
                self.counts["exact_timestamp"] += 1
            else:
                self.counts["nearest_timestamp"] += 1

        if not self.dry_run:
            self._run_full(conn)
        else:
            for url in self.seen_pages:
                self._upsert_page(conn, url, None, None, "planned", "planned")

        report = self._report(conn)
        self._emit_report(report)
        conn.close()
        return report

    def _run_full(self, conn: sqlite3.Connection) -> None:
        """Storage (id_), validation, local copy, rewriting, provenance."""
        wb = self.wb
        link_map: list[dict] = []
        stored_media: list[dict] = []
        stored_pages: dict[str, dict] = {}

        # ---- phase 1: media/documents (raw storage) ---------------------
        for url in list(self.seen_resources):
            entry = self._select(url)
            if entry["capture"] is None:
                fallback = self._live_fallback(url)
                if fallback:
                    local = local_path_for(url, fallback["kind"],
                                           fallback["content_type"], self.local_paths)
                    self._store_raw("resources", local, fallback["body"])
                    stored_media.append({"url": url, "kind": fallback["kind"],
                                         "local": local, "body": fallback["body"],
                                         "capture": None,
                                         "content_type": fallback["content_type"],
                                         "validation": fallback["validation"],
                                         "reason": "live_fallback", "replay_url": None,
                                         "source": "live_fallback"})
                    link_map.append({"original_url": url, "local_path": local,
                                     "validated": True})
                    self._upsert_resource(conn, url, fallback["kind"], local, None,
                                          "verified", "verified", source="live_fallback")
                continue  # already accounted in run()
            rows = wayback.dedup_captures(
                entry["rows"], allowed_mimetypes=self._allowed_mimes(),
                filter_statuscodes=tuple(str(s) for s in (wb.get("FILTER_STATUSCODE") or ["200"])))
            candidates = wayback.rank_for_target(
                rows, target_timestamp=self.target_ts,
                tolerance_days=float(wb.get("TIMESTAMP_TOLERANCE_DAYS", 30.0)),
                prefer_exact=bool(wb.get("PREFER_EXACT_TIMESTAMP", True)))[
                :int(wb.get("MAX_CANDIDATES", 8))]
            if not candidates:
                candidates = [entry["capture"]]
            validated = False
            for rank, cand in enumerate(candidates):
                resp = self._storage_fetch(cand, url)
                kind = _kind_of(cand.get("mimetype", ""))
                validation = self._validate(kind, resp, cand)
                self._log_validation(url, validation, cand)
                if not validation.get("ok"):
                    continue
                local = local_path_for(url, "html" if kind == "html" else kind,
                                       resp["content_type"], self.local_paths)
                raw_kind = "html" if kind == "html" else "resources"
                self._store_raw(raw_kind, local, resp["body"])
                reason = entry["reason"] if rank == 0 else "nearest_validated"
                if rank > 0 and entry["reason"] == "exact_timestamp":
                    reason = "nearest_validated"
                stored_media.append({"url": url, "kind": kind, "local": local,
                                     "body": resp["body"], "capture": cand,
                                     "content_type": resp["content_type"],
                                     "validation": validation, "reason": reason,
                                     "replay_url": resp.get("url")})
                link_map.append({"original_url": url, "local_path": local, "validated": True})
                self.counts["fetched_media"] += 1
                self.counts["valid_media"] += 1
                if kind == "audio":
                    self.counts["fetched_audio"] += 1
                    self.counts["valid_audio"] += 1
                self._upsert_resource(conn, url, kind, local, cand.get("timestamp"),
                                      "verified", "verified")
                validated = True
                break
            if not validated:
                errors = [v.get("errors")
                          for v in self._validations.get(url, [])]
                self.counts["placeholder_or_corrupt"] += 1
                self.counts["unresolved"] += 1
                self.unresolved_log.append(
                    {"url": url, "reason": f"no valid id_ capture: {errors}",
                     "discovered_from": self.seen_resources[url].get("discovered_from")})
                self._upsert_resource(conn, url, "resource", None, None, "invalid", "invalid")

        # ---- phase 2: pages (raw storage) -------------------------------
        for url in list(self.seen_pages):
            entry = self._select(url)
            self._log_captures(url, entry)
            if entry["capture"] is None:
                continue
            resp = self._storage_fetch(entry["capture"], url)
            validation = self._validate("html", resp, entry["capture"])
            self._log_validation(url, validation, entry["capture"])
            if not validation.get("ok"):
                self.counts["unresolved"] += 1
                self.unresolved_log.append(
                    {"url": url, "reason": f"invalid id_ html: {validation['errors']}",
                     "discovered_from": None})
                self._upsert_page(conn, url, None, None, "invalid", "invalid")
                continue
            discovery = self._discovery_bodies.get(url)
            if discovery and discovery.get("ok") and discovery["body"] != resp["body"]:
                self.counts["replay_id_divergences"] += 1
            local = local_path_for(url, "html", resp["content_type"], self.local_paths)
            if wb.get("STORE_RAW_HTML", True):
                self._store_raw("html", local, resp["body"])
            stored_pages[url] = {
                "body": resp["body"], "local": local, "capture": entry["capture"],
                "content_type": resp["content_type"], "validation": validation,
                "reason": entry["reason"], "replay_url": resp.get("url")}
            link_map.append({"original_url": url, "local_path": local, "validated": True})
            self.counts["fetched_html"] += 1
            self.counts["valid_html"] += 1
            self._upsert_page(conn, url, local, entry["capture"].get("timestamp"),
                              "verified", "verified")

        # ---- phase 3: rewriting against the complete link map -----------
        rewriter = LinkRewriter(link_map)
        for rec in stored_media:
            if rec["kind"] == "css":
                text = rec["body"].decode("latin-1", errors="replace")
                rewritten, ops = rewriter.rewrite_css(text, rec["url"], anchor_path=rec["local"])
                self._write_site(rec["local"], rewritten.encode("utf-8"))
                self.counts["rewritten_links"] += sum(
                    1 for op in ops if op["op_status"] == "rewritten")
                for op in ops:
                    self.rewrites_log.append({"page_url": rec["url"], **op})
            else:
                self._write_site(rec["local"], rec["body"])
            self._provenance(kind=rec["kind"], original_url=rec["url"],
                             capture=rec["capture"],
                             resp={"url": rec["replay_url"], "status": 200,
                                   "content_type": rec["content_type"],
                                   "body": rec["body"]},
                             validation=rec["validation"], local_path=rec["local"],
                             selected_reason=rec["reason"],
                             source=rec.get("source", "wayback_primary"))

        for url, rec in stored_pages.items():
            charset = _detect_charset(rec["content_type"], rec["body"])
            text = rec["body"].decode(charset, errors="replace")
            rewritten, ops = rewriter.rewrite_html(text, url, anchor_path=rec["local"])
            self._write_site(rec["local"], rewritten.encode("utf-8"))
            self.counts["rewritten_links"] += sum(
                1 for op in ops if op["op_status"] == "rewritten")
            for op in ops:
                self.rewrites_log.append({"page_url": url, **op})
            self._provenance(kind="html", original_url=url, capture=rec["capture"],
                             resp={"url": rec["replay_url"], "status": 200,
                                   "content_type": rec["content_type"],
                                   "body": rec["body"]},
                             validation=rec["validation"], local_path=rec["local"],
                             selected_reason=rec["reason"])

    # -- logging / reporting --------------------------------------------
    def _log_captures(self, url: str, entry: dict) -> None:
        rows = entry["rows"]
        selected = entry["capture"]
        for rank, row in enumerate(rows):
            is_sel = row is selected
            self.captures_log.append({
                "original_url": url,
                "requested_timestamp": self.target_ts,
                "capture_timestamp": row.get("timestamp"),
                "replay_mode": "id_",
                "wayback_url": (wayback.replay_url(row, url, mode="id_")
                                if row.get("timestamp") else None),
                "statuscode": row.get("statuscode"),
                "mimetype": row.get("mimetype"),
                "digest": row.get("digest"),
                "length": row.get("length"),
                "selected_reason": entry["reason"] if is_sel else None,
                "candidate_rank": rank if is_sel else None,
                "validation_status": None,
            })

    def _report(self, conn: sqlite3.Connection) -> dict:
        with conn:
            for cap in self.captures_log:
                conn.execute(
                    "INSERT INTO captures (url, requested_timestamp, capture_timestamp,"
                    " replay_mode, wayback_url, statuscode, mimetype, digest, length,"
                    " selected_reason, candidate_rank, validation_status)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cap["original_url"], cap["requested_timestamp"],
                     cap["capture_timestamp"], cap["replay_mode"], cap["wayback_url"],
                     cap["statuscode"], cap["mimetype"], cap["digest"], cap["length"],
                     cap["selected_reason"], cap["candidate_rank"],
                     cap["validation_status"]))
            for row in self._validation_rows:
                conn.execute(
                    "INSERT INTO validations (url, status, sha256, cdx_digest_match,"
                    " mimetype, magic_mime_match, width, height, errors)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    tuple(row[k] for k in ("url", "status", "sha256", "cdx_digest_match",
                                           "mimetype", "magic_mime_match", "width",
                                           "height", "errors")))
            for op in self.rewrites_log:
                conn.execute(
                    "INSERT INTO rewrites (page_url, attr, raw_value, status, target)"
                    " VALUES (?,?,?,?,?)",
                    (op["page_url"], op.get("attr"), op.get("attr_value"),
                     op.get("op_status"), op.get("target")))
            for unres in self.unresolved_log:
                conn.execute(
                    "INSERT INTO unresolved (url, reason, discovered_from) VALUES (?,?,?)",
                    (unres["url"], unres.get("reason"), unres.get("discovered_from")))
        self._write_jsonl("provenance.jsonl", self.provenance)
        self._write_jsonl("captures.jsonl", self.captures_log)
        self._write_jsonl("rewrites.jsonl", self.rewrites_log)
        self._write_jsonl("unresolved.jsonl", self.unresolved_log)

        counts = dict(self.counts)
        block = {
            "enabled": True,
            "seed_timestamp": self.target_ts,
            "seed_original_url": self.seed["seed_original_url"],
            "seed_original_origin": self.origin,
            "seed_replay_mode": self.seed["seed_replay_mode"],
            "allow_live_fallback": bool(self.wb.get("ALLOW_LIVE_FALLBACK", False)),
            "discovered_html": counts["discovered_html"],
            "fetched_html": counts["fetched_html"],
            "valid_html": counts["valid_html"],
            "discovered_media": counts["discovered_media"],
            "fetched_media": counts["fetched_media"],
            "valid_media": counts["valid_media"],
            "discovered_audio": counts["discovered_audio"],
            "fetched_audio": counts["fetched_audio"],
            "valid_audio": counts["valid_audio"],
            "exact_timestamp": counts["exact_timestamp"],
            "nearest_timestamp": counts["nearest_timestamp"],
            "unresolved": counts["unresolved"],
            "placeholder_or_corrupt": counts["placeholder_or_corrupt"],
            "skipped_out_of_scope": counts["skipped_out_of_scope"],
            "live_fallbacks": counts["live_fallbacks"],
            "rewritten_links": counts["rewritten_links"],
            "cdx_errors": counts["cdx_errors"],
            "replay_id_divergences": counts["replay_id_divergences"],
        }
        fetched = counts["fetched_html"] + counts["fetched_media"]
        valid = counts["valid_html"] + counts["valid_media"]
        discovered = counts["discovered_html"] + counts["discovered_media"]
        coverage = round(valid / discovered, 4) if discovered else 0.0
        report = {
            "stage": "test_report",
            "source_mode": "wayback_primary",
            "dry_run": self.dry_run,
            "gate": "stop" if self.dry_run else "proceed",
            "gate_reason": ("full run requires USER_CONFIRMED_FULL_RUN=true and --run-full"
                            if self.dry_run else "confirmed full archive run"),
            "confirmed": self.confirmed,
            "run_full_flag": self.run_full_flag,
            "coverage": coverage,
            "recovery_required": False,
            "media_invalid": counts["placeholder_or_corrupt"],
            "media_recovered": 0,
            "recovery_queue_size": 0,
            "valid_media_ratio": (counts["valid_media"] / counts["fetched_media"]
                                  if counts["fetched_media"] else 0.0),
            "wayback_primary": block,
        }
        manifest = {
            "source_mode": "wayback_primary",
            "seed": seed_snapshot(self.seed),
            "entity_timestamp": datetime.now(timezone.utc).isoformat(),
            "entities": {
                "seeds": 1,
                "pages": len(self.seen_pages),
                "resources": len(self.seen_resources),
                "captures": len(self.captures_log),
                "validations": len(self._validation_rows),
                "rewrites": len(self.rewrites_log),
                "unresolved": len(self.unresolved_log),
            },
            "files": {
                "provenance": "provenance.jsonl",
                "captures": "captures.jsonl",
                "rewrites": "rewrites.jsonl",
                "unresolved": "unresolved.jsonl",
                "database": "database.sqlite",
                "site": "site/",
                "raw": "raw/",
            },
        }
        (self.root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.root / "reports" / "coverage.json").write_text(
            json.dumps(block, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.root / "summary.json").write_text(
            json.dumps({"source_mode": "wayback_primary", "coverage": coverage,
                        "gate": report["gate"], "wayback_primary": block},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def _emit_report(self, report: dict) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    def _write_jsonl(self, name: str, rows: list[dict]) -> None:
        with (self.root / name).open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _kind_of(mimetype: str) -> str:
    mime = (mimetype or "").split(";")[0].strip().lower()
    if mime == "text/html":
        return "html"
    if mime == "text/css":
        return "css"
    if mime in ("application/javascript", "text/javascript"):
        return "js"
    if mime == "application/pdf":
        return "document"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def _looks_like_kind(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    lower = path.lower().rsplit("/", 1)[-1]
    if "." not in lower:
        return "html"
    if lower.endswith((".html", ".htm", ".php", ".asp", ".aspx", ".shtml")):
        return "html"
    return "resource"


def _hex_bytes(value) -> bytes:
    try:
        return bytes.fromhex(value)
    except (TypeError, ValueError):
        return b""


def seed_snapshot(seed: dict) -> dict:
    out = {k: seed[k] for k in ("seed_wayback_url", "seed_timestamp",
                                "seed_replay_mode", "seed_original_url",
                                "seed_original_origin") if k in seed}
    out.setdefault("seed_timestamp", None)
    return out


def run_from_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WAYBACK_PRIMARY_SITE_MODE pipeline")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-full", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    cfg = core.load_config(Path(args.config))
    pipeline = WaybackPrimaryPipeline(cfg, Path(args.output),
                                      run_full_flag=args.run_full,
                                      offline=args.offline)
    pipeline.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_from_cli())
