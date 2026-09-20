# basic-media-skill

Web, gallery and document archivist: deterministic crawl orchestration with a mandatory Wayback recovery queue and verifiable coverage. Part of the **trehgranka-archivist** family (see `forum-media-skill` for public forums).

## Install

### Claude Code

```bash
/plugin marketplace add .        # from inside this directory, or
cp -r . ~/.claude/skills/basic-media-skill/
```

### OpenCode

```bash
cp -r . ~/.config/opencode/skills/basic-media-skill/
```

### Codex / general

```bash
cp -r . ~/.agents/skills/basic-media-skill/
```

### GitHub Copilot

```bash
cp -r . ~/.copilot/skills/basic-media-skill/
```

### Cursor

Copy into the project's `.cursor/skills/` (Cursor has no global skills path).

### Anywhere, any platform

```bash
./install.sh              # auto-detect platform, user-level
./install.sh --all        # install to every detected tool
./install.sh --dry-run    # preview without installing
```

On Windows run the installer from Git Bash / WSL, or copy the directory manually.

## Use

Invoke the skill:

```
/basic-media-skill archive https://example.org/gallery/
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

Update the skill from real usage:

```bash
python scripts/evolve.py --correct "thumbnails keep landing in the originals folder"
```

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