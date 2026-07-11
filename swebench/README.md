# SWE-bench Prediction Generation

This directory contains the host-runnable bridge between OpenCollab and the
official SWE-bench evaluation harness.

## `gen_prediction.py`

For one SWE-bench instance it:

1. starts the official `sweb.eval` image as a container (repo baked at
   `/testbed`, deps installed in the `testbed` conda env),
2. runs a single OpenCollab agent inside it (edits + can run tests),
3. captures `git diff` as the model patch,
4. appends one `{instance_id, model_name_or_path, model_patch}` line to a
   predictions JSONL.

Run with the OpenCollab venv (it must import `opencollab`):

```bash
opencollab/.venv/bin/python swebench/gen_prediction.py \
    --instance-file /path/to/swebench-eval/instance_sympy-20590.json \
    --output /path/to/swebench-eval/predictions-opencollab.jsonl
```

## Team-mode batch runs

For team-mode prediction runs across many instances, use
`scripts/run_team_batch.sh` (batch driver) and `scripts/start_team_run.sh`
(single instance). See `scripts/README.md` for the full eval workflow,
including subset runs and grading.
