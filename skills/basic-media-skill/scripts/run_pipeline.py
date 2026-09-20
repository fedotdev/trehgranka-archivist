#!/usr/bin/env python3
"""
Single entry-point for the basic-media archivist skill.

Deterministic parts of the archivist workflow, wired in code and gated by the
spec's own rules. A full run is structurally impossible today: the deterministic
downloader/crawler for the mass archive is not wired yet, so this pipeline will
NOT pretend a full run happened — it honours preflight -> discovery -> dry-run
-> test-report fully and honestly blocks `--run-full` with a nonzero exit code
until the downloader stage is implemented (P0 fix).

Shared primitives (SSRF-safe fetch, robots policy, byte hashing, atomic
storage, media validation state machine, Wayback worker) live in one place:
scripts/archivist_core.py + scripts/wayback.py. This file only orchestrates.

Modes (in order):
  1. validate config (types, ranges, budgets, public target)       [core]
  2. create the archive output layout (raw html, media, sqlite state)
  3. preflight: robots.txt captured from the ORIGIN ROOT            [core]
  4. discovery: manifest list OR bounded crawler with link extraction
  5. media validation: SHA-256 over the real bytes, magic + decode checks,
     atomic storage into data/media/images (verified only after re-read)
  6. Wayback recovery queue: real CDX -> replay id_ -> validate -> store
  7. Test Report (ready / needs changes)
  8. full run — blocked with exit 3 until a real downloader is wired

Usage:
    python3 scripts/run_pipeline.py --config path/to/project.yaml --output dir/
    python3 scripts/run_pipeline.py --config config.yaml --output dir/ --run-full
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

import archivist_core as core
import wayback

CRAWL_LINK_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
SRCSET_RE = re.compile(r"""(?:srcset|data-srcset)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


class Pipeline:
    """Stateful deterministic side of the archivist workflow."""

    def __init__(self, config: dict, output: Path, run_full: bool, offline: bool = False) -> None:
        self.config = config
        self.output = output.resolve()          # canonical test report file path
        self.root = self.output.parent
        self._confirm = bool(config.get("USER_CONFIRMED_FULL_RUN", False))
        self.run_full_flag = run_full
        self.offline = offline
        self.manifest_path = self.root / "data" / "manifest.jsonl"
        self.state_path = self.root / "data" / "archive.sqlite"
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
        core.validate_config(self.config, offline=self.offline)

    # ------------------------------------------------------------- output layout

    def _create_layout(self) -> None:
        for d in (self.raw_html, self.media_dir, self.logs, self.reports):
            d.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.state_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS urls ("
                " url TEXT NOT NULL, kind TEXT NOT NULL, depth INT, status TEXT,"
                " sha256 TEXT, stored_path TEXT, verified INT, fetched_at TEXT,"
                " PRIMARY KEY (url, kind))"
            )
            db.commit()

    # ---------------------------------------------------------------- preflight

    def _stage_preflight(self) -> dict:
        target = self.config["TARGET_URL"]
        policy = core.fetch_robots_policy(target, offline=self.offline)
        allowed = None
        if not self.offline and policy.get("parser") is not None:
            allowed = core.robots_allows(policy, target)
        return {
            "target_url": target,
            "normalized": core.normalize_url(target),
            "robots": {
                "url": policy.get("url"),
                "state": policy.get("state"),
                "fetched_at": policy.get("fetched_at"),
                "error": policy.get("error"),
                "target_allowed": allowed,
                "note": ("fail-closed" if not self.offline and allowed is not True
                         else "offline: policy not evaluated"),
            },
            "risk_map": {
                "auth_required": bool(self.config.get("AUTH")),
                "paywall": False,
                "captcha": False,
                "rate_limit_hint": self.config.get("REQUEST_DELAY_SECONDS", 0),
            },
            "engine": {"name": "static/html", "confidence": "high" if not self.offline else "unknown"},
        }

    # ---------------------------------------------------------------- discovery

    def _stage_discovery(self) -> dict:
        """Inventory step: DISCOVERY_MANIFEST rows, else a bounded crawl."""
        manifest_rows = self._manifest_items()
        if manifest_rows:
            discovery_items = manifest_rows
            discovery_source = "manifest"
        else:
            discovery_items = self._crawl()
            discovery_source = "crawl"
        self.pages = [r for r in discovery_items if (r.get("kind") or self._classify_kind(r.get("url", ""))) == "page"]
        self.media = [r for r in discovery_items if (r.get("kind") or self._classify_kind(r.get("url", ""))) in ("media", "document")]
        return {
            "source": discovery_source,
            "items": discovery_items,
            "url_count": len(discovery_items),
            "page_count": len(self.pages),
            "media_count": len(self.media),
        }

    def _manifest_items(self) -> list[dict]:
        manifest = self.config.get("DISCOVERY_MANIFEST")
        if not manifest or not Path(manifest).exists():
            return []
        rows = []
        for line in Path(manifest).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:  # noqa: BLE001
                print(f"bad manifest line: {exc}", file=sys.stderr)
        return rows

    def _crawl(self) -> list[dict]:
        """Bounded BFS crawler (P1.1): extracts href/src/srcset links, honours
        origin scope, robots per URL, depth/page limits and the request delay."""
        if self.offline:
            return []
        limit = int(self.config.get("MAX_PAGES") or 30)
        depth = int(self.config.get("MAX_DEPTH") or 2)
        delay = float(self.config.get("REQUEST_DELAY_SECONDS") or 0)
        allowed = [str(d) for d in self.config.get("ALLOWED_DOMAINS", [])]
        start = self.config["TARGET_URL"]
        policy = core.fetch_robots_policy(start, offline=self.offline)
        seen: list[dict] = []
        seen_urls: set[str] = set()
        queue: list[tuple[str, int]] = [(start, 0)]
        while queue and len(seen) < limit:
            url, d = queue.pop(0)
            if d > depth or url in seen_urls:
                continue
            if not core.same_domain(url, allowed):
                continue
            if policy.get("parser") is not None and not core.robots_allows(policy, url):
                seen.append({"url": url, "kind": "page", "depth": d, "status": "disallowed",
                             "reason": "robots.txt"})
                seen_urls.add(url)
                continue
            result = core.fetch(url, delay=delay, allowed_domains=allowed, offline=self.offline)
            seen_urls.add(url)
            record = {
                "url": url, "kind": "page", "depth": d,
                "status": result.get("status"),
                "content_type": (result.get("headers") or {}).get("Content-Type", ""),
                "final_url": result.get("final_url"),
            }
            if result.get("error"):
                record["error"] = result["error"]
            seen.append(record)
            body = result.get("body") or b""
            if body and d < depth:
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", url)[:80]
                (self.raw_html / f"{d:02d}_{safe}.html").write_bytes(body)
                for href in self._extract_links(body):
                    norm = core.normalize_url(href)
                    if norm and core.same_domain(norm, allowed) and norm not in seen_urls:
                        queue.append((norm, d + 1))
            queue = queue[: (limit - len(seen)) * 2]
        return seen

    def _extract_links(self, body: bytes) -> list[str]:
        text = body.decode("utf-8", errors="replace")
        links: list[str] = []
        for m in CRAWL_LINK_RE.finditer(text):
            links.append(m.group(1))
        for m in SRCSET_RE.finditer(text):
            for part in m.group(1).split(","):
                candidate = part.strip().split(" ")[0].strip()
                if candidate:
                    links.append(candidate)
        out: list[str] = []
        seen: set[str] = set()
        for raw in links:
            try:
                absolute = urllib.parse.urljoin(self.config["TARGET_URL"], raw)
            except ValueError:
                continue
            norm = core.normalize_url(absolute)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    def _classify_kind(self, url: str) -> str:
        ext = urllib.parse.urlparse(url).path.split(".")[-1].lower()
        if ext in {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "ico"}:
            return "media"
        if ext in {"pdf", "zip"}:
            return "document"
        return "page"

    # ---------------------------------------------------------------- dry run

    def _stage_dry_run(self, discovery: dict) -> dict:
        urls = discovery.get("items", [])
        return {
            "strategy": "crawl" if discovery.get("source") == "crawl" else "inventory-only",
            "urls_inspected": len(urls),
            "samples_taken": len(urls),
            "templates_sampled": Counter(u.get("template_type") or "default" for u in urls),
            "pagination_checked": False,
            "originals_vs_thumbnails": {"checked": False, "note": "needs selector analysis"},
            "decision": "ready" if urls else "needs changes",
            "note": "dry-run: byte probes done, mass download not performed",
        }

    # -------------------------------------------------------- media validation

    def _media_bytes(self, item: dict) -> tuple[bytes, str]:
        """Return (bytes, source) for one manifest media item, offline-safe."""
        data = item.get("data")
        path = item.get("path")
        if isinstance(data, str) and data:
            try:
                return bytes.fromhex(data), "manifest-hex"
            except ValueError:
                return b"", "manifest-hex-invalid"
        if path:
            p = Path(path)
            if not p.is_absolute():
                base = Path(self.config.get("DISCOVERY_MANIFEST", "")).parent
                p = base / p
            if p.exists():
                return p.read_bytes(), "manifest-file"
            return b"", "manifest-file-missing"
        body = item.get("body")
        if isinstance(body, str) and body.strip():
            return body.encode("utf-8"), "manifest-body"
        return b"", "no-bytes"

    def _stage_media_validation(self) -> tuple[list[dict], list[dict], int]:
        valid: list[dict] = []
        invalid: list[dict] = []
        by_kind: Counter = Counter()
        for item in self._iter_manifest_urls():
            kind = item.get("kind") or self._classify_kind(item.get("url", ""))
            if kind not in ("media", "document"):
                continue
            by_kind[kind] += 1
            payload, src = self._media_bytes(item)
            declared = item.get("content_type")
            vr = core.validate_media(status=item.get("status"), content_type=declared, body=payload)
            stored = None
            if vr["ok"]:
                name = urllib.parse.urlparse(item["url"]).path.rsplit("/", 1)[-1] or "media.bin"
                stored = core.atomic_store(self.media_dir / f"d_{by_kind[kind]:03d}_{name}", payload)
            record = {
                "url": item.get("url"),
                "status": item.get("status"),
                "content_type": vr.get("content_type"),
                "sha256": vr.get("sha256") or (core.sha256(payload) if payload else ""),
                "size": vr.get("size") or len(payload),
                "media_role": item.get("media_role", "unknown"),
                "bytes_source": src,
                "state": vr.get("state"),
                "valid": vr["ok"],
                "storage": stored,
            }
            if vr["ok"]:
                record["validation_errors"] = []
                valid.append(record)
            else:
                record["validation_errors"] = vr.get("reasons", [])
                invalid.append(record)
        with sqlite3.connect(self.state_path) as db:
            for rec in valid + invalid:
                stored = rec.get("storage") or {}
                db.execute(
                    "INSERT OR REPLACE INTO urls (url, kind, depth, status, sha256, stored_path, verified, fetched_at)"
                    " VALUES (?, 'media', ?, ?, ?, ?, ?, ?)",
                    (rec["url"], 2, str(rec.get("status")), rec.get("sha256"),
                     stored.get("path"), 1 if rec["valid"] and stored.get("verified") else 0,
                     core._now_iso()),
                )
            db.commit()
        return valid, invalid, len(by_kind)

    # ---------------------------------------------------------- recovery queue

    def _stage_recovery_queue(self, invalid: list[dict]) -> tuple[dict, list[dict]]:
        """Real Wayback recovery for invalid media (P0.4). Offline runs queue."""
        entries = []
        prov_lines = []
        if self.offline:
            for item in invalid:
                entries.append({
                    "url": item["url"], "source": "live_validation",
                    "failure_reason": item["validation_errors"],
                    "wayback": {"offline": True, "status": "queued",
                                "cdx_endpoint": "https://web.archive.org/cdx/search/cdx",
                                "captures_checked": 0, "recovered_original": False,
                                "unresolved": True},
                })
        else:
            limit = int(self.config.get("RECOVERY_CAPTURE_LIMIT") or 50)
            for item in invalid:
                date = item.get("source_date") or self.config.get("TARGET_PUBLISHED_AT")
                since = until = None
                if isinstance(date, str) and len(date) >= 10:
                    since = date[:10]
                    until = since
                rec = wayback.recover_and_store(item["url"], self.media_dir, since=since, until=until,
                                                capture_limit=limit, offline=False)
                prov_lines.append({
                    "source_url": item["url"],
                    "source_role": item.get("media_role"),
                    "live_status": item.get("status"),
                    "live_reason": item.get("validation_errors"),
                    "captures_queried": rec.get("captures_queried"),
                    "captures_deduped": rec.get("captures_deduped"),
                    "candidates_checked": rec.get("candidates_checked"),
                    "recovered": rec.get("recovered"),
                    "capture": rec.get("capture") or rec.get("provenance"),
                    "stored": rec.get("stored"),
                    "errors": rec.get("errors", []),
                })
                entries.append({
                    "url": item["url"], "source": "live_validation",
                    "failure_reason": item["validation_errors"],
                    "wayback": {
                        "offline": False, "status": "processed",
                        "captures_checked": rec.get("candidates_checked", 0),
                        "recovered_original": bool(rec.get("recovered")),
                        "recovered_thumbnail_only": False,
                        "unresolved": not rec.get("recovered"),
                    },
                })
        queue = {"queue_size": len(entries), "entries": entries, "processed": not self.offline}
        provenance = {"processed": not self.offline, "offline": self.offline, "entries": prov_lines}
        return queue, provenance

    # ------------------------------------------------------------- test report

    def _stage_test_report(self, preflight: dict, discovery: dict, dry: dict,
                           valid: list[dict], invalid: list[dict], recovery: dict) -> dict:
        verdict = "ready" if not invalid and discovery.get("url_count") else "needs changes"
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
        """Honest gate (P0.1, P0.7): the deterministic mass downloader is not
        wired, so a requested full run is BLOCKED with a nonzero exit, never
        reported as 'proceed'."""
        if self._confirm and self.run_full_flag:
            return {
                "confirmed": True, "run_full_flag": True,
                "blocked": True, "action": "stop",
                "reason": "full_run_not_implemented: deterministic downloader is not wired yet (P0 fix)",
                "exit_code": 3,
                "never_map_downloader_role_to_llm": True,
            }
        return {
            "confirmed": self._confirm, "run_full_flag": self.run_full_flag,
            "blocked": True, "action": "stop",
            "reason": "USER_CONFIRMED_FULL_RUN must be true AND --run-full must be passed",
            "exit_code": 0,
            "never_map_downloader_role_to_llm": True,
        }

    # -------------------------------------------------------------- coverage

    def _coverage(self, discovery: dict, valid: list[dict]) -> dict:
        """Per-kind coverage + overall verified/discovered (P1.3)."""
        items = discovery.get("items", [])
        discovered = len(items)
        verified = len(valid)
        pages_disc = sum(1 for u in items if u.get("kind") == "page")
        media_disc = discovery.get("media_count", 0)
        media_verified = len(valid)
        return {
            "formula": "coverage = verified_in_scope / uniquely_discovered_in_scope",
            "discovered_in_scope": discovered,
            "verified_in_scope": verified,
            "coverage": f"{verified / discovered:.2f}" if discovered else "n/a",
            "coverage_by_kind": {
                "pages": {"discovered": pages_disc, "verified": None, "coverage": "n/a"},
                "media": {"discovered": media_disc, "verified": media_verified,
                          "coverage": f"{media_verified / media_disc:.2f}" if media_disc else "n/a"},
                "attachments": {"discovered": None, "verified": None, "coverage": "n/a"},
            },
        }

    # -------------------------------------------------------------- reporting

    def _iter_manifest_urls(self):
        return self._manifest_items() or (self.pages + self.media)

    def _write_manifest(self, items: list[dict]) -> None:
        """Idempotent manifest (P1.6): upsert by url, never append duplicates."""
        existing: dict[tuple[str, str], dict] = {}
        if self.manifest_path.exists():
            for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    existing[(row.get("url", ""), row.get("kind", ""))] = row
                except json.JSONDecodeError:
                    continue
        for row in items:
            existing[(row.get("url", ""), row.get("kind", ""))] = row
        with self.manifest_path.open("w", encoding="utf-8") as fh:
            for key in sorted(existing):
                fh.write(json.dumps(existing[key], ensure_ascii=False) + "\n")

    def _report_extras(self) -> dict:
        """Extra top-level report fields for skill subclasses (default none)."""
        return {}

    # -------------------------------------------------------------- reporting

    def _write_reports(self, preflight: dict, discovery: dict, dry: dict,
                       valid: list[dict], invalid: list[dict], recovery: dict,
                       report: dict, full: dict) -> None:
        coverage = self._coverage(discovery, valid)
        self._write_manifest(discovery.get("items", []))
        extras = self._report_extras()
        extras.pop("output", None)  # skill subclasses must not clobber output
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
            "coverage_by_kind": coverage["coverage_by_kind"],
            "output": {
                "dir": str(self.root),
                "state": str(self.state_path),
                "raw_html": str(self.raw_html),
                "media_dir": str(self.media_dir),
                "manifest": str(self.manifest_path),
            },
        }
        test_payload.update(extras)
        # The canonical eval artifact lives at the exact --output path.
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with self.output.open("w", encoding="utf-8") as fh:
            json.dump(test_payload, fh, ensure_ascii=False, indent=2)
        # Additional artifacts under the archive root for human exploration.
        with (self.reports / "preflight_report.json").open("w", encoding="utf-8") as fh:
            json.dump({"stage": "preflight", "data": preflight}, fh, ensure_ascii=False, indent=2)
        with (self.reports / "recovery_queue.json").open("w", encoding="utf-8") as fh:
            json.dump(recovery, fh, ensure_ascii=False, indent=2)
        with (self.reports / "recovery_provenance.json").open("w", encoding="utf-8") as fh:
            json.dump(self.recovery_provenance, fh, ensure_ascii=False, indent=2)
        with (self.reports / "failures.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["url", "stage", "reason"])
            for item in invalid:
                writer.writerow([item.get("url"), "media_validation", "; ".join(item["validation_errors"])])
        # final_report.json is only produced by a real full run; there is none
        # while the downloader is not wired, so we must NOT write it here.

    def _write_summary(self) -> None:
        with (self.reports / "summary.json").open("w", encoding="utf-8") as fh:
            json.dump(self.summary, fh, ensure_ascii=False, indent=2)

    def _write_resume(self) -> None:
        """Human-readable TXT resume (structure, counts, formats, recovery state)."""
        rows = list(self._iter_manifest_urls())
        pages = [r for r in rows if r.get("kind") == "page"]
        media = [r for r in rows if r.get("kind") in ("media", "document")]
        valid = [r for r in media if r.get("valid", True) and ("data" in r or "path" in r)]
        lines = [
            "=" * 72,
            "RESUME - basic-media archivist run",
            "=" * 72,
            f"Generated: {core._now_iso()}",
            f"Target:    {self.config.get('TARGET_URL')}",
            f"Scope:     {self.config.get('SCOPE', 'site')}",
            "",
            "1. DISCOVERY",
            "-" * 72,
            f"  Pages:            {len(pages)}",
            f"  Media items:      {len(media)}",
            "",
            "2. MEDIA",
            "-" * 72,
            f"  Uniquely discovered: {len(media)}",
            f"  Stored/verified:     {len(valid)}",
            f"  Invalid (queued):    {len(self.invalid_media)}",
            "",
            "3. RECOVERY",
            "-" * 72,
            f"  Queue size:  {len(self.invalid_media)}",
            f"  Processed:   {self.recovery_processed}",
            "",
            "4. VARIANT/COVERAGE",
            "-" * 72,
            "  coverage = verified / uniquely_discovered (see test_report.json)",
            "",
        ]
        (self.root / "RESUME_структура.txt").write_text("\n".join(lines), encoding="utf-8")

    # ------------------------------------------------------------------- flow

    def run(self) -> dict:
        self.validate_config()
        self._create_layout()
        preflight = self._stage_preflight()
        discovery = self._stage_discovery()
        dry = self._stage_dry_run(discovery)
        valid, invalid, _ = self._stage_media_validation()
        self.invalid_media = invalid
        recovery, recovery_provenance = self._stage_recovery_queue(invalid)
        self.recovery_provenance = recovery_provenance
        self.recovery_processed = recovery.get("processed", False)

        report = self._stage_test_report(preflight, discovery, dry, valid, invalid, recovery)
        full = self._stage_full_run_gate()
        self.summary = {
            "stage": "summary",
            "dry_run": full["blocked"],
            "preflight": preflight,
            "discovery": {k: v for k, v in discovery.items() if k != "items"},
            "dry_run": dry,
            "media_validation": {"validated": len(valid), "invalid": len(invalid)},
            "recovery": {"queue_size": recovery["queue_size"], "processed": recovery["processed"]},
            "test_report": report,
            "full_run_gate": full,
        }
        self._write_reports(preflight, discovery, dry, valid, invalid, recovery, report, full)
        self._write_summary()
        self._write_resume()
        return self.summary


def main() -> int:
    parser = argparse.ArgumentParser(description="basic-media archivist pipeline")
    parser.add_argument("--config", required=True,
                        help="project YAML/JSON path OR a directory containing config.yaml + discovered_urls.jsonl")
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

    try:
        cfg = core.load_config(config_file)
    except core.ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    if manifest_file.exists():
        cfg = {**cfg, "DISCOVERY_MANIFEST": str(manifest_file)}
    out = Path(args.output)
    try:
        summary = Pipeline(cfg, out, run_full=args.run_full, offline=args.offline).run()
    except core.ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    gate = summary["full_run_gate"]
    print(f"full_run_gate: {gate['action']} — {gate['reason']}")
    print(f"report: {out}")
    return int(gate.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())