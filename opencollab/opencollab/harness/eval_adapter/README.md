# eval_adapter

`eval_adapter` is the benchmark boundary for the evaluation harness. It converts
dataset rows and runtime details into solver-facing models, then classifies
official-evaluation infrastructure failures into stable fields.

The adapter uses these models:

| Model | Responsibility |
| --- | --- |
| `TaskSpec` | Normalized task identity, repository, problem statement, base commit, image, tests, and service requirements |
| `WorkspaceSpec` | Image, repository-root candidates, services, and environment required to start a workspace |
| `PreparedWorkspace` | Ready container workspace with its container ID, repository root, working directory, and cleanup callback |
| `PatchCandidate` | Solver patch, patch SHA, log paths, token usage, and cost |
| `EvalResult` | Official-evaluation completion, resolved state, technical-failure state, reasons, and log paths |
| `RunRecord` | Final per-task record combining the task, candidate, and evaluation result |

Pro-Lite-specific rules live in `prolite.py`. They cover JSONL dataset loading,
Docker image names, `/app`-first repository discovery, NodeBB Redis requirements,
empty-patch records, and technical-failure classification for Redis, SSH, Docker,
timeouts, test-patch application, and missing reports.
