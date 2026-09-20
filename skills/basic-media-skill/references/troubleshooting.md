# Troubleshooting

## Config does not load

- `TARGET_URL` must be http(s) and its host must be listed in `ALLOWED_DOMAINS`.
- `SCOPE` must be one of `site|section|gallery|forum|topics|urls`.
- `USER_CONFIRMED_FULL_RUN` must be boolean `true`/`false` — a quoted `"true"` fails validation.
- The flat YAML subset does not support nested maps; keep the config flat like the example.

## Report looks wrong

- `--output` names the exact canonical report file (`test_report.json`); archive artifacts and `reports/` land beside it.
- `dry_run: true` with a `stop` gate is correct before user confirmation.
- `coverage: n/a` means nothing was discovered — supply a manifest or run discovery first.
- `recovery_queue_size` must equal `media_invalid`; if recovery items were dropped, the queue was bypassed.

## Manifest ignored

- The manifest is loaded only when `discovered_urls.jsonl` sits next to the config file (directory-style `--config path/to/case/`) or is passed via the `DISCOVERY_MANIFEST` config key.
- URL hosts outside `ALLOWED_DOMAINS` are dropped on purpose.

## Wayback helper

- `--offline` performs no network I/O; it only prints variants and notes the replay mode.
- `--probe` is a self-check for URL-variant construction and ranking invariants.
- The CDX endpoint records only HTTP 200 captures by default; widen the filter when a 3xx chain is expected.

## Eval

- Run `py -3 scripts/run_evals.py --validate` from the skill root; it must print `VALID`.
- Golden fixtures are offline-only; never point them at live sites.