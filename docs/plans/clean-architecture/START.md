# Clean Architecture Phase - START

Date: 2026-05-18
Branch: `refactor/step01-bootstrap`

This note starts the next refactor phase. The previous session-decoupling phase
reduced construction and UI coupling, but the codebase is not yet clean by Clean
Architecture dependency rules.

## Baseline

Current verification baseline:

```bash
cd opencollab
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 57 passed
```

Relevant architecture artifacts:

- `docs/repomap/module_map.puml`: original dependency map before the latest decoupling.
- `docs/repomap/repomap-v2.puml`: current dependency map after the decoupling steps.
- `docs/repomap/repomap-target.puml`: target Clean Architecture dependency map.

## What The Previous Phase Achieved

The branch has materially improved maintainability:

- `cli.main` no longer builds agents, tools, sessions, teams, envs, or tracers directly.
- `bootstrap/` now holds most chat/team composition wiring.
- `Session` is closer to a public facade over `SessionState` and runtime collaborators.
- `SessionRunner` owns the run loop and phase transitions.
- `TUI._active_instance` was removed; TUI interaction now flows through `TuiEventSink` and `TuiPermissionPolicy`.
- Tool instances are less stateful; `ToolCallProcessor` owns runtime execution context.
- `Team` was split into `orchestrator`, `teammate_factory`, and `worktree_pool`.
- Autosave moved from runner/compactor constructor plumbing to an `AutoSaveSubscriber` on the event bus.

## Current Distance From Target

Pragmatic status:

- Maintainability decoupling: roughly 60-70% there.
- Clean Architecture dependency rule: roughly 40-50% there.

The code is cleaner, but core dependency direction is still not clean.

## Main Remaining Coupling

1. No real domain/application split yet.

   `core.session` still mixes state, use-case orchestration, ports, adapters,
   and runtime wiring. `Session` still constructs concrete collaborators such
   as `LLMClient`, `EventBus`, `AutoSaveSubscriber`, `SessionStore`,
   `SessionRunner`, `ToolCallProcessor`, and `ContextCompactor`.

2. Core depends on concrete outer policy.

   `core.session.tools` imports `SandboxInterceptor` from `opencollab.tools.safety`.
   In the target architecture, the application layer should own a
   `SafetyPolicyPort`, and the sandbox implementation should live outside the
   core/application policy.

3. Tool boundary is still leaky.

   `Tool.execute()` currently exposes concrete runtime plumbing:
   `env`, `interceptor`, and `confirm_fn`. In the target architecture, tools
   should implement a `ToolPort` and receive application-owned ports such as
   `EnvironmentPort`, `PermissionPort`, and `SafetyPolicyPort`.

4. Events are still overloaded.

   Team delegation progress still uses `SessionEvent`-style `tool_start` and
   `tool_end` events. The target should split session runtime events, team
   orchestration events, and UI view events.

5. Composition root exists, but is incomplete.

   `bootstrap/` is better than before, but object construction still happens
   inside `Session` and parts of team/runtime code. The target is a real
   composition root that wires use cases to adapters while keeping business
   rules out of bootstrap.

6. CLI still owns some policy-adjacent wiring.

   `cli.main` still resolves config and calls the evaluator path directly.
   This is acceptable short-term, but target architecture would put this behind
   composition/adapters.

## Target Rules

Dependency direction:

```text
drivers/frameworks -> adapters -> application/use cases -> domain
```

Rules for this phase:

- Domain must not import application, adapters, tools, UI, SDKs, filesystem, Docker, or shell.
- Application owns ports.
- Adapters implement ports.
- Bootstrap/container is the only layer allowed to know both concrete adapters and application use cases.
- `Session` remains the public facade, but should become a thin application facade rather than a concrete runtime builder.
- Preserve CLI/TUI behavior unless explicitly scoped otherwise.
- Preserve messages-only JSONL storage semantics unless explicitly scoped otherwise.
- Keep refactors small and reviewable.
- Add characterization tests before moving behavior across boundaries.

## Proposed Refactor Sequence

### Step CA-01 - Introduce Application Ports

Add an application-owned ports module without moving behavior yet:

- `LLMPort`
- `EnvironmentPort`
- `SessionStorePort`
- `TracePort`
- `EventPublisherPort`
- `PermissionPort`
- `SafetyPolicyPort`
- `RepoMapPort`
- `WorktreePoolPort`
- `ToolPort`

Acceptance:

- Ports are structural protocols or small ABCs.
- Existing behavior unchanged.
- Tests still pass.

### Step CA-02 - Cut `core.session.tools -> tools.safety`

Replace the direct `SandboxInterceptor` dependency in `ToolCallProcessor` with
`SafetyPolicyPort`.

Acceptance:

- `core.session.tools` no longer imports `opencollab.tools.safety`.
- `SandboxInterceptor` is adapted outside core/session.
- Existing tool safety behavior remains characterized and green.

### Step CA-03 - Normalize Tool Runtime Ports

Move from `Tool.execute(params, env, interceptor, confirm_fn)` toward a cleaner
tool runtime contract.

Target shape:

- Tool implementations depend on `EnvironmentPort`, `PermissionPort`, and
  `SafetyPolicyPort`, not concrete terminal or sandbox classes.
- `ToolCallProcessor` coordinates tool execution through `ToolPort`.

Acceptance:

- Tool schemas unchanged unless explicitly required.
- CLI/TUI behavior unchanged.
- Tool tests characterize bash/fs/human behavior before edits.

### Step CA-04 - Extract Domain State Types

Move pure state/policy types toward a domain layer:

- `SessionState`
- `SessionPhase`
- message/value objects where useful
- tool call/result value objects
- team task/review verdict value objects

Acceptance:

- Domain layer has no imports from adapters, SDKs, CLI/TUI, storage, shell, or Docker.
- `Session` facade compatibility is preserved.

### Step CA-05 - Move Use Cases Into Application Layer

Separate application orchestration from concrete runtime construction:

- session run loop/use cases
- context compaction use case
- tool execution use case
- team delegation/review use cases

Acceptance:

- Use cases depend on domain + ports only.
- Concrete adapters are wired from bootstrap/container.

### Step CA-06 - Split Event Contracts

Separate:

- `SessionEvent`: session runtime lifecycle.
- `TeamEvent`: orchestration/delegation lifecycle.
- UI view events: adapter-derived display events.

Acceptance:

- TUI consumes adapter output, not mixed internal event semantics.
- Team no longer overloads `SessionEvent` tool names for delegation progress.

### Step CA-07 - Complete Composition Root

Make bootstrap/container the single concrete wiring layer.

Acceptance:

- Application/domain modules do not construct concrete SDK/env/storage/UI classes.
- `Session` remains public facade but delegates construction through injected collaborators or a factory.
- Full test suite remains green.

## First Priority

Start with **CA-02: cut `core.session.tools -> tools.safety`**.

Reason: it is the clearest Clean Architecture violation and should be possible
to fix with a small compatibility-preserving patch:

1. characterize current safety/interceptor behavior;
2. add `SafetyPolicyPort`;
3. adapt `SandboxInterceptor` behind that port;
4. remove the concrete import from `core.session.tools`;
5. verify full test suite.
