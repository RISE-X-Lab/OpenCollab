# Duo

[Chinese guide](duo/README.zh-CN.md)

Duo V8 produces two isolated solutions to a task, compares their evidence, and
adopts one result. Its task-oriented role prompts cover source changes,
configuration, data and other required artifacts. The runtime supplies the
candidate environments and delivery mechanism.

## Run Duo

Configure a model endpoint using [the configuration guide](../configs/README.md)
and choose the workspace to work in. Duo is installed with OpenCollab and is
available without a caller-defined `workflows/` directory.

```bash
opencollab workflow list --workspace /path/to/workspace
opencollab workflow run duo --workspace /path/to/workspace \
  --task 'Complete the task described here.' \
  --args '{"allow_unisolated_shell":true}'
```

`--task` sets the workflow's `goal` argument. For longer tasks, use
`--task-file task.md` to read a UTF-8 file. Keep other workflow arguments
in `--args`. These shortcuts are mutually exclusive with each other and with
an explicit `goal` in `--args`. Existing JSON-only invocations remain supported.

The example explicitly permits shell execution in a trusted local workspace.
The default shell setting requires process isolation. Container-backed callers
can retain that default and supply their environment through the public SDK.

```python
import asyncio

from opencollab import OpenCollab


async def main():
    client = OpenCollab("/path/to/workspace")
    result = await client.workflow(
        "duo",
        {
            "goal": "Produce the requested data files and update their configuration.",
            "candidate_evidence_dir": "artifacts/duo-evidence",
            "allow_unisolated_shell": True,
        },
        budget=1_000_000,
        artifacts="artifacts/duo-run",
    )
    print(result.raise_for_status().output)

asyncio.run(main())
```

The public function is `from opencollab.builtin_workflows import duo`. Pass it
instead of the name to use an explicit callable. `get_builtin_workflows()`
returns a fresh registry with one workflow, `duo`. CLI and SDK name lookup
combine installed workflows with the workspace's `workflows/` directory or
`OPENCOLLAB_WORKFLOWS_DIR`. Duplicate names retain the ordinary registry error.

An explicit `agent_profile` selects the base agent implementation and its system
instructions, shaping, tool limits, and safety behavior. For example,
`agent_profile="base"` follows the Base mapping, currently Single2.
`agent_profile="single2"` selects that implementation directly.
Workflow-role instructions take precedence over its general repair and
submission guidance through the existing role-permission block. Omitting
`agent_profile` retains the workflow's role-defined configuration.

## Task-oriented roles

The shared instructions ask each role to follow the task and runtime delivery
requirements, preserve unrelated user work, and retain required artifacts and
services. They permit configuration, dependency, build, resource and public-check
updates when those changes belong to the task. They protect independent
validation and withheld reference answers. Commits and other submission
mechanisms follow the task or runtime's requirements.

V8 focuses verification on the final deliverable through the task's intended
entry points or outputs, including existing behavior directly affected by the
changes. Coders rerun affected focused checks after the last relevant edit or
cleanup and distinguish unchanged project checks from candidate-added or modified
checks. These shared instructions cover both submission modes and apply to code,
files, and service state. The task and runtime continue to define delivery.

Candidate A pursues the simplest complete solution. Candidate B checks the
outcome end to end, including dependencies, interactions and boundary cases.
B receives the public verification command observed from A and is asked to run
it when relevant and available. Each role reports its actual work, executed
checks and remaining limitations.

Duo runs A before B, then performs mechanical selection, any required
adjudication, and adoption. A finite workflow `budget` is shared across
its roles. Each role uses the remaining allowance unless a caller supplies a
separate per-call cap in a custom workflow.

The SDK's `concurrency` and the CLI's `--concurrency` limit active agent sessions.
`task_concurrency` and `--task-concurrency` separately limit `parallel` and
`pipeline` units, including candidate child workflows, and default to the
agent-session limit. Increasing these limits leaves Duo's A-to-B sequence intact.

The adjudicator first inspects each candidate separately for concrete failure
paths against the public task requirements. It traces a relevant input or state
and trigger through the implementation to the expected output. Coverage depends
on the behavior from the entry point or producer through its consumers to the
result. A function's presence or a matching symbol alone leaves that path
unresolved. Each requirement's `a_evidence` and `b_evidence` entries name the
original changed paths and explain the inspected behavior. Model-written reports
are claims to assess. Test records are comparable when their target, runner and
command agree and their execution remains applicable to the final candidate.

Recognized test execution through Bash creates a record with `applicability="current"`.
A later completed `file_write` or `apply_patch` operation may change this status.
When such an operation produces an observed content change, it marks earlier records
as `applicability="unknown"` and appends the changed path to `post_test_edits` in
completion order. The original exit code and parser-backed `verified` result remain
in the history. A new test execution creates a current record alongside the earlier
records. Failed edits and writes with unchanged contents preserve existing applicability.
Custom `CandidateRun` providers whose records omit these optional fields retain their
existing comparison behavior.

A later Bash command outside the recognized executable test form also makes
earlier applicability unknown and is retained in `post_test_commands`. Shell
commands can change files even when they fail or are interrupted. The observer
leaves those effects uncertain rather than inferring unchanged inputs from a
command name or exit status. Native read-only tools keep existing applicability,
and executing the test again provides a new current result.

The mechanical public-test comparison uses current records. Earlier records
with subsequent edits remain available to the adjudicator through individual
and shared evidence (either inline or paged). The adjudicator assesses the
final candidate and the recorded edits or shell commands against the task requirements. This also
applies to an observed notes-file write, whose effect on test inputs remains
uncertain from the write facts alone.

Correctness and compatibility determine the comparison. Architectural ambition,
longer reports, more changes and more files confer no preference. A covered
requirement has an evidence advantage over either `not_covered` or `unclear`
when its own evidence cites the candidate's changed paths and explains the
behavior they establish. This comparison applies symmetrically to A and B.
An explicit `not_covered` requirement that the other candidate covers rejects
the recommendation. A demonstrated advantage for A can select A. Equivalent
evidence defaults to B, including a complete, valid requirement table with
no supported advantage.

An incomplete, contradictory or insufficiently anchored adjudication receives
at most one additional adjudication session focused on the unresolved evidence.
A session with paged evidence may use several file-tool reads. If the review still leaves the
adjudication unusable, selection defaults to B. A provider exception goes
directly to the same fallback. When the inspected evidence explicitly
establishes a missing requirement in B that A covers, selection retains A,
including when an additional session fails after that finding. The existing
empty-candidate handling and comparable public-test failure preference precede
adjudication. Identical diffs select B.

The current prompt text is kept together in
[`_prompts.py`](../opencollab/builtin_workflows/_prompts.py). Its internal revision
is 8 and is recorded as `prompt_revision` in the result. Callers use `duo`
without a prompt-version suffix.

`requirements_complete` records whether the adjudicator has accounted for every
explicit requirement. Candidate correctness is recorded by the coverage entries
and supporting evidence. Shared gaps remain visible as `not_covered` or `unclear` entries
while the adjudicator compares concrete differences between the candidates.

Set `submission_mode="working_tree"` when the caller captures the chosen diff
and owns subsequent commits or submission. Both coders leave their intended
changes available for capture, and the adjudicator treats the absent candidate
commit as part of that delegated delivery process. This mode does not establish
test success. The default `submission_mode="task"` follows the task-specific
delivery instructions. The result records the mode used for that workflow call.

## Complete evidence for selection

When mechanical selection leaves a choice open, Duo saves the complete candidate
evidence. When the complete comparison payload fits within 128,000 UTF-8 bytes,
the read-only adjudicator receives both full diffs, each candidate's full public
test records, the shared comparable records and both candidate reports inline.
The reports are marked as model-supplied claims. The adjudicator compares this
complete package directly through the structured-output interface.

Larger comparison payloads give the adjudicator `read_candidate_evidence` for
complete paged reads. The original evidence files are retained in both modes.
Indexes describe original changed paths and character ranges. Text and binary
diffs remain complete. Public test evidence and model-supplied result reports
are separate files. The tool returns `next_offset` and `eof` for continued reads
and exposes only files registered for the current adjudication.

`candidate_evidence_dir` selects a host-side parent directory. Every adjudication
creates an independent child and records its location in workflow logs. The
caller owns retention of these files. Evidence can be read even when the candidate
executes in another environment.

## Candidate delivery and results

Duo uses the existing candidate workspace port for isolation, change capture and
adoption. The default Git backend delivers repository changes. A full-environment
integration supplies its own candidate backend and adopts the selected candidate
by identity. Required files and live services are retained by that backend.
Candidate validity and adoption are enforced by the runtime. Empty changes remain
an incomplete result under the existing patch-based selection rules.

Git candidates start with the source's current tracked files and untracked files
that aren't ignored. Their delivered patches describe the subsequent candidate edits.
Adoption applies that increment to the current source files and preserves independent
user edits and the source index. A conflicting patch leaves the current source in place.
Changes to ignore rules preserve files copied from the source and files added to
the candidate index or commits. Newly created ignored files join delivery when
explicitly added with `git add --force`.
If the source changes while a candidate is running, the runtime reports the
change and retains the candidate worktree. The error includes its original Git
tree, which remains referenced by `refs/worktree/opencollab-source` in the
retained worktree, for recovery with commands such as `git show <tree>:<path>`.
The runtime also records the source `HEAD` in `CandidateRun.source_revision`
when the backend exposes it and checks that revision again before adoption.
A changed revision rejects adoption even when the source diff is unchanged.

Local Git candidates initialize clean submodules already available in the
source, including nested dependencies within the selected workspace.
Uninitialized optional submodules remain empty. Source submodule changes are
reported before acquisition. A candidate that changes submodule contents,
commits, or gitlinks raises `CandidateCaptureError` and retains the complete
worktree for recovery. The repository patch backend delivers changes in the
parent repository and requires a different delivery backend for submodule edits.

`goal` supplies the task and `description` is an accepted alternative.
`injected_test_paths` lets an evaluation integration preserve its protected test
files during adoption. Budgets, model settings and role deadlines follow the
caller's runtime configuration.

Duo's output records `winner` and `adopted` separately, because a failed adoption
may fall back to the other candidate. `status` is `done` after successful adoption,
`incomplete` when no candidate is adopted, or `error` when the task is missing.
The output also retains selection reasons, candidate evidence, adoption attempts
and token consumption. Duo addresses candidate selection through behavior evidence
and the default-B preference. Task correctness is established by the caller's actual
checks and, when applicable, the evaluator's formal scoring.

OpenCollab-Eval owns benchmark inputs, environment preparation, hidden-test
isolation, candidate capture and official grading. See its
[evaluation quick start](https://github.com/RISE-X-Lab/OpenCollab-Eval#duo-quick-start).
