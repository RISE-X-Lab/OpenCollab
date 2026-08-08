# Contributing to OpenCollab

This guide explains how to prepare a development environment and submit changes
that satisfy OpenCollab's checks and dependency rule.

## Project layout

OpenCollab follows strict clean architecture. Dependencies point inward.

```
adapters  →  application  →  domain
```

- `opencollab/domain/` contains pure value objects and the session FSM. It uses the standard library and performs no I/O.
- `opencollab/application/` contains use cases, the scheduler, and ports in `application/ports.py`. It imports `domain` and the standard library.
- `opencollab/adapters/` contains the CLI, TUI, LLM providers, tools, environments, tracing, and session store.
- `opencollab/bootstrap/` is the composition root and the only layer that knows concrete types.
- `opencollab/sdk/` is the versioned boundary for external workflow and evaluation packages.
- `scripts/` contains framework launchers and provider diagnostics.

## Development setup

OpenCollab uses [uv](https://docs.astral.sh/uv/). Run the following command from
the repository root.

```bash
uv sync --extra dev            # create .venv with runtime + dev dependencies
```

Copy the example config and point it at an OpenAI-compatible or Anthropic
endpoint. **Never commit real API keys.**

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

- **Lint** runs `ruff check .` over the whole repository. Config lives in the
  repository-root `ruff.toml`.
- **PR title** must follow Conventional Commits. Squash-merge uses it as the
  commit subject on `main`.
- **File hygiene** rejects any file your change pushes over 500 KB, and any `.py`
  module it pushes over 800 lines — appending to an existing module counts.
  Commit `.tex`/`.md` sources, not compiled PDFs.

To run the same hooks locally, use
`pip install pre-commit && pre-commit install`.

## The architecture rule (enforced by tests)

`tests/test_*_boundaries.py` fail the build on any inward → outward import.

- Never import an outer layer from an inner one (e.g. `domain` importing `adapters`).
- Need an outer capability inside? Add a **port** in `application/ports.py`, then
  wire the concrete implementation in `bootstrap/`.
- When splitting a module, keep its public names re-exported so import paths stay stable.

## Commits & pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/) in English.
  `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`.
- `refactor:` commits must stay behavior-preserving.
- Keep pull requests focused. Describe what changed and how you verified it.
- Keep code, comments, tracked documentation, commit summaries, pull request
  titles, pull request descriptions, and review replies in English.

## Contribution license

By submitting a contribution, you represent that you have the legal right to
provide it and that it does not knowingly include material you cannot license.
You license the contribution to the project under the
[Mulan Permissive Software License v2](LICENSE), the same license that applies
to the repository. OpenCollab currently uses this inbound-equals-outbound rule
without requiring a Developer Certificate of Origin sign-off.

Secret-baseline updates use a dedicated, baseline-only pull request. A
maintainer applies the `security-baseline-update` label after reviewing every
entry. The security workflow rejects a baseline change mixed with source or
documentation changes.

## Reporting issues

- Functional bugs and feature requests: open a GitHub issue using the templates.
- Security vulnerabilities must not be reported in a public issue. Use the
  private process in [SECURITY.md](SECURITY.md).
