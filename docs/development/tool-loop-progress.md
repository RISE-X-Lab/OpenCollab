# Tool-loop progress measurements

Loop handling tracks an operation separately from the parameters used to dispatch
it. Native Bash keeps its command text and semantic parameters intact while its
timeout shares the operation's repeat history. Actual execution observations
identify timeout recovery and confirmed content changes. Result text continues
to reach the model unchanged.

A confirmed edit permits one validation of an operation that observed an older
workspace state. Actual timeout followed by a longer effective timeout has one
additional recovery opportunity within the existing call window. Reservations
are consumed before dispatch and survive session persistence. Window eviction
and a new user request follow the existing window lifetime.

Unresolved invocations retain ownership even after their ordinary call-window
entry expires. The bounded state refuses a new tracked operation when all safe
slots are occupied instead of dropping an unresolved owner or an active recovery
permission. Restored interrupted calls retain their explicit result and spent
permission; a stopped snapshot remains stopped.

The native-tool regressions use temporary workspaces and real subprocesses. They
exercise edits followed by validation, unchanged physical overwrites, repeated
timeout adjustments, actual timeout recovery, output noise, quoted shell
commands, and compatibility with a custom tool named Bash. Session regressions
exercise batch warning counts and recovery after a saved session is loaded.

An unknown effect leaves that invocation's progress unclassified. A batch with
confirmed repeat blocks still counts those blocks when another invocation has
unknown effects. Fresh valid evidence resets the warning count. Three subsequent
unproductive blocked batches stop the session even if each also runs a Bash
command with an unknown workspace effect and an already observed result.

```bash
uv run pytest -q tests/tools/test_loop_progress_regressions.py tests/agents/test_loop_progress_resume.py
```

Run the measurement script against explicit source checkouts on the same host.
The comparison process alternates the source order across independent runs.
Both checkouts need their repository test helpers and the runtime dependencies
available to the Python interpreter running the script.

```bash
uv run python scripts/benchmark_tool_loop.py \
  --source /path/to/updated-checkout \
  --compare /path/to/previous-checkout \
  --iterations 200 --tool-iterations 10 --repeats 3 \
  --output /tmp/tool-loop-measurements.json
```

Mock cases isolate processing with short and full call windows, including a
32-call batch. Each window is measured with a legacy text-only tool and a tool
that records execution facts when the runtime supports them. Local cases execute
Bash, read a file, and overwrite files with
identical bytes at several sizes. The largest default write is 4 MiB. The
revalidation case separately measures the additional execution and persistence
that a confirmed edit permits.

Each scenario reports median and p95 elapsed time, CPU time, dispatched calls,
blocked calls, and memory allocation. Allocation is sampled in a separate pass
with `tracemalloc`; elapsed-time samples run without that tracing. Process peak
RSS is cumulative within a child process. The revalidation case also records
store calls, save time, and snapshot plus journal bytes. Session closure is part
of that case's measured completion so its pending save is accounted for.
Dispatched calls count executed tool results. Subprocess counts separately track
environment command execution; native file operations execute without creating
a shell subprocess.
No-op write cases count bytes read by the anchored file adapter's `os.read` in a
separate instrumented pass. This exposes the existing-target comparison cost.
The instrumentation is removed before subsequent elapsed-time samples.

Evaluate ordinary processing cost independently from intended behavior changes.
A validation that previously stopped before dispatch now includes real command
time. Repeated measurements show whether an apparent cost difference exceeds
local variability. Keep the raw results with the PR's evidence attachments.

This script uses mock providers and local environments. Its model-call count is
zero. Full model-effect comparisons belong in isolated evaluation runs using the
same model, task, prompt, parameters, and existing budgets for both versions.
Record actual test results, tool executions, blocked batches, model requests,
input and output usage, and wall time. Preserve the original evaluation outputs
and save these comparison results separately.
