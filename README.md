# OpenCollab

OpenCollab is a minimal multi-agent software development framework with chat,
team, and headless evaluation modes.

OpenCollab follows a strict clean architecture. For the layer map and
contribution guidance, see `CLAUDE.md` and `opencollab/README.md`.

## Repository Guide

| Path | What it is | Read next |
|------|------------|-----------|
| `opencollab/` | The installable Python package and `opencollab` CLI. | `opencollab/README.md` |
| `configs/` | Runtime configuration templates and config loading notes. | `configs/README.md` |
| `scripts/` | Repository-level launcher and benchmark helper scripts. | `scripts/README.md` |
| `swebench/` | Docker-based SWE-bench runner. | `swebench/README.md` |
| `docs/archive/` | Historical record: completed refactor plans, architecture surveys, and the old code map. | files within |

Untracked local state (gitignored, safe to ignore when reading the code):
`evals/` (prediction outputs), `logs/`, `swe_workdir/`.

## Start Here

1. Create local runtime configuration from `configs/.env.example`.
2. Start OpenCollab from the repository root with `scripts/start_opencollab.sh`.
3. Use the nested READMEs above for setup details, CLI modes, evaluation, and
   benchmark tooling.

Do not commit real API keys or local runtime state.
