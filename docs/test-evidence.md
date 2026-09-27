# Native test evidence

`opencollab.tools.evidence_tools` composes the same native tools as
`builtin_tools` and wraps Bash with an execution observer. Workflows can use the
retained records when comparing candidate patches.

```python
from opencollab.tools import evidence_tools

tools = evidence_tools(
    "bash", "file_read", "file_write", "apply_patch", "grep", "git_diff",
    headless=True,
    allow_file_creation=True,
)
candidate = await ctx.candidate_agent(prompt, tools=tools, label="coder")
print(candidate.test_records)
print(candidate.verified_targets)
```

The constructor accepts the native `limits` mapping and inherits the current
workflow's agent profile defaults. Each invocation creates independent tool
instances. Bash retains its schema, command, timeout, formatted output, process
isolation requirement and safety policy.

The observer recognizes direct pytest invocations, their supported Python and
package-manager wrappers, `go test -json`, and Django's `python tests/runtests.py`.
A leading `cd directory &&` supplies the execution workspace for relative test
targets. Run pytest with `-rA` to retain its per-test passing results.

`BashEvidence.verification_records` returns copies of the observed target,
runner, command, workspace, exit code and verified state. `verified_targets`
returns the current passing targets. An overlapping rerun clears previous
passing targets before executing, so failed, interrupted, empty and
collection-only runs update the current evidence. Truncated captured output
also produces an unverified record. The workflow candidate runtime reads these
two properties through its existing tool interface.

`opencollab.tools.BashEvidence` can wrap an explicitly configured Bash tool.
`opencollab.tools.has_pass_evidence` interprets an already captured test result
using its runner, requested target and capture truncation state. Runner-specific
parsers remain implementation details of the tools adapter.

```python
from opencollab.tools import has_pass_evidence

passed = has_pass_evidence(
    result.returncode,
    result.stdout + "\n" + result.stderr,
    runner="python -m pytest",
    target="tests/test_example.py::test_case",
    output_truncated=result.stdout_truncated or result.stderr_truncated,
)
```

`opencollab.patches` provides Git diff block and path parsing for workflows and
external integrations. `patch_entries` returns both old and new endpoints,
`patch_paths` retains each endpoint in first-seen order, and
`patch_block_target_path` chooses the new endpoint or a deletion's old endpoint.
The public module also supplies `split_patch_blocks`, `git_header_tokens`,
`git_diff_endpoint`, `diff_target_path`, `decode_git_c_path` and
`normalize_patch_path`. The parser retains Git's quoted UTF-8 bytes, escaped
control characters and unquoted filename spaces. Block splitting preserves LF
separators and Unicode filename characters.
