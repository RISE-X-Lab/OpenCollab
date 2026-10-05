# CLAUDE.md

OpenCollab is a multi-agent software-development framework. The repository root
is the Python project root, and the package source lives in `opencollab/`.

## Architecture — strict clean architecture

The core dependency direction is `bootstrap → adapters → application → domain`.
The existing `.importlinter` configuration records the CLI composition-root
exceptions and workflow sibling-cycle exception.

- `domain/` — pure value objects + session FSM. Stdlib only, no I/O.
- `application/` — use cases, scheduler, ports (`application/ports.py`). Imports
  `domain` + stdlib only.
- `adapters/` — concrete impls: `cli/`, `tui/`, `llm/`, `tools/`, environments,
  tracing, session store.
- `bootstrap/` — composition root; the only layer that knows concrete types.
- `sdk/` — versioned integration surface used by external workflow and evaluation packages.

Never add an inward → outward import (enforced by `lint-imports`, see `.importlinter`).
Need an outer capability inside? Add a port in `application/ports.py`, wire the
concrete type in `bootstrap/`. When splitting a module, judge the split by interface
width, not line count — see `AGENTS.md`.

## Commands

```bash
uv sync --locked --extra dev    # create .venv with dev deps
uv run pytest -q                # tests (keep green)
uv run ruff check .             # lint the whole repository
uv run lint-imports              # architecture checks
uv run deptry .                  # dependency checks
uv run opencollab --workspace . # run the built-in Self-Collaboration team; add --team-config PATH for a declared team
```

Use an English Conventional Commit type with a Chinese description for commits
and PRs. `refactor` commits preserve behavior. Follow [AGENTS.md](AGENTS.md)
and [the testing guide](docs/testing.md) for the complete development checks.

Commit authorship and `Co-authored-by` trailers are reserved for human contributors.
