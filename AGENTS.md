# AGENTS.md

Guidance for coding agents (OpenAI Codex, Claude Code, and others) contributing to
**OpenCollab**. The checks below describe the repository workflows. Human
contributors can use [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

## Setup & the checks your change must pass

```bash
uv sync --locked --extra dev # create .venv with runtime + dev deps
uv run ruff check .          # lint the whole repository
uv run lint-imports          # enforce the existing architecture contracts
uv run deptry .              # check dependency use
uv run pytest -q             # execute the Python test suite
```

New behavior needs tests. Do not weaken or delete a test to make CI pass.

## Project structure & the one architecture rule

The core dependency direction is `bootstrap → adapters → application → domain`.
The existing `.importlinter` configuration records narrow CLI composition-root
exceptions and the workflow sibling-cycle exception.

```
adapters  →  application  →  domain
```

- `opencollab/domain/` — pure value objects + session FSM. **Stdlib only, no I/O.**
- `opencollab/application/` — use cases, scheduler, ports (`application/ports.py`). Imports `domain` + stdlib only.
- `opencollab/adapters/` — concrete impls (`cli/`, `tui/`, `llm/`, `tools/`, env, tracing, store).
- `opencollab/bootstrap/` — composition root; the only layer that knows concrete types.
- `opencollab/sdk/` — versioned integration surface for external workflow and evaluation packages.
- `scripts/` — framework launchers and provider diagnostics.

Never add an inward → outward import (`lint-imports` fails the build on it; the
contracts live in `.importlinter`).
Need an outer capability inside? Add a **port** in `application/ports.py`, wire the
concrete type in `bootstrap/`. When splitting a module, judge the split by interface
width — see the module rule below.

## Commits & pull requests

Commit authorship and `Co-authored-by` trailers are reserved for human contributors.

- **Conventional Commits**, with Chinese descriptions and an English type: `feat` `fix` `refactor` `docs` `test`
  `chore` `perf` `ci` `build` `style` `revert`. e.g. `feat: <Chinese description>`, `fix(tui): <Chinese description>`.
- **The PR title and merge commit subject must be valid Conventional Commits**,
  with Chinese descriptions and an English type.
- **One focused change per PR.** Don't bundle unrelated work.
- `refactor:` must be behavior-preserving.
- Merge pull requests with a merge commit so the feature-branch history and human
  authorship remain available. Keep commits focused on meaningful implementation,
  tests, or documentation changes.

## Repository checks

CI runs the Python suite on Python 3.10 through 3.14, whole-repository Ruff,
`lint-imports`, and `deptry`. The Python 3.12 job also runs the team blueprint DOM
regressions with Node 20 or newer. The distribution job builds a wheel from the
source archive, checks its contents and metadata, and exercises the installed
package outside the checkout. A macOS job executes the selected file and
terminal regressions. See [docs/testing.md](docs/testing.md) for local commands.

The Conventional Title workflow validates PR titles and direct-push subjects.
Its commit mode accepts merge commits because the PR title was checked before
merge. The contribution convention above also applies to the merge subject.

The Hygiene workflow rejects files newly added above **512,000 bytes** or grown
past that limit. Python modules crossing **800 lines** receive a warning and
leave the check successful. A push to `main` measures the complete tree. Choose
module boundaries by responsibility and public interface width.

The Security workflow scans proposed commit history against the trusted base's
existing audited secret baseline. Preserve its dedicated baseline-update
process in [CONTRIBUTING.md](CONTRIBUTING.md).

## Changes to validation and integrity mechanisms

Use Git history, version numbers, primary keys, transactions, unique constraints,
types, and ordinary tests before adding a hash, frozen contract, baseline, or
gate. An addition requires a concrete failure scenario and an explanation of why
those existing mechanisms cannot handle it. Preserve existing safety measures.
Place gates at irreversible, cross-system, security, or formal release
boundaries. Preparatory checks must leave room for actual execution, simulation,
or measurement.

## Conventions that keep the repo clean

- **English code and canonical documentation** — code, comments, and docs use English; commit and PR descriptions use Chinese while retaining a Conventional Commit type.
- **No hardcoded infrastructure.** Never bake a hostname, NFS path, username, private
  model name, or personal env-file path in as a default. Read them from env/CLI and
  fail fast if unset. (These leak topology and are useless to an external clone.)
- **No compiled artifacts or large binaries in git.** Commit `.tex`/`.md` sources, not
  the built PDFs; keep images small; put decks/datasets in release assets, not history.
- **Don't copy-paste or `base64`-embed logic that already exists as a tested module** —
  import it through the owning package's public interface.
- **Split by interface width, not by line count.** A module's *width* is the number of
  public top-level names it exports (`scripts/check_interface_width.py` measures it).
  - **A good split** leaves the two new modules with **no more** public names in total than
    the one they came from. Split by capability or by domain.
  - **A bad split** is by type (`models/`, `helpers/`, `utils/`) or by line count
    (`_foo_part2.py`) — both push the total number of public names up.
  - **800 lines is a hint, not a red line.** Long with a narrow interface is a good module;
    short with a wide one is not. When the hint fires, say in the PR description why this
    module is deep.

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

## ICLR integration history

The cumulative `integrate/iclr-2027` series was merged into `main` through PR
#154 on October 1, 2026. [docs/iclr-integration.md](docs/iclr-integration.md)
records its original sequence, source boundary, and validation results. Continue
new contributions from current `main` while retaining the existing ancestry.
