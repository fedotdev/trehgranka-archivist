#!/usr/bin/env python3
"""
Checks a produced archivist test_report.json for the skill's invariants.

Designed for the eval spec with {output} bound to a produced report path:

    python3 scripts/validate_report.py {output} --check coverage

Runs every check when none is given. Exit 0 only when all requested checks pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CHECK_NAMES = ("valid-json", "dry-run", "full-run-gate", "coverage-ratio", "recovery-required")


def _load(path: str) -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def check_valid_json(data: dict) -> bool:
    return isinstance(data, dict) and data.get("stage") == "test_report" and "dry_run" in data


def check_dry_run(data: dict) -> bool:
    # A run that never passed the gate is a dry run by definition.
    return bool(data.get("dry_run")) and data.get("gate") != "proceed"


def check_full_run_gate(data: dict) -> bool:
    # The gate lives on the summary; a plain test_report must never claim a
    # full run was released without the confirmation fields being recorded.
    if data.get("gate") not in ("proceed", "stop"):
        return False
    if data.get("gate") == "stop":
        return bool(data.get("gate_reason"))
    return "confirmed" in data and "run_full_flag" in data


def check_coverage_ratio(data: dict) -> bool:
    coverage = data.get("coverage")
    if coverage == "n/a":
        return True
    if isinstance(coverage, str):
        return coverage.replace(".", "", 1).isdigit() and 0.0 <= float(coverage) <= 1.0
    return isinstance(coverage, (int, float)) and 0.0 <= float(coverage) <= 1.0


def check_recovery_required(data: dict) -> bool:
    invalid = int(data.get("media_invalid", 0))
    queue = int(data.get("recovery_queue_size", 0))
    return invalid == queue


CHECKS = {
    "valid-json": check_valid_json,
    "dry-run": check_dry_run,
    "full-run-gate": check_full_run_gate,
    "coverage-ratio": check_coverage_ratio,
    "recovery-required": check_recovery_required,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", help="path to a produced test_report.json")
    parser.add_argument("--check", action="append", default=[], choices=CHECK_NAMES)
    args = parser.parse_args()

    try:
        data = _load(args.report)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL report not readable as JSON: {exc}", file=sys.stderr)
        return 1

    names = args.check or list(CHECK_NAMES)
    failed = False
    for name in names:
        ok = CHECKS[name](data)
        print(f"{name}: {'pass' if ok else 'FAIL'}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())