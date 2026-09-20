#!/usr/bin/env python3
"""
archivist_core.py — shared deterministic layer for the archivist skills.

Single source of truth for the crypto/network/safety primitives that both
skills rely on, so basic and forum pipelines never drift apart:

  * URL normalization, canonicalization, same-domain and path-scope checks
  * SSRF guard: no userinfo, no private/loopback/link-local/metadata targets,
    host re-validation after every redirect
  * robots.txt policy via urllib.robotparser, fetched from the origin root
    (never joined onto a deep target path); fail-closed on unavailable robots
  * RequestPolicy + per-host scheduler: configured delay is enforced between
    every two requests to the same host, not just on retry
  * fetch() with retry on 408/429/500/502/503/504, byte budget, redirect
    budget, and a recorded redirect chain
  * content classification from HTTP headers AND magic bytes (html / media /
    document / unknown) — live discovery without a prepared manifest
  * honest SHA-256 over the exact response bytes
  * atomic file store (temp file, fsync, rename, reread, hash re-verify)
  * media validation state machine (fetched -> stored -> signature_verified ->
    decoder_verified), with structural decoders for PNG (chunks + CRC + IEND),
    GIF (header + trailer), JPEG (marker chain + SOF), WebP, PDF and ZIP —
    truncated or placeholder bodies never pass
  * placeholder registry: known stub signatures and zero-content bodies
  * config validation with sane ranges and byte/redirect budgets

Stdlib only. No third-party imports.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from pathlib import Path

URL_SCHEMES = {"http", "https"}
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
PAGE_MIMES = {"text/html", "application/xhtml+xml"}
DEFAULT_USER_AGENT = "trehgranka-archivist/1.0 (+non-commercial research)"

# magic bytes per content type: (signature, need) — GIF checked in full below
MAGIC_BYTES: dict[str, tuple[bytes, int]] = {
    "image/jpeg": (b"\xff\xd8\xff", 3),
    "image/png": (b"\x89PNG\r\n\x1a\n", 8),
    "image/gif": (b"GIF8", 4),
    "image/webp": (b"RIFF", 4),
    "image/x-icon": (b"\x00\x00\x01\x00", 4),
    "application/pdf": (b"%PDF-", 5),
    "application/zip": (b"PK", 2),
}

DECODABLE = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# resource ceilings so a hostile file cannot exhaust memory (C3)
MAX_PNG_CHUNKS = 1_000_000
MAX_PNG_CHUNK_BYTES = 256 * 1024 * 1024
MAX_JPEG_MARKERS = 200_000

# well-known 1x1 / stub images, by signature prefix
PLACEHOLDER_PREFIXES: tuple[bytes, ...] = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00",  # GIF89a 1x1 (transparent pixel)
    b"\x47\x49\x46\x38\x37\x61\x01\x00\x01\x00",  # GIF87a 1x1
)


class ConfigError(ValueError):
    """Raised when the project configuration is malformed or self-contradictory."""


# --------------------------------------------------------------------------
# URL policy
# --------------------------------------------------------------------------

def normalize_url(url: str) -> str | None:
    """Validate a URL and return it without the fragment; None if unusable."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in URL_SCHEMES or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def canonical_url(url: str) -> str | None:
    """Normalize for dedup: drop default ports, sort query params, drop fragment."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in URL_SCHEMES or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    if (parsed.scheme == "http" and host.endswith(":80") and len(host) > 3 and host[-3:] == ":80"):
        host = host[:-3]
    if parsed.scheme == "https" and host.endswith(":443"):
        host = host[:-4]
    query = parsed.query
    if query:
        params = sorted(q for q in query.split("&") if q)
        query = "&".join(params)
    return urllib.parse.urlunparse(parsed._replace(netloc=host, query=query, fragment=""))


def origin_root(url: str) -> str:
    """scheme://host/ — the only place robots.txt lives."""
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def same_domain(url: str, allowed: list[str]) -> bool:
    host = (urllib.parse.urlparse(url).hostname or urllib.parse.urlparse(url).netloc).lower()
    return any(host == str(d).lower() or host.endswith("." + str(d).lower()) for d in allowed)


def path_in_scope(url: str, scope_prefixes: list[str] | None) -> bool:
    """Path-scope boundary check. None/empty prefixes means site-wide allowed."""
    if not scope_prefixes:
        return True
    path = urllib.parse.urlparse(url).path or "/"
    for prefix in scope_prefixes:
        if prefix == "/" or path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def resolve_url(base_url: str, raw: str, base_href: str | None = None) -> str | None:
    """Resolve a possibly-relative href against the document's real base."""
    if not raw or raw.startswith(("javascript:", "data:", "mailto:", "tel:", "blob:")):
        return None
    doc_base = base_href or base_url
    try:
        absolute = urllib.parse.urljoin(doc_base, raw.strip())
    except ValueError:
        return None
    return normalize_url(absolute)


def _is_public_host(hostname: str) -> tuple[bool, str]:
    """Resolve a hostname and reject non-public addresses (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        return False, f"dns resolution failed: {exc}"
    if not infos:
        return False, "no addresses resolved"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if info[0] == socket.AF_INET6 and ip.is_unspecified:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast \
                or ip.is_reserved or ip.is_unspecified:
            return False, f"non-public address {ip}"
    return True, "ok"


def assert_public_target(url: str) -> None:
    """Raise ConfigError unless the host resolves to public addresses only."""
    host = urllib.parse.urlparse(url).netloc
    if not host:
        raise ConfigError(f"target has no host: {url!r}")
    host = host.split("@")[-1]  # strip userinfo defensively (normalize_url already vetoes it)
    allowed, note = _is_public_host(host)
    if not allowed:
        raise ConfigError(f"target must be a public address: {url!r} ({note})")


# --------------------------------------------------------------------------
# request policy and per-host scheduling
# --------------------------------------------------------------------------

@dataclass
class RequestPolicy:
    """Everything that governs a single fetch, assembled from config."""

    user_agent: str = DEFAULT_USER_AGENT
    delay_seconds: float = 1.0
    retry_count: int = 2
    timeout_seconds: float = 20.0
    max_response_bytes: int = 60 * 1024 * 1024
    max_redirects: int = 5
    respect_robots: bool = True
    allowed_domains: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "user_agent": self.user_agent,
            "delay_seconds": self.delay_seconds,
            "retry_count": self.retry_count,
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "max_redirects": self.max_redirects,
            "respect_robots": self.respect_robots,
            "allowed_domains": list(self.allowed_domains),
        }

    @classmethod
    def from_config(cls, cfg: dict) -> "RequestPolicy":
        return cls(
            user_agent=str(cfg.get("USER_AGENT", DEFAULT_USER_AGENT)),
            delay_seconds=float(cfg.get("REQUEST_DELAY_SECONDS", 1.0)),
            retry_count=int(cfg.get("RETRY_COUNT", 2)),
            timeout_seconds=float(cfg.get("TIMEOUT_SECONDS", 20.0)),
            max_response_bytes=int(cfg.get("MAX_RESPONSE_BYTES", 60 * 1024 * 1024)),
            max_redirects=int(cfg.get("MAX_REDIRECTS", 5)),
            respect_robots=bool(cfg.get("RESPECT_ROBOTS_TXT", True)),
            allowed_domains=list(cfg.get("ALLOWED_DOMAINS", [])),
        )


class HostScheduler:
    """Per-host delay enforcement: no two requests to the same host within
    `delay_seconds`. Wall-clock based; safe for both sync and threaded use."""

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = max(0.0, float(delay_seconds))
        self._next: dict[str, float] = {}

    def _host(self, url: str) -> str:
        return urllib.parse.urlparse(url).netloc.lower() or "?"

    def wait(self, url: str) -> None:
        """Block until this host may be requested again."""
        if self.delay_seconds <= 0:
            return
        host = self._host(url)
        until = self._next.get(host, 0.0)
        remaining = until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self._next[host] = time.monotonic() + self.delay_seconds


# --------------------------------------------------------------------------
# robots.txt policy
# --------------------------------------------------------------------------

def fetch_robots_policy(target_url: str, *, user_agent: str = DEFAULT_USER_AGENT,
                        timeout_seconds: float = 15.0,
                        max_response_bytes: int = 1_000_000) -> dict:
    """Fetch and parse robots.txt from the origin root (fail-closed usage).

    Returns a dict: {"url", "status", "parser"|"error", "allows_wildcard"}.
    The caller must treat status != "ok" per policy: with RESPECT_ROBOTS_TXT
    the crawl is blocked (fail-closed), it is never silently allowed.
    """
    robots_url = origin_root(target_url) + "robots.txt"
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        resp = opener.open(req, timeout=timeout_seconds)
        body = resp.read(max_response_bytes + 1)
        truncated = len(body) > max_response_bytes
        body = body[:max_response_bytes]
        if truncated:
            return {"url": robots_url, "status": "error", "error": "robots.txt too large", "parser": None}
        text = body.decode("utf-8", errors="replace")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(text.splitlines())
        wildcard_allowed = any(
            line.strip().lower().startswith("allow:")
            and "*" in line
            for line in text.splitlines()
            if line.strip()
        )
        return {"url": robots_url, "status": "ok", "parser": parser, "allows_wildcard": wildcard_allowed}
    except Exception as exc:  # noqa: BLE001 — recorded, caller decides per policy
        return {"url": robots_url, "status": "error", "error": f"{type(exc).__name__}: {exc}", "parser": None}


def robots_allows(url: str, robots: dict, user_agent: str = DEFAULT_USER_AGENT) -> bool:
    """True when robots exists and permits fetching url.

    Fail-closed: robots missing/errored is NOT 'allowed' — the policy layer
    decides (block live crawl) rather than assuming permission.
    """
    parser = robots.get("parser")
    if parser is None:
        return False
    try:
        return bool(parser.can_fetch(user_agent, url))
    except Exception:  # noqa: BLE001 — malformed rules must not open the gate
        return False


# --------------------------------------------------------------------------
# content classification and media validation
# --------------------------------------------------------------------------

def detect_mime(body: bytes) -> str | None:
    """Map magic bytes to a content type; None when unrecognized."""
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    if body.startswith(b"%PDF-"):
        return "application/pdf"
    if body.startswith(b"PK\x03\x04") or body.startswith(b"PK\x05\x06"):
        return "application/zip"
    if body.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if re.match(rb"^\s*<(?:!doctype\s+)?html", body[:512], re.IGNORECASE):
        return "text/html"
    return None


def classify_content(declared: str, body: bytes) -> tuple[str, str]:
    """(kind, detected_mime) from HTTP Content-Type and magic bytes.

    kind: "html" | "media" | "document" | "unknown".
    The declared header is advisory; magic bytes decide for media formats.
    """
    detected = detect_mime(body)
    declared_norm = (declared or "").split(";")[0].strip().lower()
    if detected in PAGE_MIMES or detected == "text/html":
        return "html", detected or "text/html"
    if detected in DECODABLE or detected in ("image/x-icon",):
        return "media", detected
    if detected in ("application/pdf", "application/zip"):
        return "document", detected
    if detected:
        # recognized magic but not a handled archive/image family
        return "unknown", detected
    if declared_norm.startswith("text/html") or declared_norm in PAGE_MIMES:
        return "html", "text/html"
    if declared_norm.startswith("image/"):
        return "media", declared_norm
    if declared_norm in ("application/pdf", "application/zip"):
        return "document", declared_norm
    return "unknown", declared_norm or "unknown"


def _mime_matches(declared: str, detected: str) -> bool:
    """Magic-bytes claim vs declared Content-Type; mismatches degrade trust."""
    if not detected:
        return False
    declared_norm = (declared or "").split(";")[0].strip().lower()
    if not declared_norm or declared_norm == "application/octet-stream":
        return True  # generic containers defer to magic
    return declared_norm == detected


def _validate_png(body: bytes, reasons: list[str]) -> bool:
    """Structural PNG validation: chunk framing + CRC32 + IEND terminal."""
    if len(body) < 33:  # signature(8) + IHDR chunk(25)
        reasons.append("png too short for IHDR")
        return False
    pos = 8
    chunks = 0
    ihdr_seen = False
    while pos + 12 <= len(body):
        length = int.from_bytes(body[pos:pos + 4], "big")
        ctype = body[pos + 4:pos + 8]
        data_start = pos + 8
        data_end = data_start + length
        if data_end + 4 > len(body):
            reasons.append(f"png chunk {ctype!r} truncated")
            return False
        crc = int.from_bytes(body[data_end:data_end + 4], "big")
        computed = zlib.crc32(body[pos + 4:data_end]) & 0xFFFFFFFF
        if computed != crc:
            reasons.append(f"png chunk {ctype!r} crc mismatch")
            return False
        chunks += 1
        if chunks > MAX_PNG_CHUNKS or length > MAX_PNG_CHUNK_BYTES:
            reasons.append("png resource ceiling exceeded")
            return False
        if ctype == b"IHDR":
            if ihdr_seen or pos != 8:
                reasons.append("png IHDR not first")
                return False
            ihdr_seen = True
        if ctype == b"IEND":
            if data_end + 4 != len(body):
                reasons.append("png trailing bytes after IEND")
                return False
            reasons.clear()
            reasons.append("decoder_verified png chunks+crc+iend")
            return True
        pos = data_end + 4
    reasons.append("png missing IEND")
    return False


def _validate_gif(body: bytes, reasons: list[str]) -> bool:
    """GIF header (87a/89a) + logical screen descriptor + terminal trailer."""
    if body[:6] not in (b"GIF87a", b"GIF89a"):
        reasons.append("gif bad header")
        return False
    if len(body) < 13:  # header + logical screen descriptor
        reasons.append("gif missing logical screen descriptor")
        return False
    if not body.endswith(b"\x3b"):
        reasons.append("gif missing trailer 0x3B")
        return False
    reasons.clear()
    reasons.append("decoder_verified gif frame+trailer")
    return True


def _validate_jpeg(body: bytes, reasons: list[str]) -> bool:
    """Marker-chain validation: SOI, framed markers, at least one SOF, EOI
    (entropy-coded data after SOS is scanned for 0xFF 0xD9 with 0xFF 0x00
    stuffing, per JFIF)."""
    if not body.startswith(b"\xff\xd8"):
        reasons.append("jpeg missing SOI")
        return False
    pos, n = 2, len(body)
    sof_seen = marker_count = 0
    in_entropy = False
    while pos < n:
        if body[pos] != 0xFF:
            if in_entropy:
                pos += 1  # entropy-coded data bytes are arbitrary
                continue
            reasons.append(f"jpeg stray byte at {pos}")
            return False
        while pos < n and body[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= n:
            reasons.append("jpeg truncated marker")
            return False
        marker = body[pos]
        pos += 1
        if marker == 0x00:  # stuffed data byte
            continue
        if marker == 0xD9:  # EOI
            if not sof_seen:
                reasons.append("jpeg EOI before any SOF")
                return False
            reasons.clear()
            reasons.append("decoder_verified jpeg marker chain + sof")
            return True
        if marker in (0xD8, 0x01):  # SOI, TEM — no length
            continue
        if marker == 0xDA:  # SOS — entropy-coded segment follows
            if pos + 1 >= n:
                reasons.append("jpeg truncated SOS")
                return False
            length = int.from_bytes(body[pos:pos + 2], "big")
            if length < 2 or pos + length > n:
                reasons.append("jpeg bad SOS length")
                return False
            marker_count += 1
            pos += length
            in_entropy = True
            continue
        in_entropy = False
        if pos + 1 >= n:
            reasons.append("jpeg truncated marker length")
            return False
        length = int.from_bytes(body[pos:pos + 2], "big")
        if length < 2 or pos + length > n:
            reasons.append(f"jpeg bad marker length {length} at {pos - 2}")
            return False
        marker_count += 1
        if marker_count > MAX_JPEG_MARKERS:
            reasons.append("jpeg marker ceiling exceeded")
            return False
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            sof_seen += 1
        pos += length
    reasons.append("jpeg missing EOI")
    return False


def _validate_webp(body: bytes, reasons: list[str]) -> bool:
    if not body.startswith(b"RIFF") or len(body) < 20 or body[8:12] != b"WEBP":
        reasons.append("webp bad RIFF frame")
        return False
    declared_size = int.from_bytes(body[4:8], "little")
    if declared_size != len(body) - 8:
        reasons.append("webp size mismatch")
        return False
    chunk_fourcc = body[12:16]
    if chunk_fourcc not in (b"VP8 ", b"VP8L", b"VP8X"):
        reasons.append(f"webp unknown chunk {chunk_fourcc!r}")
        return False
    reasons.clear()
    reasons.append("decoder_verified webp riff frame")
    return True


def _validate_pdf(body: bytes, reasons: list[str]) -> bool:
    if not body.lstrip().startswith(b"%PDF-"):
        reasons.append("pdf missing %PDF- header")
        return False
    tail = body[-2048:]
    if b"%%EOF" not in tail:
        reasons.append("pdf missing %%EOF")
        return False
    if b"trailer" not in tail and b"xref" not in tail:
        reasons.append("pdf missing trailer/xref")
        return False
    reasons.clear()
    reasons.append("decoder_verified pdf header+trailer+eof")
    return True


def _validate_zip(body: bytes, reasons: list[str]) -> bool:
    import zipfile
    import io
    if not (body.startswith(b"PK\x03\x04") or body.startswith(b"PK\x05\x06")):
        reasons.append("zip missing PK signature")
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            bad = zf.testzip()
            if bad is not None:
                reasons.append(f"zip member {bad!r} failed crc check")
                return False
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        reasons.append(f"zip unreadable: {exc.__class__.__name__}")
        return False
    reasons.clear()
    reasons.append("decoder_verified zip members")
    return True


# format -> structural validator (stdlib-only, no third-party decode)
MEDIA_VALIDATORS: dict[str, callable] = {
    "image/jpeg": _validate_jpeg,
    "image/png": _validate_png,
    "image/gif": _validate_gif,
    "image/webp": _validate_webp,
}


def is_placeholder_body(body: bytes) -> bool:
    """Known stub / spacer signatures: 1x1 GIF/PNG and all-zeros micro blobs."""
    if body.startswith(PLACEHOLDER_PREFIXES):
        return True
    if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 33:
        # IHDR width/height both 1 -> the ubiquitous 1x1 spacer PNG
        if body[16:20] == b"\x00\x00\x00\x01" and body[20:24] == b"\x00\x00\x00\x01":
            return True
    return len(body) < 80 and not body.strip(b"\x00")


def validate_media(*, status, content_type, body, role: str = "unknown") -> dict:
    """Deterministic media/state validation.

    Returns {ok, state, reasons, sha256, mime, detected, placeholder, size}.
    state values: fetched -> invalid (with reasons) | verified (decoder).
    """
    reasons: list[str] = []
    sha = sha256_bytes(body)
    detected = ""
    if isinstance(status, str):
        if status != "200":
            reasons.append(f"http {status}")
        status = None
    if status in (401, 403, 404, 410, 429, 451, 500, 502, 503, 504):
        reasons.append(f"http {status}")
        state = "invalid"
    elif not body:
        reasons.append("empty body")
        state = "invalid"
    elif is_placeholder_body(body):
        reasons.append("placeholder stub body")
        state = "invalid"
    else:
        kind, detected = classify_content(content_type, body)
        if kind == "html":
            reasons.append("html placeholder instead of media")
            state = "invalid"
        elif kind == "media":
            validator = MEDIA_VALIDATORS.get(detected)
            if detected == "image/x-icon":
                ok = len(body) >= 6
            elif validator is None:
                reasons.append(f"unhandled image type {detected}")
                ok = False
            else:
                ok = validator(body, reasons)
            if ok and not _mime_matches(content_type, detected):
                reasons.append("content-type mismatch with magic")
            state = "verified" if ok else "invalid"
        elif kind == "document":
            if detected == "application/pdf":
                ok = _validate_pdf(body, reasons)
            elif detected == "application/zip":
                ok = _validate_zip(body, reasons)
            else:
                reasons.append(f"unhandled document type {detected}")
                ok = False
            state = "verified" if ok else "invalid"
        else:
            # unknown octet-stream: never verified without a recognized magic
            reasons.append(f"unknown content type {content_type!r} without recognized magic")
            state = "invalid"
    return {
        "ok": state == "verified",
        "state": state,
        "reasons": reasons,
        "sha256": sha,
        "mime": detected if detected else _declared_mime(content_type),
        "detected": detected,
        "placeholder": is_placeholder_body(body),
        "size": len(body),
    }


def _declared_mime(content_type: str) -> str:
    return (content_type or "").split(";")[0].strip().lower() or "application/octet-stream"


# --------------------------------------------------------------------------
# hashing and storage
# --------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_store(target: Path, data: bytes) -> Path:
    """Write via temp file in the same directory, fsync, rename, reread, verify.

    Prevents partial files and verifies the stored bytes hash before returning."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    stored = target.read_bytes()
    if sha256_bytes(stored) != sha256_bytes(data):
        raise IOError(f"atomic store verification mismatch for {target}")
    return target


# --------------------------------------------------------------------------
# fetch (with retry, redirect chain, budgets)
# --------------------------------------------------------------------------

class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Record every redirect hop and re-validate the new host on each.

    urllib's built-in handler follows redirects invisibly; this one surfaces
    the full chain to the pipieline and makes any hop to a non-public host
    fail the whole fetch (SSRF re-check after *every* redirect)."""

    def __init__(self, chain: list[dict], max_redirects: int) -> None:
        super().__init__()
        self._chain = chain
        self._max = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urllib.parse.urljoin(req.full_url, newurl)
        try:
            assert_public_target(target)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"redirect blocked (ssrf re-check): {exc}") from exc
        if len(self._chain) >= self._max:
            raise ValueError(f"redirect chain exceeds {self._max} hops")
        self._chain.append({"from": req.full_url, "to": target, "status": code})
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch(url: str, *, policy: RequestPolicy | None = None,
          scheduler: HostScheduler | None = None) -> dict:
    """Fetch one URL with the full policy applied.

    Returns a dict: requested_url, final_url, status, headers, body (bytes),
    truncated, redirect_chain (list of {"from","to","status"}), error,
    policy_effective, content_type, detected_type, kind.
    """
    policy = policy or RequestPolicy()
    scheduler = scheduler or HostScheduler(policy.delay_seconds)
    chain: list[dict] = []
    current = url
    final_url = url
    status: int | None = None
    headers: dict = {}
    body = b""
    truncated = False
    error: str | None = None

    last_error: Exception | None = None
    for attempt in range(policy.retry_count + 1):
        if attempt:
            scheduler.wait(url)  # delay before retry is fine (per-host too)
        try:
            scheduler.wait(current)
            opener = urllib.request.build_opener(_GuardedRedirectHandler(chain, policy.max_redirects))
            req = urllib.request.Request(current, headers={"User-Agent": policy.user_agent})
            resp = opener.open(req, timeout=policy.timeout_seconds)  # noqa: S310
            final_url = resp.url
            status = resp.status
            headers = dict(resp.headers.items())
            chunk = resp.read(policy.max_response_bytes + 1)
            if len(chunk) > policy.max_response_bytes:
                truncated = True
                chunk = chunk[:policy.max_response_bytes]
            body = chunk
            break
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers.items()) if exc.headers else {}
            if status in RETRYABLE_STATUSES:
                last_error = exc
                time.sleep(min(policy.delay_seconds * (attempt + 1), 10.0))
                continue
            error = f"http {status}"
            break
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = exc
            if attempt < policy.retry_count:
                time.sleep(min(policy.delay_seconds * (attempt + 1), 10.0))
                continue
            error = f"{type(exc).__name__}: {exc}"
            break

    if error is None and status is None and not body:
        error = "no response"
    declared = headers.get("Content-Type", "")
    kind, detected = classify_content(declared, body)
    return {
        "requested_url": url,
        "final_url": final_url,
        "status": status,
        "headers": headers,
        "body": body,
        "bytes_read": len(body),
        "truncated": truncated,
        "redirect_chain": chain,
        "error": error,
        "policy_effective": policy.to_dict(),
        "content_type": declared,
        "detected_type": detected,
        "kind": kind,
        "sha256": sha256_bytes(body) if body else "",
    }


# --------------------------------------------------------------------------
# config validation and loading
# --------------------------------------------------------------------------

def validate_config(cfg: dict, offline: bool = False) -> None:
    """Strict config validation: types, ranges, budgets, self-consistency.

    Public-address (SSRF) DNS checks are skipped in offline mode: fixtures use
    non-resolving example.org hosts. URL format checks always run."""
    target = cfg.get("TARGET_URL")
    if not isinstance(target, str) or not target.strip():
        raise ConfigError("TARGET_URL must be a non-empty URL")
    if not normalize_url(target):
        raise ConfigError(f"TARGET_URL is not a valid http(s) URL: {target!r}")
    if not offline and not cfg.get("TEST_ALLOW_LOOPBACK"):
        assert_public_target(target)

    for key, lo, hi, label in (
        ("REQUEST_DELAY_SECONDS", 0.0, 3600.0, "seconds"),
        ("TIMEOUT_SECONDS", 1.0, 300.0, "seconds"),
    ):
        val = cfg.get(key)
        if val is None:
            continue
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not (lo <= float(val) <= hi):
            raise ConfigError(f"{key} must be within [{lo}, {hi}] {label}, got {val!r}")

    for key, lo, hi in (
        ("RETRY_COUNT", 0, 10),
        ("MAX_REDIRECTS", 0, 20),
        ("MAX_PAGES", 1, 100_000),
        ("MAX_DEPTH", 1, 30),
        ("MAX_RESPONSE_BYTES", 64 * 1024, 1024 * 1024 * 1024),
    ):
        val = cfg.get(key)
        if val is None:
            continue
        if not isinstance(val, int) or isinstance(val, bool) or not (lo <= val <= hi):
            raise ConfigError(f"{key} must be an int within [{lo}, {hi}], got {val!r}")

    domain = cfg.get("ALLOWED_DOMAINS")
    if domain is not None:
        if not isinstance(domain, list) or not all(isinstance(d, str) and d.strip() for d in domain):
            raise ConfigError("ALLOWED_DOMAINS must be a list of domain strings")

    for key in ("DOWNLOAD_ORIGINALS", "DOWNLOAD_ATTACHMENTS", "SAVE_RAW_HTML", "RESPECT_ROBOTS_TXT"):
        val = cfg.get(key)
        if val is not None and not isinstance(val, bool):
            raise ConfigError(f"{key} must be true/false")

    confirm = cfg.get("USER_CONFIRMED_FULL_RUN", False)
    if not isinstance(confirm, bool):
        raise ConfigError("USER_CONFIRMED_FULL_RUN must be true/false")


# --------------------------------------------------------------------------
# generic config load (JSON or the skill's flat YAML subset)
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON: {exc}") from exc
    return _parse_simple_yaml(text)


def _scalar(token: str):
    if not token:
        return token
    low = token.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    return token


def _parse_simple_yaml(text: str) -> dict:
    cfg: dict = {}
    in_list: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent > 0 and in_list is not None:
            cfg.setdefault(in_list, []).append(_scalar(line.strip()))
            continue
        if ":" not in line:
            raise ConfigError(f"unparsable config line: {line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            in_list = key
            cfg.setdefault(key, [])
            continue
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        cfg[key] = _scalar(value)
        in_list = None
    return cfg


# --------------------------------------------------------------------------
# self-check entry point (kept tiny, run by CI before any rollout)
# --------------------------------------------------------------------------

def probe_self_check() -> str:
    jpeg_ok = validate_media(status=200, content_type="image/jpeg", body=_build_jpeg())
    assert jpeg_ok["ok"], f"jpeg self-check failed: {jpeg_ok['reasons']}"
    good_png = _build_png((2, 2))
    assert validate_media(status=200, content_type="image/png", body=good_png)["ok"]
    bad_png = good_png[:-1]  # truncate the IEND tail
    assert not validate_media(status=200, content_type="image/png", body=bad_png)["ok"], \
        "png must reject truncated/crc-bad data"
    spacer = validate_media(status=200, content_type="image/png", body=_build_png((1, 1)))
    assert not spacer["ok"] and spacer["placeholder"], "1x1 spacer PNG must be flagged"
    gif_ok = _validate_gif(b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x3b", [])
    assert gif_ok, "valid GIF89a must pass"
    http_bad = validate_media(status=404, content_type="image/jpeg", body=b"")
    assert not http_bad["ok"] and http_bad["state"] == "invalid"
    octet = validate_media(status=200, content_type="application/octet-stream", body=b"\x00\x01\x02\x03")
    assert not octet["ok"], "arbitrary octet-stream must be rejected"
    html_stub = validate_media(status=200, content_type="image/jpeg", body=b"<html><body>not an image</body></html>")
    assert not html_stub["ok"], "html placeholder must be rejected"
    return "archivist_core self-check ok"


def _build_png(size: tuple[int, int]) -> bytes:
    w, h = size
    ihdr = b"\x00\x00\x00\x0dIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" + \
        (zlib.crc32(b"IHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00") & 0xFFFFFFFF).to_bytes(4, "big")
    iend = b"\x00\x00\x00\x00IEND" + (zlib.crc32(b"IEND") & 0xFFFFFFFF).to_bytes(4, "big")
    return b"\x89PNG\r\n\x1a\n" + ihdr + iend


def _build_jpeg() -> bytes:
    """Minimal but structurally valid baseline JPEG: SOI, APP0, SOF0, SOS, EOI."""
    def seg(marker: bytes, payload: bytes) -> bytes:
        # JPEG segment length counts the two length bytes themselves
        return marker + (len(payload) + 2).to_bytes(2, "big") + payload

    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"      # 14 bytes payload
    sof0 = b"\x08" + (1).to_bytes(2, "big") + (1).to_bytes(2, "big") + b"\x01\x01\x11\x00"  # 9 payload
    sos = b"\x01\x01\x00\x00\x3f\x00"                            # 6 bytes payload
    return b"\xff\xd8" + seg(b"\xff\xe0", app0) + seg(b"\xff\xc0", sof0) + \
        seg(b"\xff\xda", sos) + b"\x00" + b"\xff\xd9"


if __name__ == "__main__":
    print(probe_self_check())