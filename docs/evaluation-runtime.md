# Configured evaluation runtime

This development version contains the candidate-workspace and unbounded-session changes integrated into the current 0.6 main API from the evaluation runtime. Explicit `OPENCOLLAB_UNBOUNDED_LIMITS=true` carries unbounded token and step settings through public agents, workflow roles, and candidate sub-workflows. Finite settings continue to use the normal budget reservations when that switch is absent.

Structured exploration, structured correction, and final synthesis inherit the configured reasoning policy. Correction uses the caller's remaining role time. The provider adapter retains typed output-limit responses with a `length` finish reason, retries declared transient provider failures, and preserves stream/terminal consistency checks. `OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION=1` keeps external failures within one logical call while the caller still owns cancellation and the configured request timeout.

Request lifecycle traces contain start, complete, and cancellation events with the same response session and step identifiers. Started and cancelled events carry zero known tokens, while completed events retain reported usage. Existing lightweight tracers continue to work when they omit an optional flush method.

`OpenCollab.create_model_client()` constructs a caller-owned native model transport from the resolved configuration. The caller closes it after use and may pass a protocol-compatible wrapper to `OpenCollab.agent(llm=...)`. Public configuration metadata includes context size, retry count, and provider error time allowance alongside the existing non-secret fields. `OpenCollab.read_session_snapshot(path)` delegates to native snapshot and journal loading without running the saved session.

These public methods let the companion evaluator collect model and role observations through the supported facade. Candidate workspaces, request parsing, finite budgets, cancellation, and snapshot replay have regression coverage. Install this development version with the paired OCE revision described in its evaluation suite guide.
