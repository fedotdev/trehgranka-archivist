# forum-media-skill

Public web forum archivist: topics, posts, quotes, reactions, attachments and authors with deterministic crawl orchestration, a mandatory Wayback recovery queue and verifiable per-entity coverage. Part of the **trehgranka-archivist** family (see `basic-media-skill` for websites, galleries and documents).

## Install

### Claude Code

```bash
/plugin marketplace add .        # from inside this directory, or
cp -r . ~/.claude/skills/forum-media-skill/
```

### OpenCode

```bash
cp -r . ~/.config/opencode/skills/forum-media-skill/
```

### Anywhere, any platform

```bash
./install.sh              # installs to the current user's skills dir
```

On Windows: `.\install.ps1`, or copy the directory manually (e.g. to `~/.config/opencode/skills/forum-media-skill/`).

## Use

Invoke the skill:

```
/forum-media-skill archive https://forum.example.org/viewforum.php?f=2
```

Then run the one-command pipeline:

```bash
python scripts/run_pipeline.py --config path/to/project.yaml --output ./test_report.json --offline
```

Eval the skill against its bundled loss function:

```bash
python scripts/run_evals.py --validate
python scripts/run_evals.py --rollout
```

## Tooling layout

This package ships only the forum-specific scripts (`run_pipeline.py`,
`fixture_factory.py`, `validate_report.py`) and its own eval runner
(`run_evals.py`). The maintenance toolchain (`evolve.py`,
`staleness_check.py`, `schema_drift.py`, `dependency_health.py`,
`review_staleness.py`, `skill_document.py`) and the `wayback.py` helper
are byte-identical across the two skills and are maintained once in
`basic-media-skill/scripts/` — this package carries no duplicate copies.

## Layout

- `SKILL.md` — activation contract and operational rules
- `AGENTS.md` — cross-tool companion summary
- `scripts/` — deterministic pipeline, Wayback helper, evals, evolve loop
- `references/` — detailed rules, loaded on demand
- `assets/` — safe example configs (project, selectors, extraction, exclusions)
- `evals/` — binary checks + golden fixtures
- `.claude-plugin/` — Claude Code plugin manifests

## License

MIT