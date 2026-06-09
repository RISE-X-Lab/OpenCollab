# SWE-bench Docker Runner

This directory contains the Docker-based SWE-bench runner for OpenCollab.

## Entrypoint

Run it from the repository root through the script wrapper:

```bash
scripts/run_swe_docker.sh --instance_ids django__django-15400
```

The wrapper builds the `swe-collab` image from `swebench/Dockerfile`
with the repository root as the Docker build context, then starts the runner
container.

## Runtime Mounts

The wrapper mounts:

- `configs/` for OpenCollab runtime configuration.
- `logs/` for prediction and evaluation output.
- `swe_workdir/` for benchmark working data.
- The Docker socket so the runner can control benchmark containers.

## Implementation

`run_swe_docker.py` runs inside the `swe-collab` container and controls
SWE-bench evaluation containers. It prefers official SWE-bench test specs and
grading when available, then falls back to return-code based checks when needed.

The default prediction output path is:

```text
logs/swe_predictions.jsonl
```
