#!/usr/bin/env python3
"""Live-discovery integration test (P0.1/P0.2 acceptance).

Serves a tiny site on 127.0.0.1 (explicit test-only override) and drives the
real pipeline's `_crawl_online` path WITHOUT a prepared manifest:

    * a nested page with an in-page relative image (resolution against the
      page's final URL + <base href>)
    * a JPEG served without a file extension (classified by magic bytes)
    * a thumbnail (name-thumb.jpg) and a PDF attachment
    * robots.txt Allow: /

Passing assert: the crawl discovers all media, classifies kinds, and none of
the media references leaked outside scope. Exit 0 only then.

    python3 scripts/live_discovery_test.py
    python3 scripts/live_discovery_test.py --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import archivist_core as core
from run_pipeline import Pipeline


def _jpeg() -> bytes:
    return core._build_jpeg()


def _pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Size 1 >>\n%%EOF\n"


ROUTES = {
    "/robots.txt": (b"User-agent: *\nAllow: /\n", "text/plain"),
    "/": (b"<html><head><base href='/sub/'></head><body>"
          b"<img src='pic.jpg'>"
          b"<a href='page.html'>sub</a></body></html>", "text/html"),
    "/sub/page.html": (b"<html><body><img src='../img/photo'>"
                       b"<img src='thumb.jpg'><a href='/att/file.pdf'>dl</a></body></html>", "text/html"),
    "/img/photo": (_jpeg(), "image/jpeg"),
    "/sub/thumb.jpg": (_jpeg(), "image/jpeg"),
    "/sub/pic.jpg": (_jpeg(), "image/jpeg"),
    "/att/file.pdf": (_pdf(), "application/pdf"),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        route = ROUTES.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        body, ctype = route
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D102
        pass


def _build_config(port: int) -> dict:
    return {
        "TARGET_URL": f"http://127.0.0.1:{port}/",
        "ALLOWED_DOMAINS": ["127.0.0.1"],
        "MAX_PAGES": 20,
        "MAX_DEPTH": 3,
        "REQUEST_DELAY_SECONDS": 0.0,
        "TIMEOUT_SECONDS": 5.0,
        "RESPECT_ROBOTS_TXT": True,
        "SAVE_RAW_HTML": False,
        "USER_CONFIRMED_FULL_RUN": False,
        "TEST_ALLOW_LOOPBACK": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="live discovery integration test")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output", type=Path, default=None,
                        help="write the produced test report somewhere (default: temp)")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        cfg = _build_config(port)
        if args.output:
            out = args.output.resolve()
        else:
            out = Path(sys.argv[0]).resolve().parent / ".tmp-live-test-report.json"
        pipe = Pipeline(cfg, out, run_full=False, offline=False)
        # loopback is an explicit test-only override (never production allowed)
        pipe._create_layout()
        discovery = pipe._stage_discovery()
    finally:
        server.shutdown()
        server.server_close()

    found_jpeg = len([m for m in discovery["items"]
                      if m.get("detected_type") == "image/jpeg"])
    found_pdf = len([m for m in discovery["items"]
                     if m.get("detected_type") == "application/pdf"])
    outside_scope = [m.get("url") for m in discovery["items"]
                     if "127.0.0.1" not in m.get("url", "")]
    assert discovery["source"] == "crawl", f"expected live crawl, got {discovery['source']}"
    assert found_jpeg >= 3, f"magic-classified JPEGs missing: {found_jpeg}"
    assert found_pdf >= 1, "PDF attachment missing"
    assert not outside_scope, f"scope leak: {outside_scope}"
    if args.verbose:
        for item in discovery["items"]:
            print(item["kind"], item["url"], "->", item.get("detected_type") or "")
    print("live-discovery test ok: html/media classified, relative+base href resolved, scope held")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())