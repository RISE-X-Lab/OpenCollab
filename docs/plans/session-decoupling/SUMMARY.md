# Session Decoupling Plan Summary

This directory collects the seven-step refactor sequence for reducing coupling
around CLI composition, session runtime ownership, TUI integration, tool
execution, team orchestration, and autosave.

## Sequence

| Step | Plan | Purpose | Result |
|---|---|---|---|
| 01 | [Step01.md](Step01.md) | Extract `bootstrap/` composition layer from `cli/main.py`. | CLI delegates chat/team construction to bootstrap factories. |
| 02 | [Step02.md](Step02.md) | Collapse duplicate `Session` and `Team` init channels. | Public init surface uses `event_sink`, `permission_policy`, and `llm` instead of parallel aliases. |
| 03 | [Step03.md](Step03.md) | Remove private `Session` compatibility shims. | `SessionRunner` owns loop mechanics; `Session` stays a public facade. |
| 04 | [Step04.md](Step04.md) | Replace `TUI._active_instance` singleton with explicit injection. | TUI rendering and permission prompting are wired through adapter objects. |
| 05 | [Step05.md](Step05.md) | Move sandbox/interceptor ownership into `ToolCallProcessor`. | Tools are less stateful; runtime derives safety policy from the environment. |
| 06 | [Step06.md](Step06.md) | Split `Team` into orchestrator, teammate factory, and worktree pool. | Team construction, budget splitting, and worktree lifecycle are separated. |
| 07 | [Step07.md](Step07.md) | Move autosave from runner/compactor callback plumbing to event subscription. | `AutoSaveSubscriber` listens on the `EventBus`; runner/compactor constructors no longer receive `auto_save`. |

## Current State

The refactor substantially reduces construction and UI coupling, but it is not
yet a full clean-architecture split.

What improved:

- `cli.main` is mostly command parsing, REPL control, and TUI lifecycle.
- Bootstrap is the main composition entry point for chat/team runtime wiring.
- `Session` is a facade over explicit runtime collaborators.
- `SessionState` is the lifecycle state holder.
- `SessionRunner` owns loop transitions.
- `ToolCallProcessor` owns tool execution, loop detection, permission policy,
  and sandbox runtime context.
- `Team` is no longer a single monolithic class for orchestration, teammate
  construction, budget splitting, and worktree lifecycle.
- Autosave is an event subscriber rather than a cross-cutting constructor
  callback.

Remaining coupling:

- `Session` still builds several concrete collaborators internally.
- `core.session.tools` still imports concrete `tools.safety.SandboxInterceptor`.
- Tool execution signatures still expose concrete runtime concepts such as
  `interceptor` and `confirm_fn`.
- `Team` still emits delegation progress using `SessionEvent`-style tool events.
- TUI still depends on string event names and payload shapes.
- CLI still owns some config resolution and directly enters the eval path.

## Target Direction

The next architectural target is documented in:

- `../../repomap/repomap-target.puml`
- `../../repomap/repomap-target.pdf`

The target shape is:

- Domain owns pure state and policy.
- Application owns use cases and ports.
- Adapters implement application-owned ports.
- Bootstrap is the only layer that imports concrete implementations across the
  full stack.
