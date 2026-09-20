#!/usr/bin/env python3
"""
Deterministic fixture factory for forum-media-skill eval golden cases.

Regenerates the golden inputs so the eval is reproducible byte for byte.
Writes each case's config.yaml + discovered_urls.jsonl under evals/golden/.

Usage:
    python3 scripts/fixture_factory.py --root ../evals/golden
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

FORUM_ENDPOINT_TEMPLATES = {
    "forum": "viewforum.php?f={id}",
    "topic": "viewtopic.php?t={id}",
    "post": "viewtopic.php?t={t}&page={page}",
}

CASES = {
    "forum-dry-run": {
        "config": {
            "TARGET_URL": "https://community.example.org/",
            "PROJECT_NAME": "forum_media_eval_dryrun",
            "SCOPE": "forum",
            "ALLOWED_DOMAINS": ["community.example.org"],
            "MAX_PAGES": 30,
            "MAX_DEPTH": 3,
            "RESPECT_ROBOTS_TXT": True,
            "REQUEST_DELAY_SECONDS": 2,
            "USER_CONFIRMED_FULL_RUN": False,
            "EXTRACTOR": "scrapy-adapter",
        },
        "manifest": [
            {"url": "https://community.example.org/", "kind": "page", "depth": 0, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/viewforum.php?f=2", "kind": "forum", "depth": 1, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/viewtopic.php?t=42", "kind": "topic", "depth": 2, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/viewtopic.php?t=42&page=2", "kind": "post", "depth": 3, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/post/9876", "kind": "post", "depth": 3, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/download/file.php?id=501", "kind": "document", "depth": 3, "status": 200, "content_type": "application/pdf", "media_role": "attachment"},
            {"url": "https://community.example.org/files/logo.png", "kind": "media", "depth": 2, "status": 200, "content_type": "image/png", "media_role": "navigation_asset"},
        ],
    },
    "forum-fullrun-blocked": {
        "config": {
            "TARGET_URL": "https://community.example.org/",
            "PROJECT_NAME": "forum_media_eval_blocked",
            "SCOPE": "forum",
            "ALLOWED_DOMAINS": ["community.example.org"],
            "MAX_PAGES": 30,
            "MAX_DEPTH": 3,
            "RESPECT_ROBOTS_TXT": True,
            "REQUEST_DELAY_SECONDS": 2,
            "USER_CONFIRMED_FULL_RUN": True,
            "EXTRACTOR": "forum-dl",
        },
        "manifest": [
            {"url": "https://community.example.org/", "kind": "page", "depth": 0, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/viewforum.php?f=2", "kind": "forum", "depth": 1, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/viewtopic.php?t=42", "kind": "topic", "depth": 2, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/viewtopic.php?t=42&page=2", "kind": "post", "depth": 3, "status": 200, "content_type": "text/html; charset=utf-8"},
        ],
    },
    "forum-extract-holdout": {
        "config": {
            "TARGET_URL": "https://community.example.org/",
            "PROJECT_NAME": "forum_media_eval_extract",
            "SCOPE": "forum",
            "ALLOWED_DOMAINS": ["community.example.org"],
            "MAX_PAGES": 30,
            "MAX_DEPTH": 3,
            "RESPECT_ROBOTS_TXT": True,
            "REQUEST_DELAY_SECONDS": 2,
            "USER_CONFIRMED_FULL_RUN": False,
            "EXTRACTOR": "forumscraper",
        },
        "manifest": [
            {"url": "https://community.example.org/", "kind": "page", "depth": 0, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/viewforum.php?f=2", "kind": "forum", "depth": 1, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/viewtopic.php?t=42", "kind": "topic", "depth": 2, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/viewtopic.php?t=42&page=2", "kind": "post", "depth": 3, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/member/101", "kind": "author", "depth": 2, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/download/file.php?id=501", "kind": "document", "depth": 3, "status": 200, "content_type": "application/pdf", "media_role": "attachment"},
        ],
    },
}

CONFIG_TEMPLATE = """TARGET_URL: "{target}"
PROJECT_NAME: "{project}"
SCOPE: "{scope}"
ALLOWED_DOMAINS:
{domains}
MAX_PAGES: {max_pages}
MAX_DEPTH: {max_depth}
RESPECT_ROBOTS_TXT: true
REQUEST_DELAY_SECONDS: 2
USER_CONFIRMED_FULL_RUN: {confirm}
EXTRACTOR: "{extractor}"

# Forum crawling profile:
# - EXTRACTOR is advisory: full runs orchestrate a deterministic core, not the
#   extractor itself.
# - Topic pagination is checked in discovery; multi-page topics are expansion
#   candidates, not auto-followed in dry-run.
"""


def render_config(case: dict) -> str:
    cfg = case["config"]
    domains = "\n".join(f'  - "{d}"' for d in cfg["ALLOWED_DOMAINS"])
    return CONFIG_TEMPLATE.format(
        target=cfg["TARGET_URL"],
        project=cfg["PROJECT_NAME"],
        scope=cfg["SCOPE"],
        domains=domains,
        max_pages=cfg["MAX_PAGES"],
        max_depth=cfg["MAX_DEPTH"],
        confirm=str(cfg["USER_CONFIRMED_FULL_RUN"]).lower(),
        extractor=cfg.get("EXTRACTOR", "scrapy-adapter"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="regenerate golden fixtures for forum-media-skill")
    parser.add_argument("--root", default="evals/golden", help="golden cases root directory")
    parser.add_argument("--overwrite", action="store_true", help="regenerate even if files exist")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    for name, case in CASES.items():
        case_dir = root / name
        case_dir.mkdir(parents=True, exist_ok=True)
        config_path = case_dir / "config.yaml"
        manifest_path = case_dir / "discovered_urls.jsonl"
        if not args.overwrite and config_path.exists() and manifest_path.exists():
            print(f"skip {name} (exists; pass --overwrite to regenerate)")
            continue
        config_path.write_text(render_config(case), encoding="utf-8")
        with manifest_path.open("w", encoding="utf-8", newline="\n") as fh:
            for item in case["manifest"]:
                fh.write(json.dumps(item, ensure_ascii=True) + "\n")
        print(f"wrote {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())