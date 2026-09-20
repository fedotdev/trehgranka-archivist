# Trehgranka Archivist

Deterministic, verifiable web/forum archiving skills driven by a single normative specification. The repo is a monorepo: the canonical prompt (`instructions/`) plus two self-contained skills that implement it (`skills/`).

Archiving here means **download → hash → validate → Wayback-recover → coverage-report**, with the LLM acting as planner/classifier/selector author — never as the downloader, verifier, or deleter. If a live file is missing or broken, Wayback recovery is mandatory, ranked by proximity to the source page's publication date (never blind-picking the newest capture).

## What's in the box

| Component | Purpose |
|---|---|
| `instructions/universal_web_forum_media_archivist_prompt.md` | Normative spec: system prompt, GitHub tool catalog, gallery & forum profiles, full-run readiness criteria, non-automatable decisions |
| `skills/basic-media-skill` | Websites, photo galleries, scans, documents |
| `skills/forum-media-skill` | Public forums: topics, posts, quotes, reactions, attachments, authors; ships a deterministic Invision Community (IPS) extractor (`scripts/extractors/invision.py`) |

Every skill is self-contained: install scripts, plugin manifests, a deterministic pipeline (`scripts/run_pipeline.py`), a Wayback CDX/replay helper (`scripts/wayback.py`, maintained once in `basic-media-skill/scripts/`), and its own loss function (`evals/*.eval.md` with golden fixtures).

> [!IMPORTANT]
> Everything under `skills/*/scripts/` is original, stdlib-only code written for this project. No third-party crawler/scraper libraries, no scripts copied from other repositories.

## Install a skill

```bash
cd skills/basic-media-skill && ./install.sh          # auto-detect platform
cd skills/forum-media-skill && ./install.sh
```

Or copy the directory into your tool's skills path (Claude Code, OpenCode, Codex, Cursor, etc.) — see each skill's `README.md`.

## Run the pipeline

```bash
# dry-run / test report, offline (uses a discovery manifest)
python skills/forum-media-skill/scripts/run_pipeline.py \
  --config path/to/project.yaml --output ./test_report.json --offline

# evals against the bundled loss function
python skills/basic-media-skill/scripts/run_evals.py --rollout --include-holdout
python skills/forum-media-skill/scripts/run_evals.py --rollout --include-holdout
```

A full run starts only after `USER_CONFIRMED_FULL_RUN = true` and an explicit confirmation — see the spec.

## Operating order (spec section 1)

1. **Preflight** — normalize URL, `robots.txt`, site rules, licensing, engine detection, authentication/DRM boundaries, risk map. No mass requests.
2. **Discovery / dry-run** — inventory URLs, classify templates, sample each type, compare raw HTML vs rendered DOM, small test download.
3. **Test Report** — sizes, MIME, dimensions, originals vs thumbnails, pagination, extractor comparison; verdict `ready` / `needs changes`.
4. **Full run** — only after explicit user confirmation.
5. **Final Report** — discovered/processed/verified/failed/skipped counts, per-entity coverage, unresolved URLs, resume instructions.

## Safety boundaries

- Never bypass CAPTCHA, login, paywall, DRM, `robots.txt`, or rate limits; no private messages, closed topics/profiles, or personal data invisible to a normal public visitor.
- On HTTP 429/503: lower concurrency, increase delay, or stop and report — never hammer.
- Archived content, credentials, and run-workspace state (SQLite, raw downloads, probe/recovery outputs, reports) **never** get committed here; they live in the separate run workspace.

## Layout

```
instructions/                          # canonical specification (normative)
skills/
  basic-media-skill/                   # websites, galleries, scans, documents
    SKILL.md  AGENTS.md  EVOLUTION.md  README.md
    install.sh  install.ps1
    .claude-plugin/
    scripts/                           # pipeline, wayback, evals, evolve, helpers
    references/                        # 7 procedure references
    assets/                            # example config/rules
    evals/golden/                      # 3 golden fixtures per skill
  forum-media-skill/                   # public forums (+ IPS extractor)
    scripts/extractors/invision.py     # deterministic Invision Community parser
```

## Development workflow

Edit skills in the working tree, run their gates (`run_evals.py --rollout --include-holdout` for both skills must stay green), then sync the published copies under `skills/` byte-identical.

```bash
python scripts/run_evals.py --validate    # check eval spec integrity offline
python scripts/run_evals.py --rollout     # full rollout against golden fixtures
```

> [!NOTE]
> The repository defines no build/test/lint/CI commands of its own — verification is done by each skill's evals and by the spec's staged workflow.

## License

MIT (see each skill's `SKILL.md` frontmatter).