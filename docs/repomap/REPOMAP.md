# OpenCollab Repo Map

Concise developer map. Clean Architecture: dependencies point inward
(`adapters → application → domain`). The `application` layer owns the port
interfaces; `adapters` implement them; `bootstrap` is the only place that
knows concrete types. See `repomap.puml` for the full dependency graph.

Source root: `opencollab/opencollab/`. Entry point: `python -m opencollab`
→ `adapters/cli/main.py:main`.

## Layers (inner = no I/O)

### domain/ — policy + state, no imports from app/adapters/SDKs/fs
- `session.py` — `SessionState` (carries `aid` + `pending_events` + `terminal_reason`), `SessionPhase` (FSM, incl. SCHEDULED + non-terminal **AWAITING_EVENTS** suspend state + distinct BUDGET_EXCEEDED / STEP_LIMIT_EXCEEDED resource-cap terminals; both caps are session-lifetime, not per-turn — `reset_for_user_turn` preserves `step_count`/`used_tokens`)
- `pending.py` — `PendingEventTable`/`PendingRow` (per-session table of a batch's tool_call_ids; deferred rows fill on a child/external completion, immediate rows fill inline; `RowKind`/`RowStatus`/`PendingRowError`)
- `agent.py` — `Agent` (declares its `ToolSpec`s)
- `context.py` — layered context as data: `ContextSource` (tagged by `ContextLayer`/`LoadTiming`/`ContextPosition` + `visible` audit flag + `loader_key` for deferred sources) and `ContextPlan` (assembles STARTUP sources into messages **generically by position** — `system_prompt`/`startup_user_messages`/`messages`/`deferred_sources`; never special-cases a layer, so new context = a new registered source)
- `tools.py` — `ToolSpec`, `ToolProcessingResult` (`apply_to`/`apply_hashes_to`), `LoopDetection`
- `scheduler.py` — `ProcessTable`, `SessionControlBlock` (aid/parent_aid/agent/state), `DelegationTask`, `ReviewVerdict`, `split_budget`
- `team.py` — `Topology` (directed who-may-spawn/message-whom graph; `allows(src,dst)`, `allow_all`)
- `compaction.py` — `CompactResult`
- `events.py` — `DomainEvent`, `SessionRuntimeEvent`, `SchedulerEvent` (incl. `agent_message_sent/delivered`)
- `hooks.py` — `HookSpec`, `HookOutcome`, `HOOK_EVENT_NAMES` (CC-style lifecycle vocab), pure `match_hooks` (event + tool-name glob)

### application/ — use cases + ports (orchestration, no concretes)
- `ports.py` — all Protocol interfaces: `LLMPort`, `EnvironmentPort`, `SessionStorePort`, `TracePort`, `EventPublisherPort`, `PermissionPort`, `SafetyPolicyPort`, `WorktreePoolPort`, `ToolPort`, `SessionFactoryPort` (`build_spawn_session` takes `task`/`context`), `SchedulerPort` (incl. `send_message`, `team_snapshot`, `inflight_spawn`), `HookPort`, `ShaperPort` (`shape(messages)->messages`, pure pre-LLM transform)
- `session.py` — `Session` + `SessionRuntime` facade; `apply_launch` (idempotent resume/seed)
- `session_run.py` — `SessionRunUseCase` (the run loop)
- `compaction.py` — `ContextCompactionUseCase` (the *mutating* compaction path). **Retired in the wired runtime**: `build_session_runtime` constructs it but passes `compaction_enabled=False` to `SessionRunUseCase`, so the run loop never routes to `COMPACTING`. The class + its unit tests stay intact; the **read-time** path (`AutoCompactShaper`) is now the sole active summarizer (Option B of `docs/plans/context-compaction-port.md`)
- `compaction_prompt.py` — pure, natively ported from Claude Code's compaction prompt: the **9-section** summary prompt (`get_compact_prompt`), the `<analysis>`/`<summary>` parser (`format_compact_summary`), the post-compaction `build_continuation_message` (+ `transcript_recovery_note`), and `build_summary_request` (replay segment → append prompt). OpenAI dict model; no LLM/async
- `compaction_summary.py` — `ReadTimeSummarizer`: the sync `SummarizerPort` body for `AutoCompactShaper`. Bridges the **async** `LLMPort` into sync `ShaperPort.shape` via `run_coro_blocking` (worker-thread event loop); summarizes a segment with the ported prompt, strips the scratchpad, appends a transcript pointer, falls back to a bounded raw excerpt on any failure
- `tool_execution.py` — `ToolExecutionUseCase` + `ToolRuntime` + `CallbackPermissionPolicy`. Persists the **full** tool result (no append-time truncation); model-facing bounding is the shaper's job
- `shaping.py` — `ShaperPipeline` (ordered `ShaperPort` chain, identity when empty) applying **lazy-degradation** (cheapest/lowest-loss layer first). Layers: `PerToolResultBudgetShaper` (caps any *one* tool result — head + "re-read a narrower range" reference) then the **reactive** history layers that bound the *total* view only once an estimated-token trigger is crossed — `ToolOutputClearShaper` (lowest-loss: clears the *content* of OLD compactable tool results — `bash`/`file_read`/`grep`/`git_diff`/`run_tests`, driven from the real registry — to a placeholder, keeping the call/answer skeleton; keeps last N) → `OldHistorySnipShaper` (deletes whole old low-reference tool-exchange turns down to a target, no model call) → `AutoCompactShaper` (summarizes the remaining old span into one *visible* `[Context auto-compacted …]` marker via an injected sync `SummarizerPort`; **now active** — wired to `ReadTimeSummarizer` + the 9-section prompt) → `ContextCollapseShaper` (reserved identity placeholder). Trigger/target scale to the model's real context window via `history_trigger_target` (falls back to fixed defaults when unknown). All are read-time projections over a *copy*: `state.messages`/transcript stay full (lossless resume), group-aware so no `tool_call_id` is orphaned, and the trigger/target gap is anti-thrash headroom. Applied in `SessionRunUseCase.call_llm` before each model call
- `scheduler.py` — `Scheduler`: `create_init_process` (agent 0 / aid=0) + `spawn` (children, topology-checked, records a `(parent,tool_call_id)` origin for deferred spawns; reserves a single-flight `(role,task)` key — `inflight_spawn`/`_clear_inflight` — so a re-issued identical spawn is refused while one is in flight); `_drive_agent` runs/resumes a session, `_wake` fills a parent's pending row + re-activates it under a per-aid lock; `run` loops until `_quiescent` (no tasks, no open pending tables/inbox messages, all terminal/idle); retains sessions for async queued `send_message` and `team_snapshot`; `LaunchSpec`
- `events.py` — `SessionEventFactory` (single builder for all run-loop/tool/compaction events)
- `event_bus.py` — `EventBus` (fan-out, implements `EventPublisherPort`)
- `autosave.py` — `AutoSaveSubscriber`
- `hooks.py` — `HookEventSubscriber` (EventBus sink: maps runtime/scheduler events → CC hook names, calls `HookPort`; observe-only)

### adapters/ — implement ports, talk to drivers
- `cli/main.py` — Typer CLI: default agent + eval commands; invokes `build_scheduler`; prompt `bottom_toolbar` shows the live team (`team_snapshot`)
- `tui/` — TUI, `TuiEventSink` (EventPort), `TuiPermissionPolicy` (PermissionPort); renders a live team roster panel from scheduler events
- `llm.py` — `LLMClient`, `estimate_messages_tokens` (OpenAI/Anthropic SDK)
- `env.py` — `LocalEnvironment`, `WorktreeEnvironment`
- `worktree_pool.py` — `WorktreePool` (git worktree)
- `storage.py` — `SessionStore` (structured per-agent JSON `{aid,role,model,messages}` + `save_manifest`; reads legacy JSONL too)
- `trace.py` — `Tracer`
- `safety.py` — `SandboxInterceptor`
- `hooks.py` — `ShellHookRunner` (implements `HookPort`; runs `command` actions via subprocess, JSON payload on stdin, timeout; holds a `scheduler` handle for a future `agent` executor)
- `tools/` — `Bash` (bash.py), `FileRead`/`FileWrite`/`Grep` (fs.py), `ApplyPatch` (edit.py: unified-diff / line-range edits), `RunTests` (run_tests.py: structured pass/fail summary), `GitDiff` (git_status.py: working-tree diff vs HEAD), `AskUser` (human.py), `Spawn`/`SpawnWithReview` (spawn.py), `MessageAgent`/`TeamStatus` (message.py) — scheduler tools (`spawn_*`, `message_agent`, `team_status`) drive `SchedulerPort`. Note: some files are named for their primary class' theme, not the class (`ApplyPatchTool`→`edit.py`, `GitDiffTool`→`git_status.py`)

### bootstrap/ — composition root (only layer that wires concretes)
- `container.py` — `build_runtime_context`, `build_session_runtime`, `build_session`/`snapshot_session`/`load_session`, `build_scheduler`, `build_spawn_session`, `DefaultSessionFactory`, `SpawnConfig`, `build_workspace_safety_policy`. `TOOL_REGISTRY` (name→factory) + `ContextBuilder`: `build_plan(role, task=…)` emits the ordered `ContextSource` list (identity+team→SYSTEM, task→USER_CONTEXT, project/memory/tool_meta registered as deferred); `build_agent` folds the plan's SYSTEM sources into `Agent.system_prompt`. `build_session`/`build_session_runtime` take `seed_user_messages` (startup user-context, e.g. a child's task) + a `shaper` (defaults to a per-tool-result-budget pipeline).
- `team_config.py` — `load_team_config` (YAML: roles{prompt,model,tools} + topology) / `default_team_config` (lead-only, `allow_all`); `RoleConfig`, `TeamConfig`. See `configs/team.example.yaml`.
- `config.py` — load config + env

### harness/
- `evaluator.py` — eval harness

## Key flows
- **Context assembly:** a session's startup messages come from a `ContextPlan` (built by `ContextBuilder.build_plan`), not a single concatenated prompt. Identity + team land in the system message; a spawned child's task is a TASK-layer USER_CONTEXT source seeded right after the system prompt (so the assembled startup is ≥2 messages, persisted and resumed intact). Project/memory/tool-meta layers are *registered* as deferred sources but not loaded yet. The lead's first turn is per-turn (`add_user_message` from the CLI), not a startup source.
- **Boot:** CLI → `build_scheduler` (loads team config, wires deps, hands `Scheduler` a `LaunchSpec` + `Topology`) → `Scheduler.create_init_process` builds agent 0 via `DefaultSessionFactory.create_lead_session` → `ContextBuilder.build_agent`.
- **Spawn (deferred):** `spawn_agent` is deferrable — the run loop's EXECUTING_TOOLS phase registers it as a PENDING row in the session's `pending_events`, suspends to **AWAITING_EVENTS** (a mixed batch buffers immediate results too, so the whole tool-result block stays contiguous), and the task returns. The child runs via `_drive_agent`; on completion the scheduler fills the parent's row and, once the batch is complete, re-activates the parent (`_drive_agent` resume) which drains the table → PRECHECK → next LLM call. Net effect: the parent reasons over the child's result **in the same turn**. The child's task is seeded by the factory (`build_spawn_session(task=…)` → TASK source), so `spawn` no longer calls `add_user_message`. `spawn_with_review` stays blocking. **Single-flight dedup:** `SpawnAgentTool` first calls `inflight_spawn(role,task)`; if that `(role,task)` is already running it returns a self-describing "already handled by aid=N — wait for its result" string (resolved synchronously, no pending row) instead of spawning a duplicate — tool-level enforcement of the "don't re-spawn the same task" guidance. The reservation clears when the child reaches a terminal phase, so a legitimate later re-run is not blocked.
- **Message:** `message_agent`/`team_status` tool → `SchedulerPort.send_message`/`team_snapshot`. `send_message` queues an XML teammate message, returns an acknowledgement immediately, and delivers it as a target `user` message once that session is safe to run; recipients may reply later with their own `message_agent` call.
- **Run loop:** `SessionRunUseCase` drives `LLMPort` ↔ `ToolExecutionUseCase` ↔ `ContextCompactionUseCase`; all emit events via `EventPublisherPort`. Before each model call it runs the `ShaperPort` pipeline over a *copy* of the history (bounding the model's view — e.g. per-tool-result budget — while the persisted transcript stays full).
- **Events:** every event carries an `aid`. `SessionEventFactory` builds them; `EventBus` fans out to `TuiEventSink` + `AutoSaveSubscriber`.

## Where to make changes
- New tool → `adapters/tools/`, register in `bootstrap/container.py:TOOL_REGISTRY`, then list its name in a role's `tools` in the team config.
- New port/integration → define Protocol in `application/ports.py`, implement in `adapters/`, wire in `bootstrap/container.py`.
- Team roles / per-role tools / topology → `configs/team.yaml` (schema in `bootstrap/team_config.py`); no code change needed.
- Event-driven hooks → top-level `hooks:` block in `configs/team.yaml` (schema in `bootstrap/team_config.py`); wired in `bootstrap/container.py:build_scheduler` (`enable_hooks`). New hook action types → executor registry in `adapters/hooks.py`; new lifecycle events → `HOOK_EVENT_NAMES` + mapping in `application/hooks.py`.
- Run-loop / scheduling behavior → `application/session_run.py`, `application/scheduler.py`.
- New context layer/source (project conventions, memory, lazy loaders) → register a `ContextSource` in `ContextBuilder.build_plan`; the `ContextPlan` assembly is generic, so no change to seeding logic.
- New pre-LLM message reshaping (e.g. history compaction) → add a `ShaperPort` impl in `application/shaping.py` and append it to the pipeline in `bootstrap/container.py:build_session_runtime`.
- New domain state/rules → `domain/`, keep it I/O-free.
