# Evolution workflow

Evolution executes ordered groups of work in fresh agent sessions against the
same workspace. Each group receives a share of the available token, step and
time allowance. Executed checks provide the evidence for handoffs and determine
whether the final delivery passes. Failed checks can trigger a bounded sequence
of fresh repair sessions.

Evolution ships with the installed OpenCollab package. The public SDK resolves
`"evolution"` by name, and `opencollab workflow list` displays it alongside Duo.

## Run a file task from the CLI

Prepare a small workspace and a verifier that checks the delivered behavior.
The verifier imports the module, calls its public function, and compares the
returned value with the requested greeting.

```bash
mkdir -p /tmp/evolution-greeting
cat > /tmp/evolution-greeting/check_greeting.py <<'PY'
from greeting import greet

assert greet("Ada") == "Hello, Ada!"
assert greet("Lin") == "Hello, Lin!"
print("GREETING_CHECK_PASSED")
PY

cat > /tmp/evolution-greeting/flow.json <<'JSON'
{
  "groups": [
    {
      "id": "greeting",
      "prompt": "Create greeting.py with greet(name) returning Hello, followed by the supplied name and an exclamation mark. For example, greet('Ada') returns 'Hello, Ada!'.",
      "resources": ["greeting.py"]
    },
    {
      "id": "command",
      "prompt": "Add a command-line entry to greeting.py. Running python greeting.py Ada must print Hello, Ada! Keep the greet(name) function working.",
      "dependencies": ["greeting"],
      "resources": ["greeting.py"]
    }
  ],
  "check_commands": [
    {"command": "python check_greeting.py", "expected_output": "GREETING_CHECK_PASSED"},
    {"command": "python greeting.py Ada", "expected_output": "Hello, Ada!"}
  ],
  "config": {"max_repair_rounds": 2}
}
JSON

uv run opencollab workflow run evolution \
  --workspace /tmp/evolution-greeting \
  --args "$(cat /tmp/evolution-greeting/flow.json)" \
  --budget 30000 --agent-profile single2

cd /tmp/evolution-greeting
python check_greeting.py
python greeting.py Ada
```

Configure the provider and model through the usual
[configuration files](../configs/README.md). The commands run from the workspace
supplied with `--workspace`. Use the Python environment containing the task's
runtime dependencies in each check command.

The JSON result's `delivery_ok` field records whether the final executed checks
passed. Group generation, command completion and delivery verification have
separate results. A check command can use parser-backed pytest, Go or Django
test output. An executable probe uses a `command` and its `expected_output`, as
shown above. Include assertions in the probe so the expected output follows
the behavior being checked.

## Run through the public SDK

This script uses the installed package, creates ordinary files, and verifies
the module through the same interpreter running the SDK.

```python
import asyncio
import shlex
import sys
from pathlib import Path

from opencollab import OpenCollab


async def main():
    workspace = Path("/tmp/evolution-sdk-greeting")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "check_greeting.py").write_text(
        "from greeting import greet\n"
        "assert greet('Ada') == 'Hello, Ada!'\n"
        "print('GREETING_CHECK_PASSED')\n",
        encoding="utf-8",
    )
    result = await OpenCollab(workspace).workflow(
        "evolution",
        {
            "groups": [
                {
                    "id": "greeting",
                    "prompt": "Create greeting.py with greet(name) returning 'Hello, ' plus name plus '!'.",
                    "resources": ["greeting.py"],
                }
            ],
            "check_commands": [
                {
                    "command": f"{shlex.quote(sys.executable)} check_greeting.py",
                    "expected_output": "GREETING_CHECK_PASSED",
                }
            ],
            "config": {"max_repair_rounds": 2},
        },
        budget=30000,
        limit_mode="explicit",
        agent_profile="single2",
    )
    result.raise_for_status()
    assert result.output["delivery_ok"], result.output
    print(result.output)


asyncio.run(main())
```

`limit_mode="explicit"` keeps the SDK allowance finite even when the environment
selects unbounded limits. The SDK derives the workflow's default allowance from
its current remaining budget. `config.main_budget` can set the initial soft
allowance, while the actual hard allowance remains bounded by the SDK grant.
Completed native edits and observed passing tests can support soft allowance
extensions during a session. The persisted usage records keep cumulative tokens
and steps for continuation.

## Groups and custom verification

Each group has a unique `id` and a nonempty `prompt`. `weight` defaults to one.
`dependencies` order groups by their ids or target ids, and `resources` identify
shared files or other affected resources. `targets` defaults to the group id.
Groups in a dependency cycle share one session. Later groups can read edits
made by earlier groups while starting with fresh conversation histories.
`plan_evolution_groups(groups)` returns the ordered groups with cycles combined
and dependencies expressed as target ids. Application loaders can pass that
plan directly to `run_evolution`.

For application-specific checks, call `run_evolution` from a wrapper workflow
and supply an `EvolutionAdapter`. The public types are exported from
`opencollab.builtin_workflows`.

```python
from opencollab.builtin_workflows import (
    EvolutionAdapter,
    EvolutionCheck,
    EvolutionGroup,
    run_evolution,
)


class ApplicationChecks(EvolutionAdapter):
    async def verify(self, ctx, targets, seconds):
        report = await run_application_checks(ctx, targets, timeout=seconds)
        return EvolutionCheck(
            ok=report["passed"],
            executed=report["executed"],
            report=report,
            repair_targets=tuple(report["failed_targets"]),
            progress_markers=tuple(report["passed_targets"]),
        )


async def application_workflow(ctx, args):
    return await run_evolution(
        ctx,
        [EvolutionGroup("greeting", "Implement the greeting function")],
        adapter=ApplicationChecks(),
    )
```

`run_application_checks` is the application's executable verifier. Its report
should describe the checks that ran and the behavior they observed. Evolution
requires both `ok` and `executed` for a passing check. Repair targets select the
remaining work, and progress markers provide measured improvement between
rounds. `max_repair_rounds` and `stagnant_round_limit` bound repair attempts.

An adapter's `save_state(data)` hook receives independent JSON snapshots for
storage. Supply a saved snapshot as the JSON `state` input or through
`EvolutionState(saved_snapshot)` with `run_evolution`. Reuse the saved `run_id`
when calling the SDK. Generated groups continue at their checks, checked groups
retain their generation results, and consumed repair rounds remain counted.
The saved group fields must agree with the supplied groups.
