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

## Execution permissions

Headless Bash still requires a process-isolated environment. Its existing
command policy, permission handling, timeout, process cancellation, and bounded
output remain unchanged. Tester defaults and shipped examples now include Bash.
Interactive teammates inherit the existing entry-agent shell policy.

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
