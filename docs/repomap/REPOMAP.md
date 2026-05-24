# OpenCollab Repo Map

Concise developer map. Clean Architecture: dependencies point inward
(`adapters → application → domain`). The `application` layer owns the port
interfaces; `adapters` implement them; `bootstrap` is the only place that
knows concrete types. See `repomap.puml` for the full dependency graph.

Source root: `opencollab/opencollab/`. Entry point: `python -m opencollab`
→ `adapters/cli/main.py:main`.

## Layers (inner = no I/O)

### domain/ — policy + state, no imports from app/adapters/SDKs/fs
- `session.py` — `SessionState` (carries `aid`), `SessionPhase` (FSM, incl. SCHEDULED)
- `agent.py` — `Agent` (declares its `ToolSpec`s)
- `tools.py` — `ToolSpec`, `ToolProcessingResult`, `LoopDetection`
- `scheduler.py` — `ProcessTable`, `SessionControlBlock` (aid/parent_aid/agent/state), `DelegationTask`, `ReviewVerdict`, `split_budget`
- `team.py` — `Topology` (directed who-may-spawn/message-whom graph; `allows(src,dst)`, `allow_all`)
- `compaction.py` — `CompactResult`
- `events.py` — `DomainEvent`, `SessionRuntimeEvent`, `SchedulerEvent` (incl. `agent_message_sent/delivered`)

### application/ — use cases + ports (orchestration, no concretes)
- `ports.py` — all Protocol interfaces: `LLMPort`, `EnvironmentPort`, `SessionStorePort`, `TracePort`, `EventPublisherPort`, `PermissionPort`, `SafetyPolicyPort`, `WorktreePoolPort`, `ToolPort`, `SessionFactoryPort`, `SchedulerPort` (incl. `send_message`, `team_snapshot`)
- `session.py` — `Session` + `SessionRuntime` facade; `apply_launch` (idempotent resume/seed)
- `session_run.py` — `SessionRunUseCase` (the run loop)
- `compaction.py` — `ContextCompactionUseCase`
- `tool_execution.py` — `ToolExecutionUseCase` + `ToolRuntime` + `CallbackPermissionPolicy`
- `scheduler.py` — `Scheduler`: `create_init_process` (agent 0 / aid=0) + `spawn` (children, topology-checked), parallel via `asyncio.create_task`; retains sessions for `send_message` (sync re-activate + reply) and `team_snapshot`; `LaunchSpec`
- `events.py` — `SessionEventFactory` (single builder for all run-loop/tool/compaction events)
- `event_bus.py` — `EventBus` (fan-out, implements `EventPublisherPort`)
- `autosave.py` — `AutoSaveSubscriber`

### adapters/ — implement ports, talk to drivers
- `cli/main.py` — Typer CLI: default agent + eval commands; invokes `build_scheduler`; prompt `bottom_toolbar` shows the live team (`team_snapshot`)
- `tui/` — TUI, `TuiEventSink` (EventPort), `TuiPermissionPolicy` (PermissionPort); renders a live team roster panel from scheduler events
- `llm.py` — `LLMClient`, `estimate_messages_tokens` (OpenAI/Anthropic SDK)
- `env.py` — `LocalEnvironment`, `WorktreeEnvironment`
- `worktree_pool.py` — `WorktreePool` (git worktree)
- `storage.py` — `SessionStore` (JSONL)
- `trace.py` — `Tracer`
- `safety.py` — `SandboxInterceptor`
- `tools/` — `Bash`, `FileRead`/`FileWrite` (fs.py), `Grep`, `AskUser` (human.py), `Spawn` (spawn.py), `MessageAgent`/`TeamStatus` (message.py) — all scheduler tools drive `SchedulerPort`

### bootstrap/ — composition root (only layer that wires concretes)
- `container.py` — `build_runtime_context`, `build_session_runtime`, `build_session`/`snapshot_session`/`load_session`, `build_scheduler`, `build_spawn_session`, `DefaultSessionFactory`, `SpawnConfig`, `build_workspace_safety_policy`. `TOOL_REGISTRY` (name→factory) + `ContextBuilder` (role name → `Agent`: topology-aware prompt + resolved tools).
- `team_config.py` — `load_team_config` (YAML: roles{prompt,model,tools} + topology) / `default_team_config` (lead-only, `allow_all`); `RoleConfig`, `TeamConfig`. See `configs/team.example.yaml`.
- `config.py` — load config + env

### harness/
- `evaluator.py` — eval harness

## Key flows
- **Boot:** CLI → `build_scheduler` (loads team config, wires deps, hands `Scheduler` a `LaunchSpec` + `Topology`) → `Scheduler.create_init_process` builds agent 0 via `DefaultSessionFactory.create_lead_session` → `ContextBuilder.build_agent`.
- **Spawn:** Spawn tool → `SchedulerPort.spawn` (topology-checked) → new `Session` via `SessionFactoryPort.build_spawn_session`, tracked in `ProcessTable` and retained for messaging.
- **Message:** `message_agent`/`team_status` tool → `SchedulerPort.send_message`/`team_snapshot`. `send_message` re-activates the target's retained session (add message + `run_loop`), awaiting any in-flight run, and returns the reply inline.
- **Run loop:** `SessionRunUseCase` drives `LLMPort` ↔ `ToolExecutionUseCase` ↔ `ContextCompactionUseCase`; all emit events via `EventPublisherPort`.
- **Events:** every event carries an `aid`. `SessionEventFactory` builds them; `EventBus` fans out to `TuiEventSink` + `AutoSaveSubscriber`.

## Where to make changes
- New tool → `adapters/tools/`, register in `bootstrap/container.py:TOOL_REGISTRY`, then list its name in a role's `tools` in the team config.
- New port/integration → define Protocol in `application/ports.py`, implement in `adapters/`, wire in `bootstrap/container.py`.
- Team roles / per-role tools / topology → `configs/team.yaml` (schema in `bootstrap/team_config.py`); no code change needed.
- Run-loop / scheduling behavior → `application/session_run.py`, `application/scheduler.py`.
- New domain state/rules → `domain/`, keep it I/O-free.
