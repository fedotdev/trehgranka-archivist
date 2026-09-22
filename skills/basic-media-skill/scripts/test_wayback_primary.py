#!/usr/bin/env python3
"""
test_wayback_primary.py — offline verification for WAYBACK_PRIMARY_SITE_MODE.

The pipeline is exercised end-to-end against fixture data (never the network):
every invariant from the mode spec is asserted on the produced report,
artifacts and provenance. Run:

    python3 scripts/test_wayback_primary.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from wayback_primary import WaybackPrimaryPipeline
import wayback_url_parser as wup
import archivist_core as core

TARGET_TS = "20040804234004"
NEAREST_TS = "20040801124510"
ORIGIN = "metro-net.da.ru"
SEED = f"https://web.archive.org/web/{TARGET_TS}/http://{ORIGIN}/"
HOME = f"http://{ORIGIN}/"
STATION = f"http://{ORIGIN}/pages/station.html"
MAP_GIF = f"http://{ORIGIN}/img/map.gif"
ABSENT_GIF = f"http://{ORIGIN}/img/absent.gif"
STATION_JPG = f"http://{ORIGIN}/img/station.jpg"
STYLE_CSS = f"http://{ORIGIN}/css/style.css"

GIF = base64.b64decode(
    "R0lGODlhEQARAPAAAP///wAAACH5BAEAAAAALAAAAAARABEAAAICRAEAOw==")
CSS = "body{background:url(%s)}h1{color:#333}" % MAP_GIF


def html(title: str, *extra: str) -> str:
    pad = ("<p>The archived fixture page padding — enough real content that "
           "the validator sees an actual document instead of a service stub. "
           "Wayback snapshots of 2004-era sites are usually rich in text, menu "
           "links, captioned pictures and repeated navigation blocks.</p>" * 2)
    parts = ["<!DOCTYPE html>", "<html><head><title>%s</title></head><body>" % title,
             "<h1>%s</h1>" % title, pad]
    parts.extend(extra)
    parts.append("</body></html>")
    return "\n".join(parts)


HOME_HTML = html(
    "Metro Net",
    '<a href="%s">station</a>' % STATION,
    '<a href="%s">home</a>' % HOME,
    '<img src="%s">' % MAP_GIF,
    '<img src="%s">' % ABSENT_GIF,
    '<link rel="stylesheet" href="%s">' % STYLE_CSS,
    '<a href="http://external.example.org/other.gif">out</a>',
)
STATION_HTML = html(
    "Station",
    '<a href="%s">back</a>' % HOME,
    '<img src="%s">' % STATION_JPG,
)


def digest_of(body: bytes) -> str:
    return base64.b32encode(hashlib.sha1(body).digest()).decode().lower()


def replay(original: str, ts: str, mode: str = "") -> str:
    suffix = "" if mode in ("", "page") else mode
    return f"https://web.archive.org/web/{ts}{suffix}/{original}"


def fixture_config(*, discovery_divergent: bool = False,
                   id_stub: bool = True) -> dict:
    """Ground-truth fixture: home + station pages, map.gif (nearest-only),
    style.css (exact), station.jpg (CDX row but invalid id_ bytes),
    absent.gif (no captures)."""
    offline_html = {
        HOME: bytes(HOME_HTML, "utf-8").hex(),
        STATION: bytes(STATION_HTML, "utf-8").hex(),
    }
    offline_cdx = {
        HOME: {"captures": [
            {"timestamp": TARGET_TS, "original": HOME, "statuscode": "200",
             "mimetype": "text/html", "digest": digest_of(HOME_HTML.encode()),
             "length": str(len(HOME_HTML))}]},
        STATION: {"captures": [
            {"timestamp": TARGET_TS, "original": STATION, "statuscode": "200",
             "mimetype": "text/html", "digest": digest_of(STATION_HTML.encode()),
             "length": str(len(STATION_HTML))}]},
        MAP_GIF: {"captures": [
            {"timestamp": NEAREST_TS, "original": MAP_GIF, "statuscode": "200",
             "mimetype": "image/gif", "digest": digest_of(GIF),
             "length": str(len(GIF))}]},
        STYLE_CSS: {"captures": [
            {"timestamp": TARGET_TS, "original": STYLE_CSS, "statuscode": "200",
             "mimetype": "text/css", "digest": digest_of(CSS.encode()),
             "length": str(len(CSS))}]},
        STATION_JPG: {"captures": [
            {"timestamp": TARGET_TS, "original": STATION_JPG, "statuscode": "200",
             "mimetype": "image/jpeg", "digest": digest_of(b"\x00" * 4),
             "length": "14"}]},
        ABSENT_GIF: {"captures": []},
    }
    jpeg_stub = bytes("<html><body>wayback internal error page</body></html>", "utf-8")
    offline_replay = {
        replay(HOME, TARGET_TS): {"status": 200, "content_type": "text/html; charset=utf-8",
                                  "body_hex": bytes(HOME_HTML, "utf-8").hex()},
        replay(HOME, TARGET_TS, "id_"): {"status": 200,
                                         "content_type": "text/html; charset=utf-8",
                                         "body_hex": bytes(HOME_HTML, "utf-8").hex()},
        replay(STATION, TARGET_TS): {"status": 200, "content_type": "text/html; charset=utf-8",
                                     "body_hex": bytes(STATION_HTML, "utf-8").hex()},
        replay(STATION, TARGET_TS, "id_"): {"status": 200,
                                            "content_type": "text/html; charset=utf-8",
                                            "body_hex": bytes(STATION_HTML, "utf-8").hex()},
        replay(MAP_GIF, NEAREST_TS, "id_"): {"status": 200, "content_type": "image/gif",
                                             "body_hex": GIF.hex()},
        replay(STYLE_CSS, TARGET_TS, "id_"): {"status": 200, "content_type": "text/css",
                                              "body_hex": bytes(CSS, "utf-8").hex()},
        replay(STATION_JPG, TARGET_TS, "id_"): {"status": 200, "content_type": "image/jpeg",
                                                "body_hex": jpeg_stub.hex()},
    }
    if discovery_divergent:
        # navigation replay injects wayback chrome; id_ stays pure: the two
        # fetches must diverge and the stored artifact is the id_ body
        chrome = HOME_HTML.replace("<body>", '<body><!-- wayback toolbar injected -->')
        offline_html[HOME] = bytes(chrome, "utf-8").hex()
        offline_replay[replay(HOME, TARGET_TS)] = {
            "status": 200, "content_type": "text/html; charset=utf-8",
            "body_hex": bytes(chrome, "utf-8").hex()}
    return {
        "TARGET_URL": SEED,
        "SOURCE_MODE": "wayback_primary",
        "USER_CONFIRMED_FULL_RUN": True,
        "REQUEST_DELAY_SECONDS": 0,
        "wayback_primary": {
            "seed_url": SEED,
            "target_timestamp": TARGET_TS,
            "prefer_exact_timestamp": True,
            "timestamp_tolerance_days": 30,
            "use_replay_for_discovery": True,
            "use_id_raw_for_storage": True,
            "allow_live_fallback": False,
            "collapse_digest": True,
            "max_candidates": 8,
            "offline_html": offline_html,
            "offline_cdx": offline_cdx,
            "offline_replay": offline_replay,
        },
    }


class WaybackPrimaryTest(unittest.TestCase):

    maxDiff = None

    def run_pipeline(self, cfg: dict, run_full: bool) -> dict:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        out = Path(self._tmp.name) / "test_report.json"
        pipeline = WaybackPrimaryPipeline(cfg, out, run_full_flag=run_full,
                                          offline=True)
        report = pipeline.run()
        self.assertTrue(out.exists())
        return report

    def report_paths(self) -> Path:
        return Path(self._tmp.name)

    # -- 1. inventory (dry run) -----------------------------------------
    def test_dry_run_inventory_no_downloads(self):
        cfg = fixture_config()
        report = self.run_pipeline(cfg, run_full=False)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["gate"], "stop")
        self.assertEqual(report["gate_reason"], "full run requires "
                         "USER_CONFIRMED_FULL_RUN=true and --run-full")
        block = report["wayback_primary"]
        self.assertEqual(block["seed_timestamp"], TARGET_TS)
        self.assertEqual(block["seed_original_url"], HOME)
        self.assertEqual(wup.parse_replay_url(SEED)["seed_replay_mode"], "page")
        # discovery from archived html only
        self.assertEqual(block["discovered_html"], 2)
        self.assertEqual(block["discovered_media"], 4)
        self.assertEqual(block["skipped_out_of_scope"], 1)
        # capture selection without any byte transfer
        self.assertEqual(block["fetched_html"], 0)
        self.assertEqual(block["fetched_media"], 0)
        self.assertEqual(block["exact_timestamp"], 4)   # home, station, css, jpg
        self.assertEqual(block["nearest_timestamp"], 1)  # map.gif
        self.assertEqual(block["unresolved"], 1)         # absent.gif
        root = self.report_paths()
        # inventory only: nothing written under site/raw, no provenance rows
        self.assertFalse(any(p.is_file() for p in (root / "site").rglob("*")))
        self.assertFalse(any(p.is_file() for p in (root / "raw").rglob("*")))
        self.assertEqual((root / "provenance.jsonl").read_text("utf-8").count("\n"), 0)

    # -- 2. full run: verified storage, id_, local copy ------------------
    def test_full_run_verified_storage_and_local_copy(self):
        cfg = fixture_config()
        report = self.run_pipeline(cfg, run_full=True)
        self.assertFalse(report["dry_run"])
        self.assertEqual(report["gate"], "proceed")
        self.assertEqual(report["coverage"], 0.6667)
        block = report["wayback_primary"]
        self.assertEqual(block["fetched_html"], 2)
        self.assertEqual(block["valid_html"], 2)
        self.assertEqual(block["fetched_media"], 2)
        self.assertEqual(block["valid_media"], 2)
        self.assertEqual(block["exact_timestamp"], 4)
        self.assertEqual(block["nearest_timestamp"], 1)
        self.assertEqual(block["unresolved"], 2)          # absent + corrupt jpeg
        self.assertEqual(block["placeholder_or_corrupt"], 1)
        self.assertEqual(block["allow_live_fallback"], False)
        root = self.report_paths()
        # local copy exists and is self-contained (no web.archive.org refs)
        site = root / "site"
        index = site / "index.html"
        self.assertTrue(index.exists())
        text = index.read_text("utf-8")
        self.assertNotIn("web.archive.org", text)
        self.assertIn('href="pages/station.html"', text)
        self.assertIn('src="img/map.gif"', text)
        self.assertIn('href="css/style.css"', text)
        self.assertTrue((site / "img/map.gif").read_bytes() == GIF)
        self.assertTrue((site / "css/style.css").exists())
        # rewritten css points at the local image
        self.assertIn("../img/map.gif", (site / "css/style.css").read_text("utf-8"))
        self.assertFalse((site / "img/absent.gif").exists())

    def test_provenance_records_complete(self):
        cfg = fixture_config()
        self.run_pipeline(cfg, run_full=True)
        root = self.report_paths()
        rows = [json.loads(line) for line in
                (root / "provenance.jsonl").read_text("utf-8").splitlines() if line]
        by_url = {row["original_url"]: row for row in rows}
        self.assertIn(HOME, by_url)
        self.assertIn(MAP_GIF, by_url)
        rec = by_url[MAP_GIF]
        self.assertEqual(rec["entity_type"], "image")
        self.assertEqual(rec["selected_capture_timestamp"], NEAREST_TS)
        # nearest-exact selection: exact cannot be proven, tolerance window hit
        self.assertIn(rec["selected_reason"], ("nearest_timestamp", "within_tolerance"))
        self.assertEqual(rec["replay_mode"], "id_")
        self.assertTrue(rec["wayback_replay_url"].endswith(
            f"web/{NEAREST_TS}id_/{MAP_GIF}"))
        self.assertEqual(rec["validation_status"], "verified")
        self.assertEqual(rec["local_path"], "img/map.gif")
        self.assertEqual(rec["seed_timestamp"], TARGET_TS)
        # every artifact gets a record: 2 pages + 2 media
        self.assertEqual({r["validation_status"] for r in rows}, {"verified"})
        self.assertEqual(len(rows), 4)

    # -- 3. exact vs nearest --------------------------------------------
    def test_exact_capture_wins_when_present(self):
        cfg = fixture_config()
        # give map.gif an exact-timestamp capture as well, with DIFFERENT but
        # still-valid bytes (same digest would be collapsed as a duplicate)
        exact_gif = bytearray(GIF)
        exact_gif[16] ^= 0xFF  # flip a GCT color byte; header+trailer intact
        exact_gif = bytes(exact_gif)
        cfg["wayback_primary"]["offline_cdx"][MAP_GIF]["captures"].append({
            "timestamp": TARGET_TS, "original": MAP_GIF, "statuscode": "200",
            "mimetype": "image/gif", "digest": digest_of(exact_gif),
            "length": str(len(exact_gif))})
        cfg["wayback_primary"]["offline_replay"][replay(MAP_GIF, TARGET_TS, "id_")] = {
            "status": 200, "content_type": "image/gif", "body_hex": exact_gif.hex()}
        self.run_pipeline(cfg, run_full=True)
        rows = [json.loads(line) for line in
                (self.report_paths() / "provenance.jsonl").read_text("utf-8").splitlines()
                if line]
        rec = next(r for r in rows if r["original_url"] == MAP_GIF)
        self.assertEqual(rec["selected_capture_timestamp"], TARGET_TS)
        self.assertEqual(rec["selected_reason"], "exact_timestamp")
        self.assertEqual((self.report_paths() / "site" / "img" / "map.gif").read_bytes(),
                         exact_gif)

    # -- 4. replay divergence -------------------------------------------
    def test_replay_id_divergence_detected(self):
        cfg = fixture_config(discovery_divergent=True)
        report = self.run_pipeline(cfg, run_full=True)
        block = report["wayback_primary"]
        self.assertEqual(block["replay_id_divergences"], 1)
        # stored artifact is the pure id_ body, not the chrome-injected replay
        site = self.report_paths() / "site"
        self.assertNotIn("wayback toolbar injected", (site / "index.html").read_text("utf-8"))

    # -- 5. wayback service page rejected --------------------------------
    def test_wayback_stub_page_not_verified(self):
        cfg = fixture_config()
        cfg["wayback_primary"]["offline_replay"][replay(HOME, TARGET_TS, "id_")] = {
            "status": 200, "content_type": "text/html; charset=utf-8",
            "body_hex": bytes(
                "<html><body><h1>The Wayback Machine has not archived that URL</h1>"
                "</body></html>", "utf-8").hex()}
        report = self.run_pipeline(cfg, run_full=True)
        block = report["wayback_primary"]
        self.assertEqual(block["valid_html"], 1)   # station only
        self.assertEqual(block["unresolved"], 3)   # absent + jpeg + stub home
        self.assertFalse((self.report_paths() / "site" / "index.html").exists())

    # -- 6. cdx error is not 'no captures' -------------------------------
    def test_cdx_error_recorded_not_unresolved(self):
        cfg = fixture_config()
        cfg["wayback_primary"]["offline_cdx"][ABSENT_GIF] = {"error": "503 cdx"}
        report = self.run_pipeline(cfg, run_full=True)
        block = report["wayback_primary"]
        self.assertEqual(block["cdx_errors"], 1)
        # absent.gif is NOT counted as unresolved (temporary failure -> retry)
        self.assertEqual(block["unresolved"], 1)   # only the corrupt jpeg

    # -- 7. unconfirmed full run stays inventory ------------------------
    def test_unconfirmed_run_is_dry(self):
        cfg = fixture_config()
        cfg["USER_CONFIRMED_FULL_RUN"] = False
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        out = Path(self._tmp.name) / "test_report.json"
        pipeline = WaybackPrimaryPipeline(cfg, out, run_full_flag=True, offline=True)
        report = pipeline.run()
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["gate"], "stop")
        self.assertEqual(report["wayback_primary"]["fetched_html"], 0)

    # -- 8. invariants ---------------------------------------------------
    def test_report_shape_and_barriers(self):
        cfg = fixture_config()
        report = self.run_pipeline(cfg, run_full=True)
        self.assertEqual(report["stage"], "test_report")
        self.assertEqual(report["source_mode"], "wayback_primary")
        self.assertEqual(report["recovery_required"], False)
        self.assertEqual(report["media_recovered"], 0)
        self.assertEqual(report["recovery_queue_size"], 0)
        block = report["wayback_primary"]
        # verified only after real validation, never by CDX presence
        self.assertEqual(block["valid_media"], 2)     # jpeg cdx row exists but invalid
        self.assertEqual(report["media_invalid"], 1)  # counted, not dropped
        # disjointness: exact + nearest <= discovered (never double-crossed)
        self.assertLessEqual(block["exact_timestamp"] + block["nearest_timestamp"],
                             block["discovered_html"] + block["discovered_media"])

    def test_seed_parse_via_parser(self):
        rec = wup.parse_replay_url(SEED)
        self.assertEqual(rec["seed_timestamp"], TARGET_TS)
        self.assertEqual(rec["seed_original_url"], HOME)

    def test_audio_is_validated_stored_and_counted(self):
        audio_url = f"http://{ORIGIN}/audio/record.mp3"
        audio = b"ID3" + b"\x04\x00\x00" + b"\x00" * 120
        cfg = fixture_config()
        html = HOME_HTML.replace("</body>", f'<audio src="{audio_url}"></audio></body>')
        cfg["wayback_primary"]["offline_html"][HOME] = html.encode().hex()
        cfg["wayback_primary"]["offline_cdx"][audio_url] = {"captures": [{
            "timestamp": TARGET_TS, "original": audio_url, "statuscode": "200",
            "mimetype": "audio/mpeg", "digest": digest_of(audio),
            "length": str(len(audio))}]}
        cfg["wayback_primary"]["offline_replay"][replay(audio_url, TARGET_TS, "id_")] = {
            "status": 200, "content_type": "audio/mpeg", "body_hex": audio.hex()}
        report = self.run_pipeline(cfg, run_full=True)
        block = report["wayback_primary"]
        self.assertEqual(block["discovered_audio"], 1)
        self.assertEqual(block["valid_audio"], 1)
        self.assertEqual((self.report_paths() / "site" / "audio" / "record.mp3").read_bytes(), audio)

    def test_audio_magic_validation_without_dependencies(self):
        wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 8
        result = core.validate_media(status=200, content_type="audio/wav", body=wav)
        self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
