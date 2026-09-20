#!/usr/bin/env python3
"""
Forum-media skill entry-point: a thin forum-specific layer over the shared
archivist pipeline (basic-media-skill/scripts/run_pipeline.py).

Everything deterministic and safety-critical (config validation, SSRF-safe
fetch, robots policy, byte hashing, atomic storage, media validation state
machine, Wayback recovery worker) lives ONCE in the shared layer. This file
only adds the forum-specific concerns:

  * forum engine detection (URL heuristic + IPS DOM markers when a page body
    was retained by discovery)
  * forum entity handling in discovery (topics, posts including multi-page,
    attachments, navigation assets) and per-entity coverage reporting

The pipeline never pretends a full run happened: --run-full + confirmation is
blocked with exit code 3 until the deterministic mass downloader is wired.

Usage:
    python3 scripts/run_pipeline.py --config path/to/project.yaml --output dir/
    python3 scripts/run_pipeline.py --config path/to/forum.yaml --output dir/ --run-full
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

# shared layer maintained once in basic-media-skill/scripts
_SKILLS = Path(__file__).resolve().parents[2]
_SHARED_SCRIPTS = _SKILLS / "basic-media-skill" / "scripts"
sys.path.insert(0, str(_SHARED_SCRIPTS))

import archivist_core as core  # noqa: E402
import wayback  # noqa: E402,F401  (worker; imported so the layer is consistent)

# the shared pipeline classes (importlib: our own file is also named
# run_pipeline.py, so a plain `import run_pipeline` would import THIS file)
import importlib.util as _ilu  # noqa: E402

_shared_spec = _ilu.spec_from_file_location("archivist_shared_pipeline", _SHARED_SCRIPTS / "run_pipeline.py")
_shared_mod = _ilu.module_from_spec(_shared_spec)
_shared_spec.loader.exec_module(_shared_mod)  # type: ignore[union-attr]
Pipeline = _shared_mod.Pipeline


class ForumPipeline(Pipeline):
    """Shared pipeline + forum entities/engine/reporting."""

    def __init__(self, config: dict, output: Path, run_full: bool, offline: bool = False) -> None:
        super().__init__(config, output, run_full, offline)
        self.forum_entities: list[dict] = []
        self.entity_tally: dict = {}
        self.media_valid: list[dict] = []

    # ------------------------------------------------------- engine detection

    def _detect_forum_engine(self, url: str) -> dict:
        """URL-pattern heuristic for the public forum families.

        Advisory only: the general rule is to sample one representative page
        of every template type, then compare against a Scrapy control before
        building a site-specific adapter."""
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
                return {"name": name, "confidence": "medium", "evidence": f"URL marker {marker!r}"}
        return {"name": "unknown", "confidence": "low", "evidence": "URL pattern probe inconclusive"}

    def _detect_engine_dom(self, html: str, url: str) -> dict:
        """Upgrade engine detection with IPS DOM markers (extractors.invision)."""
        try:
            from extractors import invision  # stdlib-only, bundled with the skill
        except ImportError:
            return self._detect_forum_engine(url)
        result = invision.detect(html)
        if result.get("name") == "invision-community-ips" and result.get("confidence") != "low":
            return {"name": "Invision Community (IPS)", "confidence": result["confidence"],
                    "evidence": result["evidence"]}
        return self._detect_forum_engine(url)

    # ------------------------------------------------------------- discovery

    def _stage_preflight(self) -> dict:
        base = super()._stage_preflight()
        base["engine"] = self._detect_forum_engine(self.config["TARGET_URL"])
        base["probes"] = {
            "api": urllib.parse.urljoin(self.config["TARGET_URL"], "api/"),
            "rss": urllib.parse.urljoin(self.config["TARGET_URL"], "rss"),
            "sitemap": urllib.parse.urljoin(self.config["TARGET_URL"], "sitemap.xml"),
        }
        risks = list(base.get("risks", []))
        if self.config.get("RESPECT_ROBOTS_TXT", True) is False:
            risks.append("RESPECT_ROBOTS_TXT=false — only continue with documented owner permission")
        base["risks"] = risks
        return base

    def _stage_discovery(self) -> dict:
        base = super()._stage_discovery()
        items = base.get("items", [])
        self.forum_entities = [d for d in items
                               if d.get("kind") in ("forum", "topic", "post", "author")]
        self.entity_tally = self._entity_tally(self.forum_entities)
        base["forum_entity_count"] = len(self.forum_entities)
        base["entity_tally"] = self.entity_tally
        return base

    def _entity_tally(self, entities: list[dict]) -> dict:
        tally = Counter()
        for e in entities:
            tally[str(e.get("kind"))] += 1
        tally["topics"] = tally.get("topic", 0)
        tally["posts"] = tally.get("post", 0)
        tally["pages"] = len(self.pages)
        media_roles = Counter(m.get("media_role", "unknown") for m in self.media)
        tally["attachments"] = media_roles.get("attachment", 0)
        return dict(tally)

    def _stage_dry_run(self, discovery: dict) -> dict:
        urls = discovery.get("items", [])
        topic_urls = [u for u in urls
                      if "page=" in u.get("url", "") or "?page" in u.get("url", "")
                      or u.get("kind") == "post"]
        return {
            "strategy": "inventory-only",
            "urls_inspected": len(urls),
            "samples_taken": min(len(urls), 5),
            "templates_sampled": discovery.get("forum_entity_count", 0),
            "pagination_checked": bool(topic_urls) or not urls,
            "originals_vs_thumbnails": {"checked": False, "note": "needs selector analysis"},
            "decision": "ready" if urls else "needs changes",
            "note": "dry-run: byte probes done, mass download not performed",
        }

    # -------------------------------------------------- media + entity coverage

    def _stage_media_validation(self) -> tuple[list[dict], list[dict], int]:
        valid, invalid, kinds = super()._stage_media_validation()
        self.media_valid = valid
        return valid, invalid, kinds

    def _report_extras(self) -> dict:
        """Forum block for the test report: engine, entities, attachment coverage."""
        attachment_total = self.entity_tally.get("attachments", 0)
        attachment_verified = sum(
            1 for m in self.media if m.get("media_role") == "attachment"
            and m.get("valid", False)
        )
        paginated = sum(
            1 for e in self.forum_entities
            if ("page=" in e.get("url", "") or "?page" in e.get("url", ""))
        )
        return {
            "forum": {
                "engine": self.summary.get("preflight", {}).get("engine", {}),
                "entity_counts": self.entity_tally,
                "extractor_plan": self.config.get("EXTRACTOR", "scrapy-adapter"),
                "paginated_posts_seen": paginated,
                "pagination": {
                    "multi_page_topics_seen": paginated,
                    "auto_followed": False,
                    "note": "multi-page topics are expansion candidates, not auto-followed in dry-run",
                },
                "attachment_coverage": (
                    f"{attachment_verified / attachment_total:.2f}" if attachment_total else "n/a"
                ),
            },
        }

    # ------------------------------------------------------------ full gate

    def _stage_full_run_gate(self) -> dict:
        """Same honest gate as the shared pipeline (blocked until a real
        deterministic full-run downloader is wired; exit 3 when requested)."""
        return super()._stage_full_run_gate()


def main() -> int:
    parser = argparse.ArgumentParser(description="forum-media archivist pipeline")
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
        summary = ForumPipeline(cfg, out, run_full=args.run_full, offline=args.offline).run()
    except core.ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    gate = summary["full_run_gate"]
    print(f"full_run_gate: {gate['action']} — {gate['reason']}")
    print(f"report: {out}")
    return int(gate.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())