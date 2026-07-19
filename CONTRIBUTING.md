# Contributing to OpenCollab

Thanks for your interest in improving OpenCollab! This guide covers how to set
up a development environment, the checks your change must pass, and the one
architectural rule the codebase enforces.

## Project layout

OpenCollab follows a strict clean architecture — dependencies point inward only:

```
adapters  →  application  →  domain
```

- `opencollab/domain/` — pure value objects + the session FSM. Standard library only, no I/O.
- `opencollab/application/` — use cases, scheduler, ports (`application/ports.py`). Imports `domain` + stdlib only.
- `opencollab/adapters/` — concrete implementations: `cli/`, `tui/`, `llm/`, `tools/`, environments, tracing, session store.
- `opencollab/bootstrap/` — composition root; the only layer that knows concrete types.
- `opencollab/sdk/` — the versioned boundary for external workflow and evaluation packages.
- `scripts/` — framework launchers and provider diagnostics.

## Development setup

OpenCollab uses [uv](https://docs.astral.sh/uv/). From the repository root:

```bash
uv sync --extra dev            # create .venv with runtime + dev dependencies
```

Copy the example config and point it at any OpenAI-compatible (or Anthropic)
endpoint — **never commit real API keys**:

```bash
cp configs/.env.example configs/.env   # then set OPENCOLLAB_API_KEY
```

## Checks (must pass before a PR)

```bash
uv run ruff check .   # lint the whole repository (root ruff.toml)
uv run pytest -q      # test suite
```

New behavior needs tests, and the suite must stay green.

### Enforced automatically in CI

- **Lint** — `ruff check .` over the whole repository; config lives in the
  repository-root `ruff.toml`.
- **PR title** — must follow Conventional Commits; squash-merge uses it as the
  commit subject on `main`.
- **File hygiene** — a newly added file over 500 KB, or a new `.py` module over
  800 lines, fails the build. Commit `.tex`/`.md` sources, not compiled PDFs.

Optionally mirror these locally: `pip install pre-commit && pre-commit install`.

## The architecture rule (enforced by tests)

`tests/test_*_boundaries.py` fail the build on any inward → outward import.

- Never import an outer layer from an inner one (e.g. `domain` importing `adapters`).
- Need an outer capability inside? Add a **port** in `application/ports.py`, then
  wire the concrete implementation in `bootstrap/`.
- When splitting a module, keep its public names re-exported so import paths stay stable.

## Commits & pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat`, `fix`,
  `refactor`, `docs`, `test`, `chore`, `perf`, `ci`.
- `refactor:` commits must stay behavior-preserving.
- Keep pull requests focused; describe what changed and how you verified it.
- All public-facing text (code, comments, docs, commit messages) should be in English.

## Reporting issues

- Functional bugs and feature requests: open a GitHub issue using the templates.
- Security vulnerabilities: **do not** open a public issue — see [SECURITY.md](SECURITY.md).
