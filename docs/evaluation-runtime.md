# Configured evaluation runtime

The public SDK exposes the runtime controls used by evaluation integrations
through `OpenCollab.agent()`, `team()`, and `workflow()`. Resolve model and
transport settings when constructing `OpenCollab(...)`, then select run budgets,
deadlines, profiles, and artifact paths on each call. See
[configuration](../configs/README.md) for environment variables and
[the package guide](../opencollab/README.md) for SDK and workflow examples.

Explicit `OPENCOLLAB_UNBOUNDED_LIMITS=true` carries unbounded token and step
settings through standalone agents, workflow roles, and candidate sub-workflows.
The default standalone agent uses Base, currently mapped to Single2. With the
switch enabled, omitted token and step limits carry `None` into that session.
Explicit positive `budget`, `max_steps`, or `steps` values retain their limits
for Single2. A public `workflow()` call with the switch enabled sets its token
and role-step limits to `None`, including when `budget` or `max_steps` is passed.
SDK `team()` continues to use its shared or declared per-role token allowances
and its `max_steps` setting. Finite settings use normal budget reservations.

Structured exploration, structured correction, and final synthesis inherit the
configured reasoning policy. Correction uses the caller's remaining role time.
Provider adapters retain typed output-limit responses, including Chat
Completions `length` and Responses `max_tokens` finish reasons, with their
available content and usage. They retry classified transient failures and
check streamed output against the terminal response. On the Responses path,
`OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION=1` keeps retryable failures within one
logical call and removes its retry-count and provider-error-time allowance.
The caller retains cancellation and the configured request deadline.

SDK `timeout=` bounds a complete run, and workflow-role `timeout=` bounds that
role. Model request, connection, first-event, and stream-idle timeouts are
separate transport controls. `cleanup_timeout=` bounds owned shutdown after
execution ends. The SDK configuration validates transport timeouts as finite
positive seconds. Lower-level native Responses adapter calls also accept
`None` for stream waits, retaining transport timeout causes in that mode.
Unbounded token and step limits preserve the configured transport and shutdown
settings.

Request lifecycle traces record `llm_call_started`, `llm_call`, and
`llm_call_cancelled` with the same response session and step identifiers.
Started and cancelled records carry zero known tokens. Completed records retain
reported usage. Late provider success or error usage is charged once during
owned draining, including after caller cancellation. Cancelled calls therefore
need their final persisted usage and cleanup outcome when interpreting cost.
Caller cancellation propagates as `asyncio.CancelledError` after owned shutdown.
A run deadline produces a stopped result with `reason="timeout"`.

Run token totals add input and output. Cache read and cache creation counts are
already included in input tokens, and reasoning counts follow the provider's
output accounting. Provider usage records preserve raw counters and identify
estimated usage. `OPENCOLLAB_API_USAGE_LOG` selects an optional append-only
provider ledger, while enabled trajectories retain per-call usage. Existing
lightweight tracers continue to work when they omit an optional flush method.

`OpenCollab.create_model_client()` constructs a caller-owned native model
transport from the resolved configuration. Close it with `await model.close()`
after use. A protocol-compatible wrapper can be passed to
`OpenCollab.agent(llm=...)`. `OpenCollab.configuration` includes protocol,
context size, retry count, and provider error time allowance alongside the
other non-secret fields. `OpenCollab.read_session_snapshot(path)` reads the
native snapshot and completed journal entries through the storage adapter.

Every public SDK run generates a `run_id` or accepts one from the caller.
The result's `metrics["run_id"]` joins the agent snapshot, team manifest, or
workflow manifest and any recorded trajectory. Saved workflow manifests retain
the same identity with tracing disabled and on failed outcomes. Artifact
directories must be new or empty. A caller-supplied environment remains owned
by the caller, whose cleanup determines environment quiescence. The result's
`session_quiesced` separately describes session and persistence completion.

Failed Team children deliver available worktree patches with their failure
results. Prebuilt teammates receive the same partial work in their stop notices.
Their ERROR or STOPPED status and original failure reason remain available.
When capture fails, or a bounded result or notice cannot carry the complete
partial patch, native worktrees retain the full files and report a recovery
location. The SDK reports the retained cleanup state. Export the full changes
from that location before releasing the worktree. A later successful
`get_diff()` clears the native retention state and permits cleanup.

A revoked execution environment stops its current session before another model
call. Workflow calls also check the shared environment before acquiring work and
after waiting for an agent slot, so queued roles terminate with the revocation
error while existing transcript, usage, and candidate recovery records survive.
An isolated candidate's environment remains distinct from its parent environment.

Rejected, oversized malformed tool arguments are shortened in the model's
read-time view, with both chat tool calls and Responses items kept paired. The
original transcript retains the rejected arguments. Successful and pending tool
calls remain exact. Small context windows retain the two most recent evidence
groups while using the existing pressure-triggered history processing.

If a role already requires a write or structured submission and the model returns
only prose, the session retries that requirement once with the same tool set. A
second prose-only response stops the session instead of declaring the requirement
complete. Both responses remain in its usage and transcript.

Responses errors with `string_above_max_length` trigger context compaction when
the provider identifies input content, tool output, or history arguments.
Errors for instruction, metadata, tool description, and name fields retain their
ordinary request-error classification. Structured roles whose input still
exceeds the provider limit after compaction finish without issuing the same
oversized corrective request.

A caller may set `OPENCOLLAB_REQUIRE_INSTRUCTIONS_ECHO=1` for endpoints that echo
submitted Responses instructions. Each completed response is compared with the
submitted instruction text before its tool calls are delivered. Requests with
no system instructions explicitly send an empty string in this mode. Setting
`OPENCOLLAB_INSTRUCTIONS_AUDIT_DIR` stores each response's requested and returned
instructions, model controls, and usage under its response identifier. This
opt-in check detects a gateway replacing the submitted role instructions during
a remote model call. Repository revisions, database identifiers, transactions,
uniqueness, type checks, and local request tests cannot observe that remote
response rewrite. The check remains at the provider delivery boundary.

Workflows can supply `candidate_workspace` to `OpenCollab.workflow()`. The
backend creates isolated candidate leases and reports source evidence through
`source_diff(exclude_paths)`. Source change detection uses this same evidence,
including for environments outside Git repositories. A backend with `adopt_run`
receives the complete selected `CandidateRun`, so equal file patches can still
identify different candidate environments. Existing patch backends keep their
`adopt(diff, preserve_paths)` path. Candidate execution continues to compare the
source before and after each lease, restore unintended source edits, and retain
the workflow's executable verification tools.

`git_diff` can display observed filesystem changes when a non-Git environment
provides `get_filesystem_diff(path, stat_only)`. The displayed evidence is limited
to the requested path and the tool's normal output allowance.

Unbounded workflow roles retain the caller's configured request, first-event,
stream-idle, and connection timeouts. Caller cancellation, provider retry settings,
and owned cleanup remain configured. Terminal traces record null token and step
limits without losing the final usage or stop reason.
