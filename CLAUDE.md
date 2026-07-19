# CLAUDE.md

OpenCollab is a multi-agent software-development framework. The package lives in
`opencollab/`; source root `opencollab/opencollab/`.

## Architecture — strict clean architecture

Dependencies point inward only: `adapters → application → domain`.

- `domain/` — pure value objects + session FSM. Stdlib only, no I/O.
- `application/` — use cases, scheduler, ports (`application/ports.py`). Imports
  `domain` + stdlib only.
- `adapters/` — concrete impls: `cli/`, `tui/`, `llm/`, `tools/`, environments,
  tracing, session store.
- `bootstrap/` — composition root; the only layer that knows concrete types.
- `sdk/` — versioned integration surface used by external workflow and evaluation packages.

Never add an inward → outward import (enforced by `tests/test_*_boundaries.py`).
Need an outer capability inside? Add a port in `application/ports.py`, wire the
concrete type in `bootstrap/`. When splitting a module, keep its public names
re-exported.

## Commands

```bash
cd opencollab && uv sync --extra dev              # one-time: create .venv with dev deps
cd opencollab && .venv/bin/python -m pytest -q    # tests (keep green; ~1439 collected)
cd opencollab && .venv/bin/ruff check opencollab/ # lint
scripts/start_opencollab.sh                       # run the app (interactive; a team when configs/team.yaml is present)
```

Conventional commits; `refactor:` commits stay behavior-preserving.
