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

## Package Layout

- `opencollab/cli/` contains Typer CLI entrypoints for chat, team, and eval.
- `opencollab/core/` contains configuration, environment, providers, sessions,
  tracing, and runtime primitives.
- `opencollab/team/` contains team orchestration.
- `opencollab/tui/` contains terminal UI adapters.
- `opencollab/tools/` contains tool implementations.
- `opencollab/harness/` contains the local headless evaluation harness.
- `tests/` contains characterization and regression tests.
