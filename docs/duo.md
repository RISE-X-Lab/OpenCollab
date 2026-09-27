# Duo

[Chinese guide](duo/README.zh-CN.md)

Duo is OpenCollab's built-in dual-coder workflow, previously named G22 in
OpenCollab-Eval. Install OpenCollab to run it through the CLI or Python SDK.
The ordinary agent and the optional Single2 profile can drive the same Duo
workflow.

## Run Duo

Configure a model endpoint using [the configuration guide](../configs/README.md).
Point the workspace at the Git repository to repair. Installed workflows are
available even when that repository has no `workflows/` directory.

```bash
opencollab workflow list --workspace /path/to/repository
```

Duo's shell tools require a process-isolated environment by default. A caller
running trusted tasks in a local Git workspace can explicitly permit local
shell execution through the workflow input.

```bash
opencollab workflow run duo --workspace /path/to/repository \
  --args '{"goal":"Fix the public issue described here.","allow_unisolated_shell":true}'
```

Select Single2 for every role using the CLI option.

```bash
opencollab workflow run duo --workspace /path/to/repository \
  --agent-profile single2 \
  --args '{"goal":"Fix the public issue described here.","allow_unisolated_shell":true}'
```

The Python SDK accepts the installed name or public workflow function. The
caller chooses model configuration, workspace, limits, and artifacts using
the same SDK options as other workflows.

```python
import asyncio

from opencollab import OpenCollab


async def main():
    client = OpenCollab("/path/to/repository")
    inputs = {
        "goal": "Fix the public issue described here.",
        "allow_unisolated_shell": True,
    }
    result = await client.workflow(
        "duo", inputs,
        budget=1_000_000,
        artifacts="artifacts/duo-run",
    )
    print(result.raise_for_status().output)

asyncio.run(main())
```

Import `duo` from `opencollab.builtin_workflows` to pass the function directly
as the first argument. Add `agent_profile="single2"` to the workflow call to
select Single2 for all roles.

For container-backed tasks, pass the public environment object to
`OpenCollab(..., environment=environment)` and keep the default shell setting.
The candidate workspace comes from that environment. Integrations can supply
their own candidate workspace port through `candidate_workspace=`.

## Candidate generation and selection

Coder A implements a minimal repair in an isolated candidate workspace. Coder
B starts from the same source state in another isolated workspace and checks
the issue across producers, consumers, APIs, and lifecycle boundaries. B
receives the public test command observed from A. Each role keeps its own
complete diff and [native test evidence](test-evidence.md).

Duo handles empty candidates and identical diffs mechanically. Comparable
public test records require the same target, runner, and command. A passing
record against a failing record can select a candidate directly. When those
rules leave the choice open, a structured adjudicator compares the complete
public task and candidate evidence against each explicit requirement.

The adjudicator selects B when supported evidence establishes an advantage
without losing a requirement better covered by A. Invalid, incomplete, or
unavailable adjudication falls back to A. Adoption first tries the selected
candidate, then the other nonempty candidate if the first adoption fails.

`goal` supplies the public task. `description` remains an accepted alternative.
`injected_test_paths` lets evaluation integrations preserve their prepared
test files while applying a candidate. Budgets, steps, model timeouts, and
agent profiles remain caller settings. A role's `budget=None` participates in
the existing shared workflow budget.

## Duo v3 and existing identifiers

The short name `duo` retains the former G22 v2 behavior. Its adjudicator receives
candidate diffs and public evidence in the prompt and has only the structured
submission tool. Select `duo-v3` for the file-evidence variant. It retains
complete candidate files and adds `read_candidate_evidence` so the adjudicator
can page through indexes, public records, and exact diff ranges.

```python
from opencollab.builtin_workflows import duo_v3

result = await client.workflow(
    duo_v3,
    {
        "goal": task,
        "candidate_evidence_dir": "artifacts/candidate-evidence",
        "allow_unisolated_shell": True,
    },
    agent_profile="single2",
)
```

`candidate_evidence_dir` chooses a host-side parent directory. Each v3
adjudication creates a separate child directory and records its location in
workflow logs. The caller owns retention of these files.

| CLI and SDK name | Public function | Behavior |
| --- | --- | --- |
| `duo` | `duo` | Original G22 v2 selector |
| `duo-v3` | `duo_v3` | G22 v3 selector with complete file evidence |
| `validation-council-dual-coder-selection-v2` | `validation_council_dual_coder_selection_v2` | Compatibility name for `duo` |
| `validation-council-dual-coder-selection-v3` | `validation_council_dual_coder_selection_v3` | Compatibility name for `duo-v3` |

All functions in the table are exported from `opencollab.builtin_workflows`.
`get_builtin_workflows()` returns a fresh registry of these installed names.
The CLI and SDK name lookup merge that registry with caller modules in the
workspace's `workflows/` directory. `OPENCOLLAB_WORKFLOWS_DIR` selects another
directory, with relative paths resolved from the workspace. A caller module
using an installed name raises the existing duplicate-registration error.
Passing a workflow function or spec directly uses the supplied object.

## Inspect the result

`RunResult` describes completion of the framework runtime. Duo's output records
its task outcome separately. Its `status` is `done` after a candidate is adopted,
`incomplete` when adoption produces no patch, or `error` for a missing task.
`winner` is the selected candidate and `adopted` is the candidate actually applied.
They can differ when adoption falls back. `selection_reason`, `judge_used`,
`judge_result`, `adoption_attempts`, `shared_public_command`, and the per-candidate
diff paths and public records explain the decision.

OpenCollab-Eval prepares benchmark tasks and environments, isolates hidden
tests, captures final patches, and performs official scoring. Its existing
`oc-eval g22` command remains available and calls the OpenCollab-owned workflow.
See the [evaluation quick start](https://github.com/RISE-X-Lab/OpenCollab-Eval#duo-quick-start)
for benchmark setup and official reports.
