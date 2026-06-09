# OpenCollab

OpenCollab is a minimal multi-agent software development framework with chat,
team, and headless evaluation modes.

## Repository Guide

- `opencollab/` contains the Python package, CLI, core runtime, TUI, team
  orchestration, and local evaluation harness. See `opencollab/README.md`.
- `configs/` contains runtime configuration templates and config loading notes.
  See `configs/README.md`.
- `scripts/` contains repository-level launcher and benchmark helper scripts.
  See `scripts/README.md`.
- `swebench/` contains the Docker-based SWE-bench runner. See
  `swebench/README.md`.
- `docs/` contains project documentation and architecture notes.

## Start Here

1. Create local runtime configuration from `configs/.env.example`.
2. Start OpenCollab from the repository root with `scripts/start_opencollab.sh`.
3. Use the nested READMEs above for setup details, CLI modes, evaluation, and
   benchmark tooling.

Do not commit real API keys or local runtime state.
