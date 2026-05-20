# Step16 - Move `cli/` under `adapters.cli`

Date: 2026-05-20
Branch: `refactor/step16-adapters-cli` off the Step 15 branch.

> Second post-`core/` cleanup step. Step 15 deleted the stale top-level
> `team/` compatibility package. The next smallest target-map mismatch is the
> top-level `cli/` package: the target slots the command-line interface under
> `adapters.cli`, and the live tree still has `opencollab/cli/main.py`.
>
> This is a mechanical package relocation. It should not change CLI behavior,
> Typer command names, eval/team/chat behavior, TUI wiring, config loading, or
> provider behavior.

## Goal

1. Move the Typer CLI module from `opencollab.cli.main` to
   `opencollab.adapters.cli.main`.
2. Retarget the package entry points that still import the old CLI path.
3. Delete the top-level `opencollab/opencollab/cli/` package.
4. Keep the application/domain boundary guards that forbid future
   `opencollab.cli` imports.

End state: `opencollab/cli/` does not exist; the console script and
`python -m opencollab` still work; CLI code lives in the adapter layer.

## Current Evidence

`cli/` after Step 15:

```text
cli/__init__.py
cli/main.py          # Typer app, chat/team/eval commands, main()
```

Live references to `opencollab.cli` from the Step 15 branch:

```text
opencollab/opencollab/__main__.py:2
    from opencollab.cli.main import main

opencollab/pyproject.toml:30
    opencollab = "opencollab.cli.main:app"
```

The remaining `opencollab.cli` strings outside those two live references are
boundary guards or documentation strings:

```text
opencollab/tests/test_application_boundaries.py
opencollab/tests/test_domain_boundaries.py
opencollab/tests/test_context_compaction_use_case.py
```

Those should stay. They assert the inner layers do not import CLI code.

One important non-surprise: `cli/main.py` already behaves like an adapter. It
parses user input, uses TUI rendering, reads config, and calls bootstrap
builders. Moving it under `adapters/cli` does not require application/domain
changes.

## Implementation Plan

Single branch, suggested two commits.

### 1. Move the CLI package into adapters

Create the adapter package and move the existing files:

```bash
mkdir -p opencollab/opencollab/adapters/cli
git mv opencollab/opencollab/cli/main.py opencollab/opencollab/adapters/cli/main.py
git mv opencollab/opencollab/cli/__init__.py opencollab/opencollab/adapters/cli/__init__.py
```

Retarget the two live entry points:

- `opencollab/opencollab/__main__.py`
  - `from opencollab.cli.main import main`
  - to `from opencollab.adapters.cli.main import main`
- `opencollab/pyproject.toml`
  - `opencollab = "opencollab.cli.main:app"`
  - to `opencollab = "opencollab.adapters.cli.main:app"`

Do not leave a compatibility shim at `opencollab.cli`. The target map has no
top-level `cli/`, and this step is intended to make that literally true.

Suggested commit:

```text
refactor(cli): move command adapter under adapters
```

### 2. Verify CLI import behavior and the full suite

Run the import search first:

```bash
rg "from opencollab\.cli|import opencollab\.cli|opencollab\.cli\.main" \
  opencollab/opencollab opencollab/tests opencollab/pyproject.toml
```

Expected: no live import/path references. Boundary-test string guards may still
mention plain `opencollab.cli`.

Run CLI smoke checks from the package directory:

```bash
OPENAI_API_KEY=fake-test-key uv run python -m opencollab --help
OPENAI_API_KEY=fake-test-key uv run opencollab --help
```

Expected: both commands show the same Typer help as before. If the installed
console script is stale in the local environment, run the first command as the
required smoke check and note the console-script limitation in the commit
summary.

Run the normal verification:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
git diff --check
```

Expected: full suite remains **162 passed** unless unrelated tests changed
after the Step 15 baseline.

Suggested commit if kept separate from the move:

```text
test(cli): verify relocated command adapter
```

## Acceptance Criteria

- `opencollab/opencollab/cli/` does not exist.
- `opencollab/opencollab/adapters/cli/main.py` contains the Typer CLI.
- `python -m opencollab --help` imports
  `opencollab.adapters.cli.main.main` and succeeds.
- The package script in `opencollab/pyproject.toml` points at
  `opencollab.adapters.cli.main:app`.
- No live `from opencollab.cli` / `import opencollab.cli` /
  `opencollab.cli.main` reference remains in production code, tests, or
  packaging metadata.
- Boundary tests still forbid application/domain imports of `opencollab.cli`.
- Full suite passes.

## Non-Goals

- Do **not** move `tools/` to `adapters.tools` in this step. That is the next
  broader relocation and touches bootstrap, harness, concrete-tool tests, and
  package-walk guards.
- Do **not** decide the fate of `harness/` here. `cli/main.py` may continue to
  lazily import `opencollab.harness.evaluator` for the `eval` command.
- Do **not** change Typer command names, options, defaults, or output text
  except for unavoidable module-path internals.
- Do **not** change TUI behavior or move `adapters/tui`.
- Do **not** weaken the application/domain boundary tests' `opencollab.cli`
  guard.

## Rollback Plan

This should be one mechanical package-move commit. Reverting it restores the
old `opencollab.cli.main` path and pyproject entry point. Any failure after
the move should name a missed import path directly; retarget it to
`opencollab.adapters.cli.main` rather than reintroducing a compatibility shim.

## Closing note - remaining target-map gaps

After Step 16, the remaining literal repomap mismatches are:

- `tools/` -> `adapters.tools`
- `harness/` decision: relocate as out-of-scope evaluator tooling or leave it
  clearly outside the clean-architecture target

The `tools/` move should be Step 17. It is larger because concrete tools are
referenced by bootstrap factories, teammate factory wiring, tests, and the
SWE-bench harness.
