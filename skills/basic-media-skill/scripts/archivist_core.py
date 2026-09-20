#!/usr/bin/env python3
"""
archivist_core.py — shared deterministic layer for the archivist skills.

Single source of truth for the crypto/network/safety primitives that both
skills rely on, so basic and forum pipelines never drift apart:

  * URL normalization and same-domain checks
  * SSRF guard: no userinfo, no private/loopback/link-local/metadata targets,
    host re-validation after every redirect
  * robots.txt policy via urllib.robotparser, fetched from the origin root
    (never joined onto a deep target path)
  * fetch() with retry on 408/429/500/502/503/504, delay, byte budget
  * honest SHA-256 over the exact response bytes
  * atomic file store (temp file, fsync, rename, reread, hash re-verify)
  * media validation state machine (fetched -> stored -> magic -> decoded)
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
from pathlib import Path

URL_SCHEMES = {"http", "https"}
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}

# magic bytes per content type (used by both probe and validation)
MAGIC_BYTES: dict[str, tuple[bytes, int]] = {
    "image/jpeg": (b"\xff\xd8\xff", 3),
    "image/png": (b"\x89PNG\r\n\x1a\n", 8),
    "image/gif": (b"GIF8", 4),
    "image/webp": (b"RIFF", 4),
    "application/pdf": (b"%PDF-", 5),
    "application/zip": (b"PK\x03\x04", 4),
    "image/x-icon": (b"\x00\x00\x01\x00", 4),
}

DECODABLE = {"image/jpeg", "image/png", "image/gif", "image/webp"}
DEFAULT_USER_AGENT = "trehgranka-archivist/1.0 (+non-commercial research)"


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


def origin_root(url: str) -> str:
    """scheme://host/ — the only place robots.txt lives."""
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def same_domain(url: str, allowed: list[str]) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(host == str(d).lower() or host.endswith("." + str(d).lower()) for d in allowed)


def _is_public_host(hostname: str) -> tuple[bool, str]:
    """Resolve a hostname and reject non-public addresses (SSRF guard).

    Returns (allowed, note). Allows only globally routable unicast addresses.
    """
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
        if not (ip.is_global and not ip.is_multicast) or ip.is_loopback or ip.is_link_local:
            return False, f"non-public address {info[4][0]}"
    return True, "public"


def assert_public_target(url: str) -> None:
    """Raise ConfigError on private/loopback/link-local/metadata targets and userinfo."""
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password:
        raise ConfigError(f"URL must not contain credentials: {url!r}")
    ok, note = _is_public_host(parsed.hostname or "")
    if not ok:
        raise ConfigError(f"target must be a public address: {url!r} ({note})")


# --------------------------------------------------------------------------
# robots.txt policy (origin-root, fail-closed)
# --------------------------------------------------------------------------

def fetch_robots_policy(target_url: str, ua: str = DEFAULT_USER_AGENT, timeout: int = 15,
                        offline: bool = False) -> dict:
    """Fetch and parse robots.txt from the origin root.

    Returns a policy dict with a ready-to-use RobotFileParser under the key
    "parser" (None offline / on error) plus a machine-readable state string:
    allowed / disallowed / robots_missing / robots_unavailable.
    """
    root = origin_root(target_url)
    url = root + "robots.txt"
    state = "robots_unavailable"
    parser = None
    body = ""
    error = None
    if not offline:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                body = resp.read(20_000).decode("utf-8", errors="replace")
            if resp.status == 404:
                state = "robots_missing"
            else:
                state = "robots_present"
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                state = "robots_missing"
            else:
                error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 — record, never fail the report
            error = f"{type(exc).__name__}: {exc}"
    if state == "robots_present":
        parser = urllib.robotparser.RobotFileParser()
        parser.parse([ln for ln in body.splitlines() if ln.strip()])
    return {
        "url": url,
        "state": state,
        "body_bytes": len(body.encode("utf-8")),
        "fetched_at": _now_iso(),
        "error": error,
        "parser": parser,
    }


def robots_allows(policy: dict, url: str, ua: str = DEFAULT_USER_AGENT) -> bool:
    """True only when the robots policy explicitly allows crawling url."""
    state = policy.get("state")
    if state == "robots_present" and policy.get("parser") is not None:
        return bool(policy["parser"].can_fetch(ua, url))
    if state == "robots_missing":
        return True  # no robots.txt: nothing to honour, but still fail-closed upstream
    return False  # unavailable / offline: refuse to claim allowance


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

class _RevalidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-check public-target + allowed-domain on every redirect hop."""

    def __init__(self, allowed_domains: list[str], max_redirects: int = 5) -> None:
        super().__init__()
        self.allowed_domains = allowed_domains
        self.max_redirects = max_redirects
        self.chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        if len(self.chain) >= self.max_redirects:
            raise urllib.error.HTTPError(req.full_url, code, "too many redirects", headers, fp)
        if not normalize_url(newurl):
            raise urllib.error.HTTPError(req.full_url, code, "redirect to invalid URL", headers, fp)
        try:
            assert_public_target(newurl)
            if self.allowed_domains and not same_domain(newurl, self.allowed_domains):
                raise ValueError("redirect escapes allowed domains")
        except ConfigError as exc:
            raise urllib.error.HTTPError(req.full_url, code, str(exc), headers, fp) from exc
        except ValueError as exc:
            raise urllib.error.HTTPError(req.full_url, code, str(exc), headers, fp) from exc
        self.chain.append(newurl)
        return urllib.request.Request(newurl, headers=req.headers)


def fetch(url: str, *, ua: str = DEFAULT_USER_AGENT, timeout: int = 15,
          max_bytes: int = 100 * 2 ** 20, delay: float = 0.0, retries: int = 3,
          allowed_domains: list[str] | None = None,
          offline: bool = False) -> dict:
    """Fetch bytes with SSRF guard, redirect re-validation, retry and budgets.

    Returns a dict: {url, final_url, status, headers, body, redirect_chain,
    error, fetched_at}. body is b"" when the request failed.
    """
    chain: list[str] = []
    if offline:
        return {"url": url, "final_url": url, "status": 200, "headers": {},
                "body": b"", "redirect_chain": chain, "error": "offline: no fetch",
                "fetched_at": _now_iso()}
    assert_public_target(url)
    if allowed_domains and not same_domain(url, allowed_domains):
        return {"url": url, "final_url": url, "status": None, "headers": {},
                "body": b"", "redirect_chain": chain,
                "error": "url outside allowed domains", "fetched_at": _now_iso()}
    opener = urllib.request.build_opener(
        _RevalidatingRedirectHandler(allowed_domains or []))
    last_error = None
    for attempt in range(max(1, retries + 1)):
        if delay and attempt:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(max_bytes + 1)
                trunc = len(body) > max_bytes
                return {
                    "url": url,
                    "final_url": resp.geturl(),
                    "status": resp.status,
                    "headers": dict(resp.headers.items()),
                    "body": body[:max_bytes],
                    "truncated": trunc,
                    "redirect_chain": chain,
                    "error": None,
                    "fetched_at": _now_iso(),
                }
        except urllib.error.HTTPError as exc:
            last_error = f"http {exc.code}"
            if exc.code in RETRYABLE_STATUSES:
                continue
            return {"url": url, "final_url": url, "status": exc.code, "headers": {},
                    "body": b"", "redirect_chain": chain, "error": last_error,
                    "fetched_at": _now_iso()}
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries and any(s in str(exc) for s in ("timed out", "timeout")):
                continue
            break
    return {"url": url, "final_url": url, "status": None, "headers": {},
            "body": b"", "redirect_chain": chain, "error": last_error,
            "fetched_at": _now_iso()}


# --------------------------------------------------------------------------
# hashing + atomic storage
# --------------------------------------------------------------------------

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_store(path: Path, data: bytes) -> dict:
    """Write bytes durably: temp file, fsync, rename, reread, hash re-verify.

    Never leaves a partial file behind and never overwrites an existing file
    with different bytes. Returns {path, sha256, size, verified}.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256(data)
    fd, tmp = tempfile.mkstemp(prefix=".part-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            existing = path.read_bytes()
            if existing == data:
                os.unlink(tmp)
                return {"path": str(path), "sha256": digest, "size": len(data), "verified": True}
            # keep the first good copy: store as a hash-suffixed sibling
            sibling = path.with_name(f"{path.name}.{digest[:8]}")
            os.replace(tmp, sibling)
            return {"path": str(sibling), "sha256": digest, "size": len(data), "verified": True,
                    "note": "collision with different bytes, stored under hash suffix"}
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # reread + verify
    stored = path.read_bytes()
    verified = sha256(stored) == digest
    return {"path": str(path), "sha256": digest, "size": len(data), "verified": verified}


# --------------------------------------------------------------------------
# media validation state machine
# --------------------------------------------------------------------------

def detect_mime(body: bytes, declared: str | None) -> str | None:
    """Content type from magic bytes, falling back to declared type."""
    for ctype, (sig, _n) in MAGIC_BYTES.items():
        if body.startswith(sig):
            return ctype
    return (declared or "").split(";")[0].strip().lower().strip() or None


def decode_checks(body: bytes, ctype: str) -> list[str]:
    """Structural decode checks (stdlib-only): dimensions for images, EOF
    markers for pdf/zip. Returns a list of failure reasons (empty = decodes)."""
    reasons: list[str] = []
    if ctype == "image/jpeg":
        if not (body.startswith(b"\xff\xd8") and body.rstrip(b"\x00").endswith(b"\xff\xd9")):
            reasons.append("jpeg missing EOI marker (truncated)")
    elif ctype == "image/png":
        if len(body) < 24 or body[12:16] != b"IHDR":
            reasons.append("png missing IHDR chunk")
    elif ctype == "image/gif":
        if len(body) < 13 or body[6:10] not in (b"87a", b"89a"):
            reasons.append("gif header malformed")
    elif ctype == "image/webp":
        if len(body) < 12 or body[8:12] != b"WEBP":
            reasons.append("webp missing WEBP marker")
    elif ctype == "application/pdf":
        if b"%%EOF" not in body[-2048:]:
            reasons.append("pdf missing %%EOF trailer (truncated)")
    elif ctype == "application/zip":
        if body[-22:-2] != b"PK\x05\x06":
            reasons.append("zip missing end-of-central-directory")
    return reasons


def validate_media(*, status, content_type, body, role: str = "unknown") -> dict:
    """Validation state machine for one fetched media object.

    Returns {ok, state, reasons, content_type, sha256, size, dimensions}.
    ok requires: final 200, non-empty body, known-decodable type, magic match,
    decode check pass, and byte presence (never empty data).
    """
    reasons: list[str] = []
    state = "fetched"
    if isinstance(status, int) and status != 200:
        reasons.append(f"http {status}")
        state = f"failed({status})"
    elif isinstance(status, str) and status != "200":
        reasons.append(f"http {status}")
        state = f"failed({status})"
    body = body or b""
    if not body:
        reasons.append("empty body")
        state = "failed(empty)"
    ctype = detect_mime(body, content_type)
    if ctype in ("text/html", "text/plain", "application/json") and body:
        reasons.append(f"placeholder/html instead of media ({ctype})")
        state = "failed(placeholder)"
    if not ctype and body:
        reasons.append("unrecognized content type")
        state = "failed(unrecognized)"
    if ctype in MAGIC_BYTES:
        sig, n = MAGIC_BYTES[ctype]
        if not body.startswith(sig):
            reasons.append(f"magic bytes do not match {ctype}")
            state = "failed(magic)"
        elif ctype in DECODABLE:
            dec = decode_checks(body, ctype)
            if dec:
                reasons.extend(dec)
                state = "failed(undecodable)"
            else:
                state = "decoded"
    if not reasons and state == "fetched":
        state = "magic_verified"
    return {
        "ok": not reasons,
        "state": state,
        "reasons": reasons,
        "content_type": ctype,
        "sha256": sha256(body) if body else "",
        "size": len(body),
    }


# --------------------------------------------------------------------------
# config validation
# --------------------------------------------------------------------------

DEFAULT_LIMITS = {
    "MAX_PAGES": (1, 10_000_000),
    "MAX_DEPTH": (0, 100),
    "MAX_RESPONSE_BYTES": (1_024, 2 ** 33),
    "MAX_REDIRECTS": (0, 20),
    "RETRY_COUNT": (0, 10),
    "RECOVERY_CAPTURE_LIMIT": (1, 10_000),
}


def validate_config(cfg: dict, offline: bool = False) -> None:
    """Strict config validation: types, ranges, budgets, self-consistency.

    Public-address (SSRF) DNS checks are skipped in offline mode: fixtures use
    non-resolving example.org hosts. URL format checks always run."""
    target = cfg.get("TARGET_URL")
    if not isinstance(target, str) or not target.strip():
        raise ConfigError("TARGET_URL must be a non-empty URL")
    if not normalize_url(target):
        raise ConfigError(f"TARGET_URL is not a valid http(s) URL: {target!r}")
    if not offline:
        assert_public_target(target)

    allowed = cfg.get("ALLOWED_DOMAINS")
    if not isinstance(allowed, list) or not allowed:
        raise ConfigError("ALLOWED_DOMAINS must be a non-empty list of domains")
    target_host = urllib.parse.urlparse(target).netloc.lower()
    if target_host not in {str(d).lower() for d in allowed}:
        raise ConfigError("TARGET_URL host must be listed in ALLOWED_DOMAINS")

    scope = cfg.get("SCOPE", "site")
    if scope not in {"site", "section", "gallery", "forum", "topics", "urls"}:
        raise ConfigError(f"SCOPE must be one of site|section|gallery|forum|topics|urls, got {scope!r}")

    for key, lo, hi in (
        ("MAX_PAGES", 1, 10_000_000),
        ("MAX_DEPTH", 0, 100),
        ("MAX_RESPONSE_BYTES", 1_024, 2 ** 33),
        ("MAX_REDIRECTS", 0, 20),
        ("RETRY_COUNT", 0, 10),
        ("RECOVERY_CAPTURE_LIMIT", 1, 10_000),
    ):
        val = cfg.get(key)
        if val is None:
            continue
        if not isinstance(val, int) or isinstance(val, bool) or not (lo <= val <= hi):
            raise ConfigError(f"{key} must be an integer in [{lo}, {hi}], got {val!r}")

    delay = cfg.get("REQUEST_DELAY_SECONDS", 0.0)
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        raise ConfigError(f"REQUEST_DELAY_SECONDS must be >= 0, got {delay!r}")

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
    """Flat key/value subset with `- item` lists (the bundled fixtures)."""
    cfg: dict = {}
    current_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("-") and current_key:
            token = stripped[1:].strip().strip("'\"")
            cfg.setdefault(current_key, []).append(_scalar(token))
            continue
        if ":" not in line:
            current_key = None
            continue
        key, _, value = line.partition(":")
        current_key = key.strip()
        if not current_key:
            continue
        if value.strip() == "":
            cfg[current_key] = []
            continue
        raw = value.strip()
        if raw.startswith("[") and raw.endswith("]"):
            cfg[current_key] = [
                _scalar(item.strip().strip("'\""))
                for item in raw[1:-1].split(",")
                if item.strip()
            ]
        else:
            cfg[current_key] = _scalar(raw.strip().strip("'\""))
    return cfg


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def run_self_check() -> int:
    """Offline invariant checks (no network). Exit 0 on success."""
    v = _parse_simple_yaml("A: 1\nB: [x, y]\nC:\n  - 1\n  - 2\n")
    assert v == {"A": 1, "B": ["x", "y"], "C": [1, 2]}, v
    assert normalize_url("https://user:pass@example.org/x") is None
    assert origin_root("https://example.org/a/b") == "https://example.org/"
    assert same_domain("https://www.example.org/x", ["example.org"])
    assert not same_domain("https://evil.invalid/x", ["example.org"])
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
    vr = validate_media(status=200, content_type="image/jpeg", body=jpg)
    assert vr["ok"] and vr["state"] == "decoded", vr
    bad = validate_media(status=200, content_type="image/jpeg", body=b"\xff\xd8\xff" + b"\x00" * 8)
    assert not bad["ok"] and "shorted" in " ".join(bad["reasons"]) or "EOI" in " ".join(bad["reasons"]), bad
    ph = validate_media(status=200, content_type="image/jpeg", body=b"<html>placeholder</html>")
    assert not ph["ok"] and ("placeholder" in " ".join(ph["reasons"]) or "magic" in " ".join(ph["reasons"])), ph
    empty = validate_media(status=200, content_type="image/jpeg", body=b"")
    assert not empty["ok"] and "empty" in " ".join(empty["reasons"]), empty
    print("archivist_core self-check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_self_check())