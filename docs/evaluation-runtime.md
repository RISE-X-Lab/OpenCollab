# Configured evaluation runtime

This development version contains the candidate-workspace and unbounded-session changes integrated into the current 0.6 main API from the evaluation runtime. Explicit `OPENCOLLAB_UNBOUNDED_LIMITS=true` carries unbounded token and step settings through public agents, workflow roles, and candidate sub-workflows. Finite settings continue to use the normal budget reservations when that switch is absent.

Structured exploration, structured correction, and final synthesis inherit the configured reasoning policy. Correction uses the caller's remaining role time. The provider adapter retains typed output-limit responses with a `length` finish reason, retries declared transient provider failures, and preserves stream/terminal consistency checks. `OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION=1` keeps external failures within one logical call while the caller still owns cancellation and the configured request timeout.

Request lifecycle traces contain start, complete, and cancellation events with the same response session and step identifiers. Started and cancelled events carry zero known tokens, while completed events retain reported usage. Existing lightweight tracers continue to work when they omit an optional flush method.

`OpenCollab.create_model_client()` constructs a caller-owned native model transport from the resolved configuration. The caller closes it after use and may pass a protocol-compatible wrapper to `OpenCollab.agent(llm=...)`. Public configuration metadata includes context size, retry count, and provider error time allowance alongside the existing non-secret fields. `OpenCollab.read_session_snapshot(path)` delegates to native snapshot and journal loading without running the saved session.

These public methods let the companion evaluator collect model and role observations through the supported facade. Candidate workspaces, request parsing, finite budgets, cancellation, and snapshot replay have regression coverage. Install this development version with the paired OCE revision described in its evaluation suite guide.

The explicit unbounded switch also applies to native Single calls through
`OpenCollab.agent()`. Numeric token and step arguments are still validated,
then the configured switch carries `None` into the actual session limits.
Provider output size, context capacity, cancellation, and cleanup remain explicit.

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
