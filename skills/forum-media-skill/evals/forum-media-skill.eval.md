# forum-media-skill eval spec

Binary checks + golden fixtures. Extracted from the first `json` fenced block by
the bundled `scripts/run_evals.py` — edit the block, never the sidecars.

```json
{
  "skill": "forum-media-skill",
  "run": "python scripts/run_pipeline.py --config {input} --output {output} --offline",
  "criteria": [
    {"id": "valid-json", "text": "Report is structured JSON containing a stage, gate and decision", "type": "command", "cmd": "python scripts/validate_report.py {output} --check valid-json"},
    {"id": "dry-run", "text": "A run without --run-full stays in dry-run and does not claim a released full run", "type": "command", "cmd": "python scripts/validate_report.py {output} --check dry-run"},
    {"id": "full-run-gate", "text": "Full-run confirmation fields are present and the gate is explicitly closed when not released", "type": "command", "cmd": "python scripts/validate_report.py {output} --check full-run-gate"},
    {"id": "coverage-ratio", "text": "Coverage is reported as n/a or as a decimal between 0 and 1 inclusive", "type": "command", "cmd": "python scripts/validate_report.py {output} --check coverage-ratio"},
    {"id": "recovery-required", "text": "Every validation failure appears in the recovery queue and the queue size matches the failure count", "type": "command", "cmd": "python scripts/validate_report.py {output} --check recovery-required"},
    {"id": "forum-payload", "text": "The report exposes a forum block with entity counts, pagination marks and attachment coverage", "type": "command", "cmd": "python scripts/validate_report.py {output} --check forum-payload"}
  ],
  "golden": [
    {
      "id": "forum-dry-run",
      "input": "golden/forum-dry-run/",
      "split": "val",
      "expected": null,
      "expected_status": "pending-first-green",
      "compare_ignore": ["generated_at", "output"]
    },
    {
      "id": "forum-fullrun-blocked",
      "input": "golden/forum-fullrun-blocked/",
      "split": "val",
      "expected": null,
      "expected_status": "pending-first-green",
      "compare_ignore": ["generated_at", "output"]
    },
    {
      "id": "forum-extract-holdout",
      "input": "golden/forum-extract-holdout/",
      "split": "test",
      "expected": null,
      "expected_status": "pending-first-green",
      "compare_ignore": ["generated_at", "output"]
    }
  ]
}
```

## Regenerating fixtures

```bash
python scripts/fixture_factory.py --root evals/golden --overwrite
```

## Rolling out

```bash
python scripts/run_evals.py
python scripts/run_evals.py --rollout --promote
```