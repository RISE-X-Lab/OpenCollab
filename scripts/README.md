# Scripts

Helper scripts for running OpenCollab from the repository root.

## `start_opencollab.sh`

Bootstraps the local Python environment and starts OpenCollab.

```bash
scripts/start_opencollab.sh
```

By default, this starts chat mode:

```bash
opencollab/.venv/bin/opencollab chat --workspace <repo-root>
```

Start team mode instead:

```bash
scripts/start_opencollab.sh team
```

Pass extra OpenCollab CLI arguments after the mode:

```bash
scripts/start_opencollab.sh team --trace
scripts/start_opencollab.sh chat --yolo
```

Show script help:

```bash
scripts/start_opencollab.sh --help
```

## Configuration

The script expects runtime config in:

```text
configs/.env
```

If that file is missing, the script creates it from:

```text
configs/.env.example
```

Then it exits so you can add a real API key:

```dotenv
OPENCOLLAB_API_KEY=<your-api-key>
```

For DashScope-compatible mode, `DASHSCOPE_API_KEY` is also accepted and is
preferred over generic API-key variables for DashScope base URLs.

The API key can also come from the process environment:

```bash
export OPENCOLLAB_API_KEY=<your-api-key>
scripts/start_opencollab.sh
```

Do not commit `configs/.env`.

## Environment Bootstrap

If `opencollab/.venv/bin/opencollab` already exists, the script uses it.

Otherwise it tries, in order:

1. `uv venv opencollab/.venv` and `uv pip install --python opencollab/.venv/bin/python -e opencollab`
2. `python3 -m venv opencollab/.venv` and `opencollab/.venv/bin/pip install -e opencollab`

If neither `uv` nor `python3` is available, the script exits with an error.

## Generated Files

OpenCollab may create local runtime state under:

```text
.opencollab/
```

That directory is ignored by git.

## `run_swe_docker.sh`

Builds and runs the SWE-bench Docker harness.

```bash
scripts/run_swe_docker.sh --instance_ids django__django-15400
```

The implementation lives under:

```text
tools/swe_bench/
```

The script builds `tools/swe_bench/Dockerfile` with the repository root as the
Docker build context, then runs the resulting `swe-collab` image with mounted
`configs/`, `logs/`, `swe_workdir/`, and the Docker socket.

See `tools/swe_bench/README.md` for runner implementation details.
