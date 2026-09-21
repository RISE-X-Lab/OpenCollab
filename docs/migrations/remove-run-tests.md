# Retiring the built-in test runner

This change removes the `run_tests` tool and its pytest, Go, and Django runner
selection, output parsers, GREEN/RED verdicts, target evidence cache, and repeated
failure nudges. Agents run the repository's native test command through `bash`
and inspect the command, exit status, and output. Bash does not synthesize a
verified test verdict.

## Team and workflow migration

Replace `run_tests` with `bash` in role tool lists. If Bash is already present,
remove the duplicate entry. Remove `tool_limits.run_tests` and use
`tool_limits.bash.max_output_chars` when a shell output cap is needed. Update role
prompts to request the repository's actual test command, selected targets, and
observed results rather than a `run_tests` invocation or GREEN verdict.

```python
from opencollab.tools import builtin_tools

tools = builtin_tools("file_read", "bash", "git_diff")
```

Existing configurations requesting `run_tests` fail as an unknown built-in tool;
there is no silent alias that converts a tests-only capability into a shell.
Saved runs or external workflows that reconstruct that tool need migration
before resuming with this version. Existing installations and their saved
artifacts remain usable with their original version.

## Test command timeouts

Bash keeps its shared default timeout of **120 seconds**. The removed
`run_tests` tool defaulted to **300 seconds**, so migrating a test invocation
also changes its default time allowance. For a longer test command, supply
`timeout` in seconds as a Bash tool-call argument. Use `timeout=300` to preserve
the previous test-tool allowance, or a larger value such as `timeout=600` when
the suite needs more time.

For example, the agent or calling code can provide these tool-call arguments.

```python
test_call = dict(
    command="python -m pytest -q",
    timeout=600,
)
```

An explicit `timeout` applies to that invocation. Calls that omit it continue
to use the shared 120-second Bash default.

## Execution permissions

Bash requires a process-isolated environment unless the caller explicitly
authorizes host execution. Its existing
command policy, permission handling, timeout, process cancellation, and bounded
output remain unchanged. Tester defaults and shipped examples now include Bash.
Prebuilt teammates inherit the existing entry-agent shell policy. Dynamically
spawned children retain the process-isolation requirement even in interactive
mode. The CLI option `--allow-local-child-shell` or the scheduler builder option
`allow_unisolated_child_shell=True` explicitly authorizes host commands for those
children. This permission is broader than the removed tests-only permission and
keeps the existing command confirmation policy.

The team-issue demo requires this explicit opt-in. Run
`./scripts/demo_team_issue.sh --allow-local-child-shell` to allow its dynamically
spawned Coder and Tester to execute tests on the host.

The `--allow-local-child-tests` CLI switch and the internal
`allow_unisolated_tests` / `allow_unisolated_child_tests` options are removed with
the tests-only capability. They do not grant general shell access as a side
effect of migration. Use an isolated environment for unattended test commands.

## Verification integrations

The public structural `VerificationTool` protocol remains available for external
custom tools. No built-in tool implements its `verified_targets` property after
this removal. Integrations relying on parser-backed test evidence must supply
an explicit custom verifier or retain their existing runner version. A zero
shell exit code, help output, or zero collected tests alone does not prove that
the requested tests passed.

This removal does not change benchmark scoring, external evaluation runners,
OC's scheduler, or the Docker artifact-preservation fix. Historical design
records under `docs/archive/` describe the original implementation.
