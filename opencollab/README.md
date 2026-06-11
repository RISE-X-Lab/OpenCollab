# OpenCollab Package

This directory contains the installable Python package and the `opencollab`
CLI.

## Install

From the repository root with `uv`:

```bash
uv venv opencollab/.venv
uv pip install --python opencollab/.venv/bin/python -e opencollab
```

Or with `pip`:

```bash
python3 -m venv opencollab/.venv
opencollab/.venv/bin/pip install -e opencollab
```

From inside this package directory:

```bash
pip install -e .
```

## Commands

The repository launcher is the preferred entrypoint during local development:

```bash
scripts/start_opencollab.sh
```

It uses `opencollab/.venv`, checks `configs/.env`, and starts chat mode by
default. Pass `team` to start team mode:

```bash
scripts/start_opencollab.sh team
```

Direct CLI commands are also available after installation. Activate the venv or
call the installed binary directly:

```bash
opencollab/.venv/bin/opencollab chat --workspace .
opencollab/.venv/bin/opencollab team --workspace .
opencollab/.venv/bin/opencollab eval tasks.jsonl --output eval_results --concurrency 1
```

## Headless Eval

The eval command reads JSONL tasks. Each line describes one task:

```json
{"task_id":"example","description":"Fix the bug described here.","repo_path":"/path/to/repo","timeout":600,"max_tokens":100000}
```

The harness writes `results.jsonl` and trajectory logs under the output
directory.

Use the Docker-based SWE-bench runner in `swebench/` for benchmark
container orchestration.

## Architecture

OpenCollab follows a strict clean architecture: dependencies point inward only,
`adapters → application → domain`.

- `opencollab/domain/` — pure value objects and the session FSM. Stdlib only,
  no I/O.
- `opencollab/application/` — use cases, the scheduler, messaging, and the port
  Protocols (`opencollab/application/ports.py`). Imports `domain` + stdlib only.
- `opencollab/adapters/` — concrete implementations: `cli/`, `tui/`, `llm/`,
  `tools/`, environments, tracing, and the session store.
- `opencollab/bootstrap/` — the composition root; the only layer that knows
  concrete types.
- `opencollab/harness/` — the headless evaluation runner.
- `tests/` — characterization and regression tests, including the import-
  direction guards `test_domain_boundaries.py` and
  `test_application_boundaries.py`.

## Making Changes

1. Locate the module by listing or grepping the layer directories above — the
   layout is small and the names are descriptive.
2. Respect the dependency direction `adapters → application → domain`. Ports
   live in `opencollab/application/ports.py`; only `bootstrap/` knows concrete
   types.
3. Tests live in `tests/`. Run them from this package directory.
4. If your change moves or renames modules, update the affected README in the
   same commit.

An archived module-by-module map and dependency graph (a snapshot, not kept in
sync) lives under `../docs/archive/repomap/`.
