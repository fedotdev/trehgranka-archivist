#!/usr/bin/env python3
"""
Deterministic fixture factory for basic-media-skill eval golden cases.

Regenerates the golden inputs so the eval is reproducible byte for byte.
Writes each case's config.yaml + discovered_urls.jsonl under evals/golden/.

Usage:
    python3 scripts/fixture_factory.py --root ../evals/golden
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CASES = {
    "gallery-dry-run": {
        "config": {
            "TARGET_URL": "https://example.org/gallery/",
            "PROJECT_NAME": "basic_media_eval_gallery",
            "SCOPE": "gallery",
            "ALLOWED_DOMAINS": ["example.org"],
            "MAX_PAGES": 20,
            "MAX_DEPTH": 3,
            "RESPECT_ROBOTS_TXT": True,
            "REQUEST_DELAY_SECONDS": 2,
            "USER_CONFIRMED_FULL_RUN": False,
        },
        "manifest": [
            {"url": "https://example.org/gallery/", "kind": "page", "depth": 0, "status": 200, "content_type": "text/html; charset=utf-8", "template_type": "gallery_index"},
            {"url": "https://example.org/gallery/album-1/", "kind": "page", "depth": 1, "status": 200, "content_type": "text/html; charset=utf-8", "template_type": "album_page"},
            {"url": "https://example.org/gallery/album-1/photo-1.jpg", "kind": "media", "depth": 2, "status": 200, "content_type": "image/jpeg", "media_role": "original", "body": "6254734c-7e90-4a1e-9d9b-13c0e7f1a1b2"},
            {"url": "https://example.org/gallery/album-1/photo-1-thumb.jpg", "kind": "media", "depth": 2, "status": 200, "content_type": "image/jpeg", "media_role": "thumbnail", "body": "52f9dc8c-6f51-4e9d-8f8e-b4479c38513c"},
            {"url": "https://example.org/gallery/album-1/photo-2.jpg", "kind": "media", "depth": 2, "status": 404, "content_type": "image/jpeg", "media_role": "original", "body": ""},
            {"url": "https://example.org/gallery/album-1/photo-2-thumb.jpg", "kind": "media", "depth": 2, "status": 200, "content_type": "image/jpeg", "media_role": "thumbnail", "body": "ef6b4f5d-55a3-4f0a-9c40-2db4c8a0d114"},
            {"url": "https://example.org/gallery/manual.pdf", "kind": "document", "depth": 1, "status": 200, "content_type": "application/pdf", "media_role": "document_scan", "body": "PDF-document-body"},
        ],
    },
    "gallery-fullrun-blocked": {
        "config": {
            "TARGET_URL": "https://example.org/gallery/",
            "PROJECT_NAME": "basic_media_eval_blocked",
            "SCOPE": "gallery",
            "ALLOWED_DOMAINS": ["example.org"],
            "MAX_PAGES": 20,
            "MAX_DEPTH": 3,
            "RESPECT_ROBOTS_TXT": True,
            "REQUEST_DELAY_SECONDS": 2,
            "USER_CONFIRMED_FULL_RUN": True,
        },
        "manifest": [
            {"url": "https://example.org/gallery/", "kind": "page", "depth": 0, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://example.org/gallery/album-1/", "kind": "page", "depth": 1, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://example.org/gallery/album-1/photo-1.jpg", "kind": "media", "depth": 2, "status": 200, "content_type": "image/jpeg", "media_role": "original"},
            {"url": "https://example.org/gallery/album-1/photo-2.jpg", "kind": "media", "depth": 2, "status": 200, "content_type": "image/png", "media_role": "original"},
        ],
    },
    "attachment-410-recovery": {
        "config": {
            "TARGET_URL": "https://community.example.org/",
            "PROJECT_NAME": "basic_media_eval_attachment",
            "SCOPE": "section",
            "ALLOWED_DOMAINS": ["community.example.org"],
            "MAX_PAGES": 20,
            "MAX_DEPTH": 3,
            "RESPECT_ROBOTS_TXT": True,
            "REQUEST_DELAY_SECONDS": 2,
            "USER_CONFIRMED_FULL_RUN": False,
        },
        "manifest": [
            {"url": "https://community.example.org/", "kind": "page", "depth": 0, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/topic/42/", "kind": "page", "depth": 1, "status": 200, "content_type": "text/html; charset=utf-8"},
            {"url": "https://community.example.org/files/attachment.pdf", "kind": "document", "depth": 2, "status": 200, "content_type": "application/pdf", "media_role": "attachment", "data": "255044462d312e34"},
            {"url": "https://community.example.org/files/photo.jpg", "kind": "media", "depth": 2, "status": 200, "content_type": "image/jpeg", "media_role": "original", "data": "ffd8ffe0"},
            {"url": "https://community.example.org/files/broken.jpg", "kind": "media", "depth": 2, "status": 410, "content_type": "image/jpeg", "media_role": "original"},
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
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="regenerate eval golden fixtures")
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