#!/usr/bin/env python3
"""basic-media-skill pipeline — deterministic side of the archivist workflow.

Stage flow (canonical spec order):
    preflight: robots.txt fail-closed, scope, risk map, published policy
    discovery: DISCOVERY_MANIFEST rows OR a policy-bound live crawl —
               fetch -> classify by headers+magic -> html: parse links
               (final_url + <base href> aware) / media: bytes to storage
    dry-run:   inventory summary, no mass download
    media validation: structural decode (PNG/JPEG/GIF/WebP/PDF/ZIP), sha256,
               atomic store, SQLite state
    recovery:  Wayback CDX/replay closed loop: recovered verified items move
               back into valid (coverage/verdict follow), the queue never leaks
    full-run gate: blocked (exit 3) unless USER_CONFIRMED_FULL_RUN and
               --run-full — no "preliminary ready" fiction

Offline (eval) mode: manifests + canned bodies only; robots/network never hit.

Usage:
    python3 scripts/run_pipeline.py --config path/to/project.yaml --output dir/
    python3 scripts/run_pipeline.py --config path/to/project.yaml --output dir/ --run-full
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import archivist_core as core
import wayback


class LinkExtractor(HTMLParser):
    """Document-link collector: href/src/srcset/data-* plus <base href>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base_href: str | None = None
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:  # noqa: D102
        attrs = dict(attrs)
        if tag == "base" and attrs.get("href"):
            self.base_href = attrs["href"]
            return
        for key in ("href", "src"):
            raw = attrs.get(key)
            if raw and not raw.startswith(("javascript:", "data:", "mailto:", "tel:", "#")):
                self.links.append(raw)
        for key in ("srcset", "data-srcset"):
            srcset = attrs.get(key)
            if srcset:
                for candidate in srcset.split(","):
                    entry = candidate.strip().split(" ", 1)[0]
                    if entry:
                        self.links.append(entry)
        for key in ("data-src", "data-fallback", "data-original"):
            raw = attrs.get(key)
            if raw and not raw.startswith("data:"):
                self.links.append(raw)


class Pipeline:
    """Stateful deterministic side of the archivist workflow."""

    def __init__(self, config: dict, output: Path, run_full: bool, offline: bool = False) -> None:
        self.config = config
        self.output = output.resolve()
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
        self.policy = core.RequestPolicy.from_config(config)
        self.scheduler = core.HostScheduler(self.policy.delay_seconds)
        self.pages: list[dict] = []
        self.media: list[dict] = []
        self.invalid_media: list[dict] = []
        self.recovered_verified: list[dict] = []
        self.summary: dict = {}
        self.recovery_provenance: list[dict] = []

    def validate_config(self) -> None:
        core.validate_config(self.config, offline=self.offline)

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

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ---------------------------------------------------------------- preflight

    def _scope_prefixes(self) -> list[str]:
        explicit = self.config.get("SCOPE_PATHS")
        if explicit:
            return [p if p.startswith("/") else "/" + p for p in explicit]
        scope = self.config.get("SCOPE", "site")
        if scope in ("section", "gallery", "forum", "topics"):
            return ["/"]
        return []

    def _stage_preflight(self) -> dict:
        target = self.config["TARGET_URL"]
        robots = {"url": core.origin_root(target) + "robots.txt", "status": "skipped"}
        if self.offline:
            robots["status"] = "offline"
            robots["note"] = "policy not evaluated in offline mode"
            target_allowed = None
        else:
            fetched = core.fetch_robots_policy(target, user_agent=self.policy.user_agent,
                                               timeout_seconds=min(self.policy.timeout_seconds, 30))
            robots = {k: v for k, v in fetched.items() if k != "parser"}
            robots["authoritative"] = fetched.get("status") == "ok"
            target_allowed = core.robots_allows(target, fetched, self.policy.user_agent) \
                if fetched.get("status") == "ok" else None
            robots["target_allowed"] = target_allowed
            if self.policy.respect_robots and target_allowed is not True:
                robots["policy"] = "blocked: robots unavailable or disallowed (fail-closed)"
            elif self.policy.respect_robots:
                robots["policy"] = "proceed: robots allows target"
            else:
                robots["policy"] = "explicit RESPECT_ROBOTS_TXT=false override (owner permission required)"
        return {
            "target_url": target,
            "normalized": core.normalize_url(target),
            "scope": self.config.get("SCOPE", "site"),
            "scope_paths": self._scope_prefixes(),
            "allowed_domains": self.config.get("ALLOWED_DOMAINS", []),
            "robots": robots,
            "policy_effective": self.policy.to_dict(),
            "risk_map": {"auth_required": bool(self.config.get("AUTH")), "paywall": False,
                         "captcha": False, "rate_limit_hint": self.policy.delay_seconds},
            "risks": (["RESPECT_ROBOTS_TXT=false — only continue with documented owner permission"]
                      if not self.policy.respect_robots else []),
            "engine": {"name": "static/html", "confidence": "high" if not self.offline else "unknown"},
        }

    # ---------------------------------------------------------------- discovery

    def _stage_discovery(self) -> dict:
        """Inventory: DISCOVERY_MANIFEST rows, else a live policy-bound crawl."""
        crawl_notes: list[str] = []
        if self.config.get("DISCOVERY_MANIFEST"):
            items, source = self._manifest_items(), "manifest"
        elif self.offline:
            items, source = [], "manifest-required-offline"
        else:
            items, source, crawl_notes = self._crawl_online()
        self._partition_items(items)
        return {
            "source": source, "notes": crawl_notes, "items": items,
            "url_count": len(items), "page_count": len(self.pages),
            "media_count": len(self.media),
        }

    def _partition_items(self, items: list[dict]) -> None:
        self.pages = [r for r in items if r.get("kind") in ("page", "html", "forum", "topic", "post")]
        self.media = [r for r in items if r.get("kind") in ("media", "document")]

    def _manifest_items(self) -> list[dict]:
        manifest = Path(self.config["DISCOVERY_MANIFEST"])
        rows = []
        if manifest.exists():
            for line in manifest.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("data"):  # fixture bodies arrive hex-encoded
                    try:
                        row["data"] = bytes.fromhex(row["data"])
                    except ValueError:
                        row.pop("data", None)
                row.setdefault("kind", "page")
                rows.append(row)
        return rows

    # ------------------------------------------------------------ live crawl

    def _crawl_online(self) -> tuple[list[dict], str, list[str]]:
        """Policy-bound BFS crawl: robots fail-closed, per-host delay, path
        scope, canonical dedup. Returns (items, source, notes)."""
        notes: list[str] = []
        if self.policy.respect_robots:
            robots = core.fetch_robots_policy(self.config["TARGET_URL"],
                                              user_agent=self.policy.user_agent,
                                              timeout_seconds=min(self.policy.timeout_seconds, 30))
            if robots.get("status") != "ok" or not core.robots_allows(
                    self.config["TARGET_URL"], robots, self.policy.user_agent):
                notes.append("robots unavailable or disallowed — crawl blocked (fail-closed)")
                return [], "crawl-blocked", notes
        allowed = self.config.get("ALLOWED_DOMAINS") or \
            [urllib.parse.urlparse(self.config["TARGET_URL"]).netloc]
        max_pages = int(self.config.get("MAX_PAGES", 100))
        max_depth = int(self.config.get("MAX_DEPTH", 5))
        prefixes = self._scope_prefixes()
        save_html = bool(self.config.get("SAVE_RAW_HTML", True))
        items: list[dict] = []
        seen: set[str] = set()
        queue: list[tuple[str, int]] = [(self.config["TARGET_URL"], 0)]
        while queue and len(items) < max_pages:
            url, depth = queue.pop(0)
            canon = core.canonical_url(url)
            if not canon or canon in seen:
                continue
            seen.add(canon)
            if not core.same_domain(canon, allowed) or not core.path_in_scope(canon, prefixes):
                continue
            if depth > max_depth:
                continue
            fetched = core.fetch(canon, policy=self.policy, scheduler=self.scheduler)
            status = fetched.get("status")
            item = {
                "url": canon,
                "final_url": fetched.get("final_url") or canon,
                "kind": fetched.get("kind") if fetched.get("error") is None else "unknown",
                "depth": depth,
                "status": status if status is not None else fetched.get("error"),
                "content_type": fetched.get("content_type"),
                "detected_type": fetched.get("detected_type"),
                "sha256": fetched.get("sha256"),
                "redirect_chain": fetched.get("redirect_chain"),
            }
            if fetched.get("error") and status is None:
                item["fetch_error"] = fetched["error"]
            body: bytes = fetched.get("body") or b""
            if item["kind"] == "html" and not item.get("fetch_error"):
                if save_html:
                    stem = "".join(ch for ch in urllib.parse.urlparse(canon).path.strip("/") or "index"
                                   if ch.isalnum() or ch in "-_")[:80] or "page"
                    try:
                        core.atomic_store(self.raw_html / f"{stem}-{depth}.html", body)
                    except OSError:
                        pass
                parser = LinkExtractor()
                try:
                    parser.feed(body.decode("utf-8", errors="replace"))
                except Exception:  # noqa: BLE001 — a bad page must never kill the crawl
                    parser = LinkExtractor()
                doc_base = item["final_url"]
                if parser.base_href:
                    # <base href> may itself be relative — join it onto the
                    # page's real final URL before resolving child links
                    try:
                        doc_base = urllib.parse.urljoin(item["final_url"], parser.base_href)
                    except ValueError:
                        doc_base = item["final_url"]
                for raw in parser.links:
                    absolute = core.resolve_url(doc_base, raw)
                    if absolute and core.same_domain(absolute, allowed) \
                            and core.path_in_scope(absolute, prefixes):
                        queue.append((absolute, depth + 1))
            elif item["kind"] in ("media", "document") and body and not item.get("fetch_error"):
                item["data"] = body
                item["media_role"] = "attachment" if item["kind"] == "document" else "image"
            items.append(item)
        notes.append(f"crawl completed: {len(items)} items, {len(seen)} unique URLs seen")
        return items, "crawl", notes

    # ---------------------------------------------------------------- dry run

    def _stage_dry_run(self, discovery: dict) -> dict:
        urls = [m for m in self._iter_manifest_urls() if m.get("url")]
        pageish = [u for u in urls if u.get("kind") in ("page", "html", "forum", "topic", "post")]
        media_items = [u for u in urls if u.get("kind") in ("media", "document")]
        return {
            "strategy": "inventory-only",
            "urls_inspected": len(urls),
            "pages_seen": len(pageish),
            "media_seen": len(media_items),
            "samples_taken": min(len(urls), 5),
            "templates_sampled": len(pageish),
            "pagination_checked": discovery.get("source") != "crawl-blocked",
            "originals_vs_thumbnails": {"checked": False, "note": "needs selector analysis"},
            "decision": "ready" if urls else "needs changes",
            "note": "dry-run: byte probes done, mass download not performed",
        }

    def _iter_manifest_urls(self):
        manifest = self.config.get("DISCOVERY_MANIFEST")
        if manifest and Path(manifest).exists():
            yield from self._manifest_items()
        else:
            yield from self.pages + self.media

    # -------------------------------------------------------- media validation

    def _validate_media_item(self, item: dict) -> tuple[bool, list[str]]:
        if "data" not in item:
            return False, ["no body bytes recorded"]
        res = core.validate_media(status=item.get("status"), content_type=item.get("content_type"),
                                  body=item["data"], role=str(item.get("media_role", "unknown")))
        item["sha256"] = res["sha256"]
        item["validation_errors"] = res["reasons"]
        item["verified_state"] = res["state"]
        item["detected_type"] = res.get("detected") or item.get("detected_type")
        return res["ok"], res["reasons"]

    def _media_storage(self, item: dict) -> dict | None:
        data = item.get("data")
        if not data:
            return None
        ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                   "image/webp": ".webp", "image/x-icon": ".ico",
                   "application/pdf": ".pdf", "application/zip": ".zip"}
        ctype = str(item.get("content_type", "")).split(";")[0].strip().lower()
        ext = ext_map.get(item.get("detected_type") or ctype, ".bin")
        base = urllib.parse.urlparse(item["url"]).path.rsplit("/", 1)[-1]
        stem = "".join(ch for ch in (base or "media") if ch.isalnum() or ch in "-_")[:100] or "media"
        target = self.media_dir / f"{stem}-{item['sha256'][:12]}{ext}"
        try:
            core.atomic_store(target, data)
        except OSError:
            return None
        return {"path": str(target), "verified": True, "sha256": item["sha256"]}

    def _stage_media_validation(self) -> tuple[list[dict], list[dict], int]:
        valid: list[dict] = []
        invalid: list[dict] = []
        by_kind: dict[str, int] = {}
        for item in self.media:
            ok, _ = self._validate_media_item(item)
            by_kind[item.get("media_role", "image")] = by_kind.get(item.get("media_role", "image"), 0) + 1
            item["valid"] = ok
            if ok:
                storage = self._media_storage(item)
                item["storage"] = storage
                item["valid"] = bool(storage and storage.get("verified"))
                if not item["valid"]:
                    item.setdefault("validation_errors", []).append("storage failed")
                    invalid.append(item)
                    continue
                valid.append(item)
            else:
                item.setdefault("validation_errors", []).append("invalid")
                invalid.append(item)
        with sqlite3.connect(self.state_path) as db:
            for rec in valid + invalid:
                stored = rec.get("storage") or {}
                db.execute(
                    "INSERT OR REPLACE INTO urls (url, kind, depth, status, sha256, stored_path, verified, fetched_at)"
                    " VALUES (?, 'media', ?, ?, ?, ?, ?, ?)",
                    (rec["url"], 2, str(rec.get("status")), rec.get("sha256"),
                     stored.get("path"), 1 if rec["valid"] else 0, self._now_iso()),
                )
            db.commit()
        return valid, invalid, len(by_kind)

    # ---------------------------------------------------------- recovery queue

    def _stage_recovery_queue(self, invalid: list[dict]) -> tuple[dict, list[dict]]:
        """Wayback recovery with a closed loop: recovered verified items move
        back into valid (coverage/verdict follow); the rest stay unresolved."""
        entries: list[dict] = []
        prov_lines: list[dict] = []
        self.recovered_verified = []
        counts = {"pending": 0, "recovered_verified": 0, "thumbnail_only": 0,
                  "placeholder": 0, "ambiguous": 0, "unresolved": 0}
        for item in invalid:
            if self.offline:
                counts["pending"] += 1
                entries.append({
                    "url": item["url"], "source": "live_validation",
                    "failure_reason": item["validation_errors"],
                    "wayback": {"offline": True, "status": "queued",
                                "cdx_endpoint": wayback.CDX_ENDPOINT,
                                "captures_checked": 0, "outcome": "unresolved"},
                })
                continue
            date = item.get("source_date") or self.config.get("TARGET_PUBLISHED_AT")
            since = until = None
            if isinstance(date, str) and len(date) >= 10:
                since = until = date[:10]  # ranking preference; wayback queries WIDE
            rec = wayback.recover_and_store(
                item["url"], self.media_dir, since=since, until=until,
                capture_limit=int(self.config.get("RECOVERY_CAPTURE_LIMIT") or 500),
                offline=False, source_page_url=self.config.get("SOURCE_PAGE_URL"))
            state = wayback.recovery_state(rec)
            counts[state if state in counts else "ambiguous"] += 1
            prov_lines.append({
                "source_url": item["url"], "source_role": item.get("media_role"),
                "live_status": item.get("status"), "live_reason": item.get("validation_errors"),
                "captures_queried": rec.get("captures_queried"),
                "captures_deduped": rec.get("captures_deduped"),
                "candidates_checked": rec.get("candidates_checked"),
                "outcome": state, "capture": rec.get("capture"),
                "stored": rec.get("stored"),
                "fallback_attempted": rec.get("fallback_attempted", False),
                "errors": rec.get("errors", []),
            })
            entries.append({
                "url": item["url"], "source": "live_validation",
                "failure_reason": item["validation_errors"],
                "wayback": {"offline": False, "status": "processed",
                            "captures_checked": rec.get("candidates_checked", 0),
                            "outcome": state,
                            "recovered_original": state == "recovered_verified",
                            "recovered_thumbnail_only": state == "thumbnail_only",
                            "unresolved": state == "unresolved",
                            "capture": rec.get("capture")},
            })
            if rec.get("recovered") and rec.get("body") and state == "recovered_verified":
                recovered = dict(item)
                recovered["valid"] = True
                recovered["storage"] = {"path": rec.get("stored"), "verified": True,
                                        "sha256": rec["capture"]["sha256"]}
                recovered["recovered_from"] = rec["capture"]
                recovered["validation_errors"] = rec.get("errors", [])
                self.recovered_verified.append(recovered)
        return {"queue_size": len(entries), "processed": True, "offline": self.offline,
                "counts": counts}, prov_lines

    # -------------------------------------------------------------- coverage

    def _kind_bucket(self, item: dict) -> str:
        kind = item.get("kind")
        role = item.get("media_role")
        if kind in ("forum", "topic", "post", "author"):
            return "forum_entities"
        if kind == "document":
            return "attachments" if role == "attachment" else "documents"
        if kind == "media":
            return "media"
        return "pages"

    def _coverage(self, discovery: dict) -> dict:
        discovered = discovery.get("url_count", 0)
        total = len(self.pages) + len(self.media)
        verified = sum(1 for m in self.pages + self.media
                       if m.get("valid") or m.get("recovered_from"))
        coverage = f"{verified / total:.4f}" if total else "n/a"
        kinds: dict[str, dict] = {}
        for kind in ("pages", "media", "documents", "attachments", "forum_entities"):
            items = self.pages + self.media
            bucket = [m for m in items if self._kind_bucket(m) == kind]
            n = len(bucket)
            verified_kind = len([m for m in bucket if m.get("valid") or m.get("recovered_from")])
            kinds[kind] = {"discovered": n if n else None,
                           "verified": verified_kind if n else None,
                           "coverage": f"{verified_kind / n:.4f}" if n else "n/a"}
        return {"coverage": coverage, "coverage_by_kind": kinds}

    # ------------------------------------------------------------ gate / report

    def _stage_test_report(self, preflight: dict, discovery: dict, dry: dict,
                           valid: list[dict], invalid: list[dict],
                           recovery: dict) -> dict:
        media_invalid = len(invalid) - len(self.recovered_verified)
        verdict = "ready" if media_invalid <= 0 and discovery.get("url_count", 0) > 0 \
            else "needs changes"
        return {
            "verdict": verdict,
            "pages": len(self.pages),
            "media_discovered": len(self.media),
            "media_validated": len(valid) + len(self.recovered_verified),
            "media_invalid": max(media_invalid, 0),
            "recovery_queue_size": recovery.get("queue_size", 0),
            "recovery_counts": recovery.get("counts", {}),
            "blocking_risks": preflight.get("risks", []),
        }

    def _stage_full_run_gate(self) -> dict:
        """Honest full-run gate: exit 3 unless confirmation AND --run-full."""
        if not self._confirm and not self.run_full_flag:
            return {"action": "stop", "blocked": True,
                    "reason": "USER_CONFIRMED_FULL_RUN must be true AND --run-full must be passed",
                    "exit_code": 0}
        if not self._confirm:
            return {"action": "stop", "blocked": True,
                    "reason": "USER_CONFIRMED_FULL_RUN=false — full run not authorized", "exit_code": 3}
        if not self.run_full_flag:
            return {"action": "stop", "blocked": True,
                    "reason": "--run-full not passed — refusing a full run", "exit_code": 3}
        return {"action": "proceed", "blocked": False,
                "reason": "confirmation and flag present; deterministic mass downloader required",
                "exit_code": 3}

    def _write_manifest(self, items: list[dict]) -> None:
        existing: dict[str, dict] = {}
        if self.manifest_path.exists():
            for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                existing[row.get("url", "")] = row  # merge, never clobber
        for item in items:
            if "data" in item:  # bytes stay out of the published manifest
                item = {k: v for k, v in item.items() if k != "data"}
            key = item.get("url", "")
            existing[key] = {**existing.get(key, {}), **item}
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("w", encoding="utf-8") as fh:
            for key in sorted(existing):
                fh.write(json.dumps(existing[key], ensure_ascii=False) + "\n")

    def _report_extras(self) -> dict:
        """Extra top-level report fields for skill subclasses (default none)."""
        return {}

    def _write_reports(self, preflight: dict, discovery: dict, dry: dict,
                       valid: list[dict], invalid: list[dict], recovery: dict,
                       report: dict, full: dict) -> None:
        coverage = self._coverage(discovery)
        self._write_manifest(discovery.get("items", []))
        extras = self._report_extras()
        extras.pop("output", None)  # skill subclasses must not clobber output
        test_payload = {
            "stage": "test_report",
            "dry_run": full["blocked"],
            "confirmed": self._confirm,
            "run_full_flag": self.run_full_flag,
            "gate": full["action"],
            "gate_reason": full["reason"],
            "decision": report["verdict"],
            "urls_discovered": discovery.get("url_count", 0),
            "pages": report["pages"],
            "media_discovered": report["media_discovered"],
            "media_valid": report["media_validated"],
            "media_invalid": report["media_invalid"],
            "recovery_queue_size": report["recovery_queue_size"],
            "recovery_counts": report["recovery_counts"],
            "coverage": coverage["coverage"],
            "coverage_by_kind": coverage["coverage_by_kind"],
            "policy_effective": preflight.get("policy_effective", {}),
            "robots": preflight.get("robots", {}),
            "scope": preflight.get("scope"),
            "output": {
                "dir": str(self.root),
                "state": str(self.state_path),
                "raw_html": str(self.raw_html),
                "media_dir": str(self.media_dir),
                "manifest": str(self.manifest_path),
            },
        }
        test_payload.update(extras)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(test_payload, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        try:
            self.reports.mkdir(parents=True, exist_ok=True)
            (self.reports / "coverage.json").write_text(
                json.dumps({"coverage": coverage, "accounted_by_kind": report}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            (self.reports / "recovery_provenance.jsonl").write_text(
                "\n".join(json.dumps(p, ensure_ascii=False) for p in self.recovery_provenance) + "\n",
                encoding="utf-8")
        except OSError:
            pass

    def _write_summary(self) -> None:
        self.summary["written_at"] = self._now_iso()
        (self.root / "summary.json").write_text(
            json.dumps(self.summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_resume(self) -> None:
        resume = {
            "resume": True,
            "state_path": str(self.state_path),
            "manifest_path": str(self.manifest_path),
            "continue_command": (f"python scripts/run_pipeline.py --config "
                                 f"{self.config.get('DISCOVERY_MANIFEST') or 'config.yaml'} "
                                 f"--output {self.output}"),
            "next_stage": "recovery" if self.invalid_media else "done",
        }
        (self.root / "resume.json").write_text(
            json.dumps(resume, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ------------------------------------------------------------ orchestration

    def run(self) -> dict:
        self.validate_config()
        self._create_layout()
        preflight = self._stage_preflight()
        discovery = self._stage_discovery()
        dry = self._stage_dry_run(discovery)
        valid, invalid, _ = self._stage_media_validation()
        self.invalid_media = invalid
        recovery, self.recovery_provenance = self._stage_recovery_queue(invalid)
        report = self._stage_test_report(preflight, discovery, dry, valid, invalid, recovery)
        full = self._stage_full_run_gate()
        self.summary = {
            "stage": "summary",
            "preflight": preflight,
            "discovery": {k: v for k, v in discovery.items() if k != "items"},
            "dry_run": dry,
            "media_validation": {"validated": len(valid) + len(self.recovered_verified),
                                 "invalid": len(invalid) - len(self.recovered_verified)},
            "recovery": {"queue_size": recovery["queue_size"], "processed": recovery["processed"],
                         "counts": recovery["counts"]},
            "test_report": report,
            "full_run_gate": full,
        }
        self._write_reports(preflight, discovery, dry, valid, invalid, recovery, report, full)
        self._write_summary()
        self._write_resume()
        return self.summary


def main() -> int:
    parser = argparse.ArgumentParser(description="basic-media archivist pipeline")
    parser.add_argument("--config", required=True, dest="config",
                        help="project YAML/JSON path OR a directory containing config.yaml + discovered_urls.jsonl")
    parser.add_argument("--input", dest="config",
                        help="alias for --config (eval runner compatibility)")
    parser.add_argument("--output", default="test_report.json",
                        help="write the canonical test report to this exact file path")
    parser.add_argument("--run-full", action="store_true",
                        help="request a full run (requires USER_CONFIRMED_FULL_RUN=true)")
    parser.add_argument("--offline", action="store_true",
                        help="no network calls; canned responses (eval-safe)")
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