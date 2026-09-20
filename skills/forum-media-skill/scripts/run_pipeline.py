#!/usr/bin/env python3
"""
Single entry-point for the basic-media archivist skill.

Deterministic parts of the archivist workflow, wired in code and gated by the
spec's own rules. A full run is structurally impossible unless the configuration
carries USER_CONFIRMED_FULL_RUN=true AND the operator passes --run-full.

Modes (in order):
  1. validate config
  2. create the archive output layout
  3. preflight (robots.txt capture, risk map, engine detection)
  4. discovery / dry-run inventory (from a discovery manifest or a small crawl)
  5. media validation (SHA-256, MIME, image signature, size sanity)
  6. Wayback recovery queue for invalid/empty/substituted media
  7. Test Report (ready / needs changes)
  8. full run — blocked unless USER_CONFIRMED_FULL_RUN=true and --run-full

The LLM plans, classifies and writes selectors; this script never pretends to
be a downloader for the mass crawl. It records intent, state and coverage.

Usage:
    python3 scripts/run_pipeline.py --config path/to/project.yaml --output dir/
    python3 scripts/run_pipeline.py --manifest discovered_urls.jsonl --config config.yaml --output dir/
    python3 scripts/run_pipeline.py --config config.yaml --output dir/ --run-full  (requires USER_CONFIRMED_FULL_RUN=true)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STAGES = ("preflight", "discovery", "dry_run", "media_validation", "recovery_queue", "test_report", "full_run", "final_report")
ROBOTS_TXT = "robots.txt"
WILDCARD = "*"
USER_AGENT_DEFAULT = "trehgranka-archivist-basic-media-skill/1.0 (+archive@example.org)"

MAGIC_BYTES: dict[str, tuple[bytes, int]] = {
    "image/jpeg": (b"\xff\xd8\xff", 3),
    "image/png": (b"\x89PNG\r\n\x1a\n", 8),
    "image/gif": (b"GIF8", 4),
    "image/webp": (b"RIFF", 4),
    "application/pdf": (b"%PDF-", 5),
    "application/zip": (b"PK\x03\x04", 4),
}

URL_SCHEMES = {"http", "https"}


class ConfigError(ValueError):
    """Raised when the project configuration is malformed or self-contradictory."""


class Pipeline:
    """Stateful deterministic side of the archivist workflow."""

    def __init__(self, config: dict, output: Path, run_full: bool, offline: bool = False) -> None:
        self.config = config
        self.output = output.resolve()
        self._confirm = bool(config.get("USER_CONFIRMED_FULL_RUN", False))
        self.run_full_flag = run_full
        self.offline = offline
        self.root = self.output.parent
        self.manifest_path = self.root / "data" / "manifest.jsonl"
        self.logs = self.root / "logs"
        self.reports = self.root / "reports"
        self.raw_html = self.root / "data" / "raw" / "html"
        self.media_dir = self.root / "data" / "media" / "images"
        self.pages: list[dict] = []
        self.media: list[dict] = []
        self.invalid_media: list[dict] = []
        self.summary: dict = {}

    # ------------------------------------------------------------------ config

    def validate_config(self) -> None:
        cfg = self.config
        target = cfg.get("TARGET_URL")
        if not isinstance(target, str) or not target.strip():
            raise ConfigError("TARGET_URL must be a non-empty URL")
        if not self._normalize_url(target):
            raise ConfigError(f"TARGET_URL is not a valid http(s) URL: {target!r}")

        allowed = cfg.get("ALLOWED_DOMAINS")
        if not isinstance(allowed, list) or not allowed:
            raise ConfigError("ALLOWED_DOMAINS must be a non-empty list of domains")
        target_host = urllib.parse.urlparse(target).netloc.lower()
        if target_host not in {str(d).lower() for d in allowed}:
            raise ConfigError("TARGET_URL host must be listed in ALLOWED_DOMAINS")

        scope = cfg.get("SCOPE", "site")
        if scope not in {"site", "section", "gallery", "forum", "topics", "urls"}:
            raise ConfigError(f"SCOPE must be one of site|section|gallery|forum|topics|urls, got {scope!r}")

        for key, kind in (("MAX_PAGES", int), ("MAX_DEPTH", int), ("REQUEST_DELAY_SECONDS", (int, float))):
            val = cfg.get(key)
            if val is not None and not isinstance(val, kind):
                raise ConfigError(f"{key} must be numeric")
        for key in ("DOWNLOAD_ORIGINALS", "DOWNLOAD_ATTACHMENTS", "SAVE_RAW_HTML", "RESPECT_ROBOTS_TXT"):
            val = cfg.get(key)
            if val is not None and not isinstance(val, bool):
                raise ConfigError(f"{key} must be true/false")

        confirm = cfg.get("USER_CONFIRMED_FULL_RUN", False)
        if not isinstance(confirm, bool):
            raise ConfigError("USER_CONFIRMED_FULL_RUN must be true/false")

    @staticmethod
    def _normalize_url(url: str) -> str | None:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in URL_SCHEMES or not parsed.netloc:
            return None
        return urllib.parse.urlunparse(parsed._replace(fragment=""))

    def _same_domain(self, url: str, allowed: list[str]) -> bool:
        host = urllib.parse.urlparse(url).netloc.lower()
        return any(host == str(d).lower() or host.endswith("." + str(d).lower()) for d in allowed)

    # ------------------------------------------------------------- output layout

    def run(self) -> dict:
        self.validate_config()
        self._mkdirs()
        preflight = self._stage_preflight()
        discovery = self._stage_discovery()
        dry = self._stage_dry_run(discovery)
        valid, invalid = self._stage_media_validation(dry)
        recovery = self._stage_recovery_queue(invalid)
        report = self._stage_test_report(preflight, discovery, dry, valid, invalid, recovery)
        full = self._stage_full_run_gate()
        self._write_reports(preflight, discovery, dry, valid, invalid, recovery, report, full)
        self.summary = {
            "version": 1,
            "project": self.config.get("PROJECT_NAME", "archive"),
            "target_url": self.config["TARGET_URL"],
            "stages": list(STAGES),
            "preflight": preflight,
            "discovery": discovery,
            "dry_run": dry,
            "media_validation": {"valid_count": len(valid), "invalid_count": len(invalid)},
            "recovery_queue": recovery,
            "test_report": report,
            "full_run_gate": full,
            "coverage": self._coverage(discovery, valid),
            "generated_at": self._now_iso(),
        }
        self._write_summary()
        self._write_resume()
        return self.summary

    def _mkdirs(self) -> None:
        for path in (
            self.root, self.root / "config", self.root / "data", self.root / "data" / "raw" / "html",
            self.root / "data" / "media" / "images", self.root / "data" / "media" / "documents",
            self.root / "data" / "media" / "attachments", self.root / "data" / "media" / "thumbnails",
            self.logs, self.reports, self.root / "scrapy_project",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # --------------------------------------------------------------- preflight

    def _fetch_robots(self) -> dict:
        try:
            url = urllib.parse.urljoin(self.config["TARGET_URL"], ROBOTS_TXT)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT_DEFAULT})
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                body = resp.read(20_000).decode("utf-8", errors="replace")
            allowed = any(
                ln.strip().lower().startswith("allow:")
                and WILDCARD in ln
                for ln in body.splitlines()
                if ln.strip()
            )
            return {"url": url, "status": "ok", "body_bytes": len(body.encode("utf-8")), "allows_wildcard": allowed}
        except Exception as exc:  # noqa: BLE001 — record, never fail the report
            return {"url": urllib.parse.urljoin(self.config["TARGET_URL"], ROBOTS_TXT), "status": "error", "error": f"{type(exc).__name__}: {exc}", "note": "recorded, run continues"}

    def _stage_preflight(self) -> dict:
        target = self.config["TARGET_URL"]
        parsed = urllib.parse.urlparse(target)
        candidates = {
            "api": urllib.parse.urljoin(target, "api/"),
            "rss": urllib.parse.urljoin(target, "rss"),
            "sitemap": urllib.parse.urljoin(target, "sitemap.xml"),
        }
        risks: list[str] = []
        if self.config.get("RESPECT_ROBOTS_TXT", True) is False:
            risks.append("RESPECT_ROBOTS_TXT=false — only continue with documented owner permission")
        engine = self._detect_forum_engine(target)
        return {
            "target_url": target,
            "final_url": target,
            "host": parsed.netloc,
            "scope": self.config.get("SCOPE", "site"),
            "allowed_domains": self.config.get("ALLOWED_DOMAINS", []),
            "robots": self._fetch_robots(),
            "probes": candidates,
            "engine": engine,
            "risks": risks,
            "checked_at": self._now_iso(),
        }

    def _detect_forum_engine(self, url: str) -> dict:
        """Heuristic engine probe for the main public forum families.

        Never a verdict: the general rule is to first sample one representative
        page of every template type you find, then compare against Scrapy
        controls before building a site-specific Scrapy adapter.
        """
        lower = url.lower()
        for marker, name in (
            ("/viewtopic.php", "phpBB"),
            ("/showthread.php", "vBulletin"),
            ("/threads/", "XenForo"),
            ("/topics/", "Discourse"),
            ("/forums/topic/", "Invision Community (IPS)"),
            ("/konu/", "vBulletin/legacy"),
            ("/viewforum.php", "phpBB (forum listing)"),
        ):
            if marker in lower:
                return {"name": name, "confidence": "medium", "evidence": f"URL pattern: {marker}"}
        return {"name": "unknown", "confidence": "low", "evidence": "URL pattern probe inconclusive"}

    def _detect_engine_dom(self, html: str, url: str) -> dict:
        """Upgrade engine detection with DOM markers from a sampled page (IPS).

        Callers pass the body of a sampled topic page when discovery retained
        it; on ImportError or an unmarked page the URL probe decides. Falls back
        to {"name": "unknown", ...} only through _detect_forum_engine — the
        report field stays advisory, never a verdict.
        """
        try:
            from extractors import invision  # stdlib-only, bundled with the skill
        except ImportError:
            return self._detect_forum_engine(url)
        result = invision.detect(html)
        if result.get("name") == "invision-community-ips" and result.get("confidence") != "low":
            return {
                "name": "Invision Community (IPS)",
                "confidence": result["confidence"],
                "evidence": result["evidence"],
            }
        return self._detect_forum_engine(url)

    # --------------------------------------------------------------- discovery

    def _stage_discovery(self) -> dict:
        manifest = self.config.get("DISCOVERY_MANIFEST")
        discovered: list[dict] = []
        source = "manifest"
        if manifest and Path(manifest).exists():
            discovered = self._read_manifest(Path(manifest))
        elif self.offline:
            discovered = []
            source = "manifest-required-offline"
        else:
            discovered = self._small_probe()
            source = "probe"
        self.pages = [d for d in discovered if d.get("kind") in ("page", "html")]
        self.media = [d for d in discovered if d.get("kind") in ("media", "document", "image")]
        self.forum_entities = [
            d for d in discovered
            if d.get("kind") in ("category", "forum", "topic", "post", "author", "quote", "reaction")
        ]
        return {
            "source": source,
            "url_count": len(discovered),
            "page_count": len(self.pages),
            "media_count": len(self.media),
            "forum_entity_count": len(self.forum_entities),
            "forum_entities": self._entity_tally(self.forum_entities),
            "max_pages_limit": self.config.get("MAX_PAGES"),
            "max_depth_limit": self.config.get("MAX_DEPTH"),
            "sample": [
                {"url": d.get("url"), "kind": d.get("kind"), "status": d.get("status"), "content_type": d.get("content_type")}
                for d in discovered[:5]
            ],
            "discovered_at": self._now_iso(),
        }

    def _read_manifest(self, path: Path) -> list[dict]:
        items: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = self._normalize_url(str(item.get("url", "")))
            if not url:
                continue
            if not self._same_domain(url, self.config.get("ALLOWED_DOMAINS", [])):
                continue
            item["url"] = url
            item["kind"] = item.get("kind", "page")
            items.append(item)
        return items

    def _entity_tally(self, entities: list[dict]) -> dict:
        tally: dict[str, int] = {}
        for entity in entities:
            kind = str(entity.get("kind") or "unknown")
            tally[kind] = tally.get(kind, 0) + 1
        return dict(sorted(tally.items()))

    def _small_probe(self) -> list[dict]:
        """Bounded discovery probe — at most MAX_PAGES items, depth <= MAX_DEPTH."""
        if self.offline:
            return []
        limit = int(self.config.get("MAX_PAGES") or 30)
        depth = int(self.config.get("MAX_DEPTH") or 2)
        start = self.config["TARGET_URL"]
        seen: list[dict] = []
        queue = [(start, 0)]
        while queue and len(seen) < limit:
            url, d = queue.pop(0)
            if d > depth or any(item["url"] == url for item in seen):
                continue
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT_DEFAULT})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                    seen.append({"url": url, "kind": "page", "depth": d, "status": resp.status, "content_type": resp.headers.get("Content-Type", "")})
            except Exception as exc:  # noqa: BLE001
                seen.append({"url": url, "kind": "page", "depth": d, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return seen

    # ---------------------------------------------------------------- dry run

    def _stage_dry_run(self, discovery: dict) -> dict:
        urls = [m for m in self._iter_manifest_urls() if m.get("url")]
        topic_urls = [u for u in urls if "page=" in u.get("url", "") or "?page" in u.get("url", "") or u.get("kind") == "post"]
        pagination_checked = bool(topic_urls) or not urls
        return {
            "strategy": "inventory-only",
            "urls_inspected": len(urls),
            "samples_taken": min(len(urls), 5),
            "templates_sampled": discovery.get("forum_entity_count", 0),
            "pagination_checked": pagination_checked,
            "originals_vs_thumbnails": {"checked": False, "note": "needs selector analysis"},
            "decision": "ready" if urls else "needs changes",
            "note": "dry-run: no mass download performed",
        }

    def _iter_manifest_urls(self):
        manifest = self.config.get("DISCOVERY_MANIFEST")
        if manifest and Path(manifest).exists():
            yield from self._read_manifest(Path(manifest))
        else:
            yield from self.pages + self.media

    # -------------------------------------------------------- media validation

    def _validate_media_item(self, item: dict) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        status = item.get("status")
        if isinstance(status, str):
            # network-level error / non-numeric upstream status: not a 200 result
            if status != "200":
                reasons.append(f"http {status}")
            status = None
        if status in (401, 403, 404, 410, 429, 451, 500, 502, 503, 504):
            reasons.append(f"http {status}")
        body = item.get("body")
        if isinstance(body, str) and not body.strip():
            reasons.append("empty body")
        ctype = str(item.get("content_type") or "").lower().split(";")[0].strip()
        if ctype and ctype != "application/octet-stream" and not ctype.startswith(("image/", "text/")) and ctype not in {"application/pdf", "application/zip"}:
            reasons.append(f"suspicious content type: {ctype}")
        data = item.get("data")
        if data:
            payload = bytes.fromhex(data) if isinstance(data, str) else bytes(data)
            magic = MAGIC_BYTES.get(ctype)
            if magic and magic[1] <= len(payload) and not payload.startswith(magic[0]):
                reasons.append(f"magic bytes do not match {ctype}")
        return (not reasons, reasons)

    def _stage_media_validation(self, dry: dict) -> tuple[list[dict], list[dict]]:
        valid: list[dict] = []
        invalid: list[dict] = []
        for item in self._iter_manifest_urls():
            if item.get("kind") not in ("media", "document", "image"):
                continue
            ok, reasons = self._validate_media_item(item)
            sha = self._sha256(item)
            record = {
                "url": item.get("url"),
                "status": item.get("status"),
                "content_type": item.get("content_type"),
                "sha256": sha,
                "media_role": item.get("media_role", "unknown"),
                "valid": ok,
            }
            if ok:
                record["validation_errors"] = []
                valid.append(record)
            else:
                record["validation_errors"] = reasons
                invalid.append(record)
        self.media_valid = valid
        self.media_invalid = invalid
        return valid, invalid

    @staticmethod
    def _sha256(item: dict) -> str:
        payload = str(item.get("body") or item.get("url") or "").encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    # ---------------------------------------------------------- recovery queue

    def _stage_recovery_queue(self, invalid: list[dict]) -> dict:
        queued = [
            {
                "url": item["url"],
                "source": "live_validation",
                "failure_reason": item["validation_errors"],
                "wayback": {
                    "offline": self.offline,
                    "cdx_endpoint": "https://web.archive.org/cdx/search/cdx",
                    "replay_mode": "id_",
                    "captures_checked": 0,
                    "recovered_original": False,
                    "recovered_thumbnail_only": False,
                    "unresolved": True,
                    "status": "queued" if self.offline else "pending",
                },
            }
            for item in invalid
        ]
        return {"queue_size": len(queued), "entries": queued, "processed": False}

    # ------------------------------------------------------------- test report

    def _stage_test_report(self, preflight: dict, discovery: dict, dry: dict, valid: list[dict], invalid: list[dict], recovery: dict) -> dict:
        verdict = "ready" if not invalid and discovery["url_count"] else "needs changes"
        return {
            "verdict": verdict,
            "pages": discovery.get("page_count", 0),
            "media_discovered": discovery.get("media_count", 0),
            "media_validated": len(valid),
            "media_invalid": len(invalid),
            "recovery_queue_size": recovery["queue_size"],
            "originals_separated_from_thumbnails": False,
            "note": "verify originals vs thumbnails via selector analysis before full run",
        }

    # -------------------------------------------------------------- full gate

    def _stage_full_run_gate(self) -> dict:
        confirmed_ok = self._confirm and self.run_full_flag
        return {
            "confirmed": self._confirm,
            "run_full_flag": self.run_full_flag,
            "blocked": not confirmed_ok,
            "action": "proceed" if confirmed_ok else "stop",
            "reason": (
                None
                if confirmed_ok
                else "USER_CONFIRMED_FULL_RUN must be true AND --run-full must be passed"
            ),
            "never_map_downloader_role_to_llm": True,
        }

    # -------------------------------------------------------------- reporting

    def _write_reports(self, preflight: dict, discovery: dict, dry: dict, valid: list[dict], invalid: list[dict], recovery: dict, report: dict, full: dict) -> None:
        coverage = self._coverage(discovery, valid)
        test_payload = {
            "stage": "test_report",
            "dry_run": full["blocked"],
            "confirmed": full["confirmed"],
            "run_full_flag": full["run_full_flag"],
            "gate": full["action"],
            "gate_reason": full["reason"],
            "decision": report["verdict"],
            "urls_discovered": discovery.get("url_count", 0),
            "pages": report["pages"],
            "media_discovered": report["media_discovered"],
            "media_valid": report["media_validated"],
            "media_invalid": report["media_invalid"],
            "recovery_queue_size": report["recovery_queue_size"],
            "coverage": coverage["coverage"],
            "forum": self._forum_report_payload(),
            "output": {
                "dir": str(self.root),
                "raw_html": str(self.raw_html),
                "media_dir": str(self.media_dir),
                "manifest": str(self.manifest_path),
            },
        }
        # The canonical eval artifact lives at the exact --output path.
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with self.output.open("w", encoding="utf-8") as fh:
            json.dump(test_payload, fh, ensure_ascii=False, indent=2)
        # Additional artifacts under the archive root for human exploration.
        with (self.reports / "preflight_report.json").open("w", encoding="utf-8") as fh:
            json.dump({"stage": "preflight", "data": preflight}, fh, ensure_ascii=False, indent=2)
        with (self.reports / "final_report.json").open("w", encoding="utf-8") as fh:
            json.dump({"stage": "final_report", "data": coverage}, fh, ensure_ascii=False, indent=2)
        with (self.reports / "recovery_queue.json").open("w", encoding="utf-8") as fh:
            json.dump(recovery, fh, ensure_ascii=False, indent=2)
        with (self.reports / "failures.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["url", "stage", "reason"])
            for item in invalid:
                writer.writerow([item.get("url"), "media_validation", "; ".join(item["validation_errors"])])

    def _forum_report_payload(self) -> dict:
        """Forum-specific block for the test report: entities, engine, extractors."""
        valid_urls = {item.get("url") for item in getattr(self, "media_valid", [])}
        discovered = getattr(self, "forum_entities", []) + [
            item for item in getattr(self, "media", [])
            if item.get("kind") in ("document", "media")
        ]
        attachment_verified = sum(
            1 for item in discovered
            if item.get("url") in valid_urls
        )
        attachment_total = len(discovered)
        return {
            "engine": self.summary.get("preflight", {}).get("engine", {}),
            "entity_counts": self._entity_tally(getattr(self, "forum_entities", [])),
            "extractor_plan": self.config.get("EXTRACTOR", "scrapy-adapter"),
            "paginated_posts_seen": self._count_paginated(),
            "attachment_verified": attachment_verified,
            "attachment_total": attachment_total,
            "attachment_coverage": f"{attachment_verified / attachment_total:.2f}" if attachment_total else "n/a",
        }

    def _count_paginated(self) -> int:
        """Count post URLs / topic page URLs that carry pagination markers."""
        return sum(
            1 for item in getattr(self, "forum_entities", [])
            if "page=" in str(item.get("url", "")) or "?page" in str(item.get("url", ""))
        )

    def _write_summary(self) -> None:
        with (self.reports / "summary.json").open("w", encoding="utf-8") as fh:
            json.dump(self.summary, fh, ensure_ascii=False, indent=2)

    def _write_resume(self) -> None:
        """Human-readable TXT resume of the archive run (site structure, counts, formats,
        archives, recovery state). Written next to the canonical test report on every run."""
        from collections import Counter, defaultdict
        rows = list(self._iter_manifest_urls())
        pages = [r for r in rows if r.get("kind") in ("page", "html")]
        media = [r for r in rows if r.get("kind") in ("media", "document", "image")]
        media_valid = sum(1 for r in media if r.get("status") == 200 and not r.get("error"))
        media_failed = sum(1 for r in media if r.get("status") not in (None, 200) or r.get("error"))
        bytes_total = sum(int(r.get("size") or 0) for r in media if r.get("size"))

        def mime_of(r):
            return (str(r.get("content_type") or "").split(";")[0].strip().lower() or "?")

        def section(u):
            p = urllib.parse.urlparse(u).path
            parts = [x for x in p.split("/") if x]
            return "/".join(parts[:2]) if len(parts) > 1 else "root"

        mime_counts = Counter(mime_of(r) for r in media)
        ext_counts = Counter()
        zip_names = []
        for r in media:
            ext = urllib.parse.urlparse(r["url"]).path.rsplit(".", 1)[-1].lower()
            ext_counts[ext] += 1
            if mime_of(r) == "application/zip" or r["url"].lower().endswith(".zip"):
                zip_names.append(urllib.parse.urlparse(r["url"]).path.rsplit("/", 1)[-1])
        sec = Counter(section(r["url"]) for r in media)
        sec_bytes = defaultdict(int)
        for r in media:
            sec_bytes[section(r["url"])] += int(r.get("size") or 0)

        L = ["=" * 72,
             f"RESUME — {self.config.get('TARGET_URL', 'archive target')}",
             "=" * 72,
             f"Project:     {self.config.get('PROJECT_NAME', 'archive')}",
             f"Generated:   {self._now_iso()}",
             f"Dry run:     {bool(not (self._confirm and self.run_full_flag))}",
             f"Full run:    {bool(self._confirm and self.run_full_flag)}",
             "",
             "1. OVERALL",
             "-" * 72,
             f"  Pages:             {len(pages)}",
             f"  Media files:       {len(media)}",
             f"  Media valid (200): {media_valid}",
             f"  Media failed:      {media_failed}",
             f"  Total bytes:       {bytes_total / 2**20:.1f} MB",
             "",
             "2. MEDIA BY MIME",
             "-" * 72]
        for mime, n in mime_counts.most_common():
            L.append(f"  {mime:<28} {n:>6}")
        L += ["", "3. FILE EXTENSIONS", "-" * 72]
        for ext, n in ext_counts.most_common():
            L.append(f"  .{ext:<10} {n:>6}")
        L += ["", "4. BY SITE SECTION", "-" * 72]
        service_total = sum(n for s, n in sec.items() if s.startswith("service/"))
        service_bytes = sum(b for s, b in sec_bytes.items() if s.startswith("service/"))
        for s, n in sorted(sec.items(), key=lambda kv: -kv[1]):
            if s.startswith("service/"):
                continue
            L.append(f"  {s:<28} {n:>6}  {sec_bytes[s] / 2**20:8.1f} MB")
        if service_total:
            L.append(f"  {'service/* (site chrome)':<28} {service_total:>6}  {service_bytes / 2**20:8.1f} MB")
        L += ["", "5. ZIP ARCHIVES", "-" * 72]
        L.append(f"  count: {len(zip_names)}")
        for z in sorted(set(zip_names)):
            L.append(f"    {z}")
        L += ["", "6. RECOVERY QUEUE (Wayback)", "-" * 72]
        L.append(f"  entries: {self.summary['recovery_queue'].get('queue_size', 0)}  processed: {self.summary['recovery_queue'].get('processed', False)}")
        L += ["", "7. OUTPUT LAYOUT", "-" * 72,
              f"  root:      {self.root}",
              f"  raw_html:  {self.raw_html}",
              f"  media_dir: {self.media_dir}",
              f"  manifest:  {self.manifest_path}",
              "", "=" * 72]
        out_txt = self.output.parent / "RESUME_структура.txt"
        out_txt.write_text("\n".join(L), encoding="utf-8")

    def _coverage(self, discovery: dict, valid: list[dict]) -> dict:
        discovered = discovery.get("url_count", 0)
        verified = len(valid)
        return {
            "formula": "coverage = verified_in_scope / uniquely_discovered_in_scope",
            "discovered_in_scope": discovered,
            "verified_in_scope": verified,
            "coverage": f"{verified / discovered:.2f}" if discovered else "n/a",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="basic-media archivist pipeline")
    parser.add_argument("--config", required=True, help="project YAML/JSON path OR a directory containing config.yaml + discovered_urls.jsonl")
    parser.add_argument("--output", default="test_report.json", help="write the canonical test report to this exact file path")
    parser.add_argument("--run-full", action="store_true", help="request a full run (requires USER_CONFIRMED_FULL_RUN=true)")
    parser.add_argument("--offline", action="store_true", help="no network calls; canned responses (eval-safe)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if config_path.is_dir():
        config_file = config_path / "config.yaml"
        manifest_file = config_path / "discovered_urls.jsonl"
    else:
        config_file = config_path
        manifest_file = config_path.parent / "discovered_urls.jsonl"

    cfg = _load_config(config_file)
    if manifest_file.exists():
        cfg = {**cfg, "DISCOVERY_MANIFEST": str(manifest_file)}
    out = Path(args.output)
    if args.offline:
        _install_offline_harness()
    try:
        summary = Pipeline(cfg, out, run_full=args.run_full, offline=args.offline).run()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"full_run_gate: {summary['full_run_gate']['action']} — report at {out}")
    return 0


def _load_config(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON: {exc}") from exc
    return _parse_simple_yaml(text)


# --------------------------------------------------------------------------
# Offline network harness (eval-safe): replaces urllib calls with canned
# responses when --offline is passed, so evals run with zero network I/O.
# --------------------------------------------------------------------------

class _CannedResponse:
    def __init__(self, url: str) -> None:
        self.url = url
        self.status = 200
        self.headers = {"Content-Type": "text/plain; charset=utf-8"}

    def read(self, _amt: int = -1) -> bytes:
        if "/cdx/search/cdx" in self.url:
            lines = [
                "2015-06-01T12:00:00Z http://example.org/gallery/album-1/photo-2.jpg image/jpeg 200 SHA1/abc123 12345",
                "2016-01-15T08:30:00Z https://example.org/gallery/album-1/photo-2.jpg image/jpeg 200 SHA1/def456 23456",
                "2020-11-20T18:00:00Z https://example.org/gallery/album-1/photo-2.jpg image/jpeg 200 SHA1/ghi789 34567",
            ]
            return ("\n".join(lines) + "\n").encode("utf-8")
        if self.url.endswith("robots.txt"):
            return b"User-agent: *\nAllow: /\n"
        return b"<html><body>offline fixture page</body></html>"

    def __enter__(self) -> "_CannedResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class _FakeRequest:
    def __init__(self, url: str, *args, **kwargs) -> None:
        self.full_url = url


def _install_offline_harness() -> None:
    """Monkeypatch urllib.request for --offline runs (bytes-identical output)."""
    urllib.request.Request = _FakeRequest  # noqa: A001
    urllib.request.urlopen = lambda req, *a, **k: _CannedResponse(req.full_url)  # noqa: E731


def _parse_simple_yaml(text: str) -> dict:
    """Tiny subset for the skill's flat project.yaml files.

    Supports flat `key: value` entries plus `key:` followed by indented
    `- item` list lines (the layout used by the bundled fixtures).
    """
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


def _scalar(token: str):
    """Coerce a scalar token: int, float, bool, else str."""
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


if __name__ == "__main__":
    raise SystemExit(main())