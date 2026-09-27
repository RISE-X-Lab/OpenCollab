# Development testing

Prepare the development environment from the repository root.

```bash
uv sync --locked --extra dev
```

Run the suite and repository checks with the existing development commands.

```bash
uv run pytest -q
uv run ruff check .
uv run lint-imports
uv run deptry .
```

The [test directory guide](../tests/README.md) maps behavior to directories.
Use a directory, topic file or pytest node ID to run the part being changed.

```bash
uv run pytest -q tests/agents
uv run pytest -q tests/workflows/test_duo_runtime.py
uv run pytest -q tests/tools/test_safe_files.py::test_read_rejects_fifo_without_blocking
```

The default suite includes `examples/mini-edict/tests`. Test workspaces use
pytest's system temporary directory. For logs or reports, select a location
outside the repository. This example saves a JUnit report and skips pytest's
cache for that run.

```bash
uv run pytest -q -p no:cacheprovider --junitxml=/tmp/opencollab-tests.xml
```

Shared test preparation uses explicit `tests.support` imports. Keep local
preparation with its topic until another topic needs it. Use
`tests.support.paths` when accessing repository files or starting a subprocess
that imports the installed package.
