# SWE-bench Prediction Generation

This directory contains the host-runnable bridge between OpenCollab and the
official SWE-bench evaluation harness.

## `gen_prediction.py`

For one SWE-bench instance it:

1. starts the official `sweb.eval` image as a container (repo baked at
   `/testbed`, deps installed in the `testbed` conda env),
2. runs a single OpenCollab agent inside it (edits + can run tests),
3. copies a bounded workspace archive after container quiescence and extracts
   the patch with trusted host Git against a pre-Solver anonymous baseline,
4. appends one `{instance_id, model_name_or_path, model_patch}` line to a
   predictions JSONL.

Run with the OpenCollab venv (it must import `opencollab`):

```bash
opencollab/.venv/bin/python swebench/gen_prediction.py \
    --instance-file /path/to/swebench-eval/instance_sympy-20590.json \
    --output /path/to/swebench-eval/predictions-opencollab.jsonl
```

## Workflow and batch runs

Use `gen_prediction_workflow.py` for one blind workflow task and
`scripts/swe_v1_prolite_runner.py` for current remote batches. The historical
`run_team_batch.sh` and `start_team_run.sh` entrypoints now return technical
status 125 before starting a solver because their mount model cannot provide
the current isolation and trusted-extraction evidence.
