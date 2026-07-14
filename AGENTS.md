# AGENTS.md

Guidance for coding agents (OpenAI Codex, Claude Code, and others) contributing to
**OpenCollab**. Follow it exactly — CI enforces most of it, and a PR that ignores it
will be blocked. Human contributors: see [CONTRIBUTING.md](CONTRIBUTING.md) for the
long form.

## Setup & the checks your change must pass

```bash
cd opencollab && uv sync --extra dev          # create .venv with runtime + dev deps
uv run --project opencollab ruff check .      # run from the REPO ROOT — lints the whole tree
cd opencollab && uv run pytest -q             # test suite; must stay green
```

New behavior needs tests. Do not weaken or delete a test to make CI pass.

## Project structure & the one architecture rule

Strict clean architecture — dependencies point **inward only**:

```
adapters  →  application  →  domain
```

- `opencollab/opencollab/domain/` — pure value objects + session FSM. **Stdlib only, no I/O.**
- `opencollab/opencollab/application/` — use cases, scheduler, ports (`application/ports.py`). Imports `domain` + stdlib only.
- `opencollab/opencollab/adapters/` — concrete impls (`cli/`, `tui/`, `llm/`, `tools/`, env, tracing, store).
- `opencollab/opencollab/bootstrap/` — composition root; the only layer that knows concrete types.
- `opencollab/opencollab/sdk/` — versioned integration surface for external workflow and evaluation packages.
- `scripts/` — framework launchers and provider diagnostics.

Never add an inward → outward import (`tests/test_*_boundaries.py` fail the build on it).
Need an outer capability inside? Add a **port** in `application/ports.py`, wire the
concrete type in `bootstrap/`. When splitting a module, keep its public names re-exported.

## Commits & pull requests

- **Conventional Commits**, in **English**: `feat` `fix` `refactor` `docs` `test`
  `chore` `perf` `ci` `build` `style` `revert`. e.g. `feat: add X`, `fix(tui): handle Y`.
- **The PR title must itself be a valid Conventional Commit** — merges are squashed, so
  the PR title becomes the commit subject on `main`.
- **One focused change per PR.** Don't bundle unrelated work.
- `refactor:` must be behavior-preserving.
- Feature branches are squash-merged, so keep review churn out of `main` — don't rely on
  a merge preserving your intermediate commits.

## Hard gates CI will fail your PR on

1. **Lint** — `ruff check .` over the **whole** repository. Config is the
   repo-root `ruff.toml` (line-length 120, py310).
2. **PR title** — Conventional Commits (see above).
3. **File hygiene** — a newly added file over **500 KB**, or a new `.py` module over
   **800 lines**, fails the build.

## Conventions that keep the repo clean

- **English everywhere public-facing** — code, comments, docs, commit messages, PR text.
  This repo ships to a public/OSS audience; no Chinese in tracked files.
- **No hardcoded infrastructure.** Never bake a hostname, NFS path, username, private
  model name, or personal env-file path in as a default. Read them from env/CLI and
  fail fast if unset. (These leak topology and are useless to an external clone.)
- **No compiled artifacts or large binaries in git.** Commit `.tex`/`.md` sources, not
  the built PDFs; keep images small; put decks/datasets in release assets, not history.
- **Don't copy-paste or `base64`-embed logic that already exists as a tested module** —
  import it through the owning package's public interface.
- **Keep modules focused** (< 800 lines) — split by feature/domain, not by type.

## Executable evidence integrity

OpenCollab tools and workflows must distinguish command completion from verified
behavior. A successful process exit is evidence only for the command that actually
ran.

- Never report a test as passed unless the requested test targets executed and passed.
- Empty commands, zero collected tests, help output, and collection-only runs are
  unverified outcomes.
- A verifier role must retain an executable probe before it may issue a passing verdict.

## See also

- [CONTRIBUTING.md](CONTRIBUTING.md) — full contributor guide and dev setup.
- [CLAUDE.md](CLAUDE.md) — repo notes for Claude Code.
- [SECURITY.md](SECURITY.md) — report vulnerabilities privately; never in a public issue.
