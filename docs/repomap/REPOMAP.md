# OpenCollab Repo Map

Concise developer map. Clean Architecture: dependencies point inward
(`adapters → application → domain`). The `application` layer owns the port
interfaces; `adapters` implement them; `bootstrap` is the only place that
knows concrete types. See `repomap.puml` for the full dependency graph.

Source root: `opencollab/opencollab/`. Entry point: `python -m opencollab`
→ `adapters/cli/main.py:main`.

## Layers (inner = no I/O)

### domain/ — policy + state, no imports from app/adapters/SDKs/fs
- `session.py` — `SessionState` (carries `aid` + `pending_events`), `SessionPhase` (FSM, incl. SCHEDULED + non-terminal **AWAITING_EVENTS** suspend state)
- `pending.py` — `PendingEventTable`/`PendingRow` (per-session table of a batch's tool_call_ids; deferred rows fill on a child/external completion, immediate rows fill inline; `RowKind`/`RowStatus`/`PendingRowError`)
- `agent.py` — `Agent` (declares its `ToolSpec`s)
- `tools.py` — `ToolSpec`, `ToolProcessingResult` (`apply_to`/`apply_hashes_to`), `LoopDetection`
- `scheduler.py` — `ProcessTable`, `SessionControlBlock` (aid/parent_aid/agent/state), `DelegationTask`, `ReviewVerdict`, `split_budget`
- `team.py` — `Topology` (directed who-may-spawn/message-whom graph; `allows(src,dst)`, `allow_all`)
- `compaction.py` — `CompactResult`
- `events.py` — `DomainEvent`, `SessionRuntimeEvent`, `SchedulerEvent` (incl. `agent_message_sent/delivered`)
- `hooks.py` — `HookSpec`, `HookOutcome`, `HOOK_EVENT_NAMES` (CC-style lifecycle vocab), pure `match_hooks` (event + tool-name glob)

### application/ — use cases + ports (orchestration, no concretes)
- `ports.py` — all Protocol interfaces: `LLMPort`, `EnvironmentPort`, `SessionStorePort`, `TracePort`, `EventPublisherPort`, `PermissionPort`, `SafetyPolicyPort`, `WorktreePoolPort`, `ToolPort`, `SessionFactoryPort`, `SchedulerPort` (incl. `send_message`, `team_snapshot`), `HookPort`
- `session.py` — `Session` + `SessionRuntime` facade; `apply_launch` (idempotent resume/seed)
- `session_run.py` — `SessionRunUseCase` (the run loop)
- `compaction.py` — `ContextCompactionUseCase`
- `tool_execution.py` — `ToolExecutionUseCase` + `ToolRuntime` + `CallbackPermissionPolicy`
- `scheduler.py` — `Scheduler`: `create_init_process` (agent 0 / aid=0) + `spawn` (children, topology-checked, records a `(parent,tool_call_id)` origin for deferred spawns); `_drive_agent` runs/resumes a session, `_wake` fills a parent's pending row + re-activates it under a per-aid lock; `run` loops until `_quiescent` (no tasks, no open pending tables, all terminal/idle); retains sessions for `send_message` (sync re-activate + reply; rejects an AWAITING_EVENTS target) and `team_snapshot`; `LaunchSpec`
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
- `tools/` — `Bash`, `FileRead`/`FileWrite` (fs.py), `Grep`, `AskUser` (human.py), `Spawn` (spawn.py), `MessageAgent`/`TeamStatus` (message.py) — all scheduler tools drive `SchedulerPort`

### bootstrap/ — composition root (only layer that wires concretes)
- `container.py` — `build_runtime_context`, `build_session_runtime`, `build_session`/`snapshot_session`/`load_session`, `build_scheduler`, `build_spawn_session`, `DefaultSessionFactory`, `SpawnConfig`, `build_workspace_safety_policy`. `TOOL_REGISTRY` (name→factory) + `ContextBuilder` (role name → `Agent`: topology-aware prompt + resolved tools).
- `team_config.py` — `load_team_config` (YAML: roles{prompt,model,tools} + topology) / `default_team_config` (lead-only, `allow_all`); `RoleConfig`, `TeamConfig`. See `configs/team.example.yaml`.
- `config.py` — load config + env

### harness/
- `evaluator.py` — eval harness

## Key flows
- **Boot:** CLI → `build_scheduler` (loads team config, wires deps, hands `Scheduler` a `LaunchSpec` + `Topology`) → `Scheduler.create_init_process` builds agent 0 via `DefaultSessionFactory.create_lead_session` → `ContextBuilder.build_agent`.
- **Spawn (deferred):** `spawn_agent` is deferrable — the run loop's EXECUTING_TOOLS phase registers it as a PENDING row in the session's `pending_events`, suspends to **AWAITING_EVENTS** (a mixed batch buffers immediate results too, so the whole tool-result block stays contiguous), and the task returns. The child runs via `_drive_agent`; on completion the scheduler fills the parent's row and, once the batch is complete, re-activates the parent (`_drive_agent` resume) which drains the table → PRECHECK → next LLM call. Net effect: the parent reasons over the child's result **in the same turn**. `spawn_with_review` stays blocking.
- **Message:** `message_agent`/`team_status` tool → `SchedulerPort.send_message`/`team_snapshot`. `send_message` re-activates the target's retained session (add message + `run_loop`), awaiting any in-flight run, and returns the reply inline (synchronous; rejects a target that is AWAITING_EVENTS).
- **Run loop:** `SessionRunUseCase` drives `LLMPort` ↔ `ToolExecutionUseCase` ↔ `ContextCompactionUseCase`; all emit events via `EventPublisherPort`.
- **Events:** every event carries an `aid`. `SessionEventFactory` builds them; `EventBus` fans out to `TuiEventSink` + `AutoSaveSubscriber`.

## Where to make changes
- New tool → `adapters/tools/`, register in `bootstrap/container.py:TOOL_REGISTRY`, then list its name in a role's `tools` in the team config.
- New port/integration → define Protocol in `application/ports.py`, implement in `adapters/`, wire in `bootstrap/container.py`.
- Team roles / per-role tools / topology → `configs/team.yaml` (schema in `bootstrap/team_config.py`); no code change needed.
- Event-driven hooks → top-level `hooks:` block in `configs/team.yaml` (schema in `bootstrap/team_config.py`); wired in `bootstrap/container.py:build_scheduler` (`enable_hooks`). New hook action types → executor registry in `adapters/hooks.py`; new lifecycle events → `HOOK_EVENT_NAMES` + mapping in `application/hooks.py`.
- Run-loop / scheduling behavior → `application/session_run.py`, `application/scheduler.py`.
- New domain state/rules → `domain/`, keep it I/O-free.
