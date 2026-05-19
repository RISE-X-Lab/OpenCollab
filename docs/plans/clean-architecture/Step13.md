# Step13 - REM-03 + REM-04 + REM-06 Complete Ports and Composition Root

Date: 2026-05-19
Branch: `refactor/step01-bootstrap`

## Current Code Review

Step12 split event contracts and cleaned the team -> session boundary:

- `opencollab/opencollab/application/events.py` owns `SessionRuntimeEvent`
  and `TeamEvent`;
- `core.session.events.SessionEvent` is a compatibility alias;
- `team.orchestrator` emits `TeamEvent` for delegation/review and depends
  on the application session-factory port;
- TUI consumes the split event families.

Remaining concrete-construction inside `core.session.session.Session`:

- `opencollab/opencollab/core/session/session.py:53` constructs `EventBus`.
- `:57-59` constructs `SessionStore` and wires `AutoSaveSubscriber`.
- `:64` constructs `SessionState` with the system message.
- `:66-74` constructs `LLMClient` from the `Agent` config.
- `:77-104` `_build_runtime()` constructs `ToolCallProcessor`,
  `ContextCompactor`, and `SessionRunner` directly.
- `:185-195` `snapshot()` re-runs the same construction path.
- `:209-212` `load()` also goes through `cls(...)`.

CLI-side wiring still alive in `cli/main.py` and `bootstrap/`:

- `bootstrap/session_factory.py:36-72` calls `Session(...)` with concrete
  kwargs and re-routes load.
- `bootstrap/session_factory.py:76-106` constructs `Team` with concrete
  kwargs.
- `bootstrap/runtime.py:16,23,40` constructs `Tracer` directly.

Application ports today (`opencollab/opencollab/application/ports.py`):

- present: `EnvironmentPort`, `SafetyPolicyPort`, `SafetyPolicyFactory`,
  `PermissionPort`, `EventPublisherPort`, `ToolPort`,
  `SessionFactoryPort` (added in Step12).
- missing or implicit: `LLMPort`, `SessionStorePort`, `TracePort`,
  `TokenEstimatorPort`, `RepoMapPort`, `WorktreePoolPort`.

Verification baseline from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# expected: all green after Step12
```

Boundary state before Step13:

```bash
rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/application \
  opencollab/opencollab/domain
# no matches (Step12 invariant)
```

## Remaining Problem

`Session` is still both a public facade and a runtime builder. Every
caller pays for that:

- Tests that want to inject fakes have to pass nine optional kwargs and
  rely on Session's internal `_build_runtime()` ordering.
- `snapshot()` re-runs the full construction path, including the
  `LLMClient` constructor, even when the snapshot only needs to copy
  state.
- `bootstrap/session_factory.py` calls `Session(...)` with a kwargs bag
  that mirrors the constructor, but cannot intercept which collaborators
  are built or in what order.
- `load(...)` is forced to instantiate before reading messages.

Application use cases (`ToolExecutionUseCase`, `ContextCompactionUseCase`)
still take collaborators typed as `Any` because the corresponding ports
do not exist yet. That makes it impossible to write a boundary test that
proves a use case depends only on ports.

## Goal

After this step:

- The application port set is complete enough that every collaborator
  threaded into a use case has a named, narrow protocol.
- A single composition root in `bootstrap/container.py` owns construction
  of `EventBus`, `SessionState`, `LLMClient`, `SessionStore`,
  `AutoSaveSubscriber`, `ToolCallProcessor`, `ContextCompactor`, and
  `SessionRunner`.
- `Session.__init__` becomes a thin facade: it accepts a pre-built
  `SessionRuntime` bundle, or it falls back to a default factory that
  delegates to bootstrap.
- `bootstrap/session_factory.build_chat_session` and `build_team` continue
  to expose the same public signature.
- `cli/main.py` calls bootstrap entry points and no longer touches
  collaborator construction directly.

This is one architectural boundary in two coordinated patches:

```text
application owns explicit ports
bootstrap owns concrete construction; Session becomes a facade
```

REM-07 (retire `Tool.execute(env=, interceptor=, confirm_fn=)` and
`tool_runtime_from_legacy`) is intentionally **not** in this step. See
"Next After This".

## Implementation Plan

### 1. Add characterization tests first

Before changing construction, lock the current observable behavior:

- `opencollab/tests/test_session_construction.py` (new)
  - assert `Session(agent=...)` produces a `Session` with non-null
    `tool_processor`, `compactor`, `runner`, and that `event_bus.emit(...)`
    reaches an injected sink;
  - assert `Session(..., auto_save_path=path)` registers exactly one
    `AutoSaveSubscriber` (do not assume position) and writes JSONL after
    a `user_message_appended` event;
  - assert `Session.snapshot()` produces an independent session whose
    `event_bus._targets` does not include the original
    `AutoSaveSubscriber`;
  - assert `Session.load(path, agent)` returns a `Session` whose
    `messages` matches what `SessionStore.load_messages` returned.
- `opencollab/tests/test_bootstrap_chat_session.py` (extend existing
  `test_bootstrap.py` rather than duplicate where overlap exists)
  - assert `build_chat_session(ctx)` returns a working `Session` whose
    `event_bus.sink` is `ctx.event_sink`, whose `tracer` is `ctx.tracer`,
    and whose `auto_save_path` matches the ctx workspace.

Run new tests against the current code first. They must pass before any
structural change.

### 2. Extend application ports

In `opencollab/opencollab/application/ports.py`, add narrow protocols.

Keep each port as small as the current concrete usage. Do not encode
whole classes:

```python
class LLMPort(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
    ) -> Any: ...


class SessionStorePort(Protocol):
    def save(self, path: str, messages: list[dict[str, Any]]) -> None: ...
    def load_messages(
        self, path: str, system_prompt: str
    ) -> list[dict[str, Any]]: ...


class TracePort(Protocol):
    def log_step(
        self,
        *,
        step_type: str,
        payload: dict[str, Any],
        tokens: int = 0,
        latency: float = 0.0,
    ) -> None: ...


class TokenEstimatorPort(Protocol):
    def __call__(self, messages: list[dict[str, Any]]) -> int: ...


class RepoMapPort(Protocol):
    def render(self) -> str: ...


class WorktreePoolPort(Protocol):
    async def acquire(self, role: str) -> Any: ...
    async def release(self, env: Any) -> None: ...
```

`Tracer`, `LLMClient`, `SessionStore`, and `RepoMap` already satisfy
these structurally. Do not move those modules; just stop using them as
type annotations in `application/` and `domain/`.

Update existing use cases:

- `application/tool_execution.py` — narrow `tracer: Any | None` to
  `tracer: TracePort | None` where present; narrow LLM-related params to
  `LLMPort` where they are actually used.
- `application/compaction.py` — replace the `estimate_tokens: Callable`
  parameter with `TokenEstimatorPort` (callable protocol is the same
  shape; this is a rename for documentation).
- `application/tool_dispatch.py` / `tool_runtime.py` — no change in
  signatures; just verify imports remain inside `application/`.

### 3. Add a boundary test for application/domain

Add `opencollab/tests/test_application_boundaries.py` (extend if exists):

```python
import pathlib, re

FORBIDDEN = re.compile(
    r"^\s*(?:from|import)\s+opencollab\.(?:core|tools|bootstrap|cli|tui|team)\b",
    re.MULTILINE,
)

def test_application_does_not_import_outer_layers():
    root = pathlib.Path("opencollab/opencollab/application")
    offenders = [
        str(p) for p in root.rglob("*.py")
        if FORBIDDEN.search(p.read_text())
    ]
    assert offenders == [], offenders

def test_domain_does_not_import_outer_layers():
    root = pathlib.Path("opencollab/opencollab/domain")
    offenders = [
        str(p) for p in root.rglob("*.py")
        if FORBIDDEN.search(p.read_text())
    ]
    assert offenders == [], offenders
```

Path resolution: tests run from `opencollab/`. Adjust the root to
`pathlib.Path(__file__).resolve().parents[1] / "opencollab" / "application"`
to be robust to cwd.

### 4. Introduce `SessionRuntime` bundle and composition root

Add `opencollab/opencollab/bootstrap/container.py`:

```python
@dataclass(frozen=True)
class SessionRuntime:
    state: SessionState
    event_bus: EventBus
    llm: LLMPort
    store: SessionStorePort
    tool_processor: ToolCallProcessor
    compactor: ContextCompactor
    runner: SessionRunner
    auto_save_path: str | None
```

Add a builder:

```python
def build_session_runtime(
    *,
    agent: Agent,
    env: Environment,
    tracer: TracePort | None,
    max_budget_tokens: int,
    max_steps: int,
    compaction_threshold: int,
    repo_map: str | None,
    auto_save_path: str | None,
    event_sink: EventSink | None,
    permission_policy: PermissionPort | None,
    safety_policy: SafetyPolicyPort | None,
    llm: LLMPort | None = None,
    store: SessionStorePort | None = None,
) -> SessionRuntime:
    ...
```

This function owns all calls to `EventBus(...)`, `LLMClient(...)`,
`SessionStore()`, `AutoSaveSubscriber(...)`, `ToolCallProcessor(...)`,
`ContextCompactor(...)`, and `SessionRunner(...)`. Wire `AutoSaveSubscriber`
when `auto_save_path` is set. Build `SessionState` here.

### 5. Slim `Session` to a facade

Rewrite `opencollab/opencollab/core/session/session.py`:

- accept a `runtime: SessionRuntime` keyword, optional;
- if `runtime` is not passed, call
  `bootstrap.container.build_session_runtime(...)` with the existing
  kwargs (default factory). This keeps the public constructor signature
  exactly the same;
- store the runtime fields as attributes (`event_bus`, `tool_processor`,
  `compactor`, `runner`, `store`, `state`) so existing accessor and
  test surfaces are unchanged;
- delete the inline `_build_runtime()` method;
- in `snapshot()`, build a fresh runtime with the existing kwargs but
  copy state, used_tokens, and step_count over (do not include the
  current session's `AutoSaveSubscriber`);
- `load(path, agent, **kwargs)` keeps its signature.

Import direction now becomes:

```text
core.session.session -> bootstrap.container  (default factory)
bootstrap.container  -> core.session.*       (concrete construction)
```

This is the one place where the dependency rule deliberately crosses the
core <- bootstrap edge, because `Session` is the public facade. Document
it with a one-line comment at the import site:

```python
# Facade default-factory shim: bootstrap owns concrete construction.
from opencollab.bootstrap.container import build_session_runtime
```

If this circular reference between `core.session.session` and
`bootstrap.container` proves awkward, an alternative is to inject the
factory via a module-level `set_default_session_runtime_factory(...)`
called once from `bootstrap/__init__.py`. Prefer the direct import path
first; only fall back to the registration shim if the import cycle is
real.

### 6. Update bootstrap entry points

In `opencollab/opencollab/bootstrap/session_factory.py`:

- `build_chat_session` continues to call `Session(...)` and `Session.load(...)`.
  No public signature change. The construction it triggers now flows
  through `bootstrap.container` automatically.
- Where `auto_save_path` is computed (`session_factory.py:50-54`), no
  change.
- Where `Team(...)` is constructed, no change in this step — REM-05 is
  already done; `Team` already receives the session-factory port.

If bootstrap currently builds `Tracer` directly in `runtime.py`, that
stays — `TracePort` is a structural protocol that `Tracer` satisfies.

### 7. Tighten `cli/main.py`

Survey `opencollab/opencollab/cli/main.py` for any remaining direct
construction of `Session`, `Team`, `LLMClient`, `SessionStore`, or
`Tracer`. Move those calls into the appropriate bootstrap builder.

Acceptance for this step is qualitative: after the patch,
`rg -n "Session\\(|Team\\(|LLMClient\\(|SessionStore\\(" opencollab/opencollab/cli/`
should return only the imports needed to forward kwargs from CLI flags
into the bootstrap functions, and ideally nothing.

CLI argument behavior and stdout must remain unchanged.

### 8. Verify

From `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_session_construction.py \
  tests/test_application_boundaries.py -q

OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_session_characterization.py \
  tests/test_autosave_subscriber.py \
  tests/test_bootstrap.py \
  tests/test_team_decomposition.py -q

OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_tool_execution_use_case.py \
  tests/test_context_compaction_use_case.py \
  tests/test_tool_dispatch.py \
  tests/test_tool_runtime_contract.py -q

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Boundary checks from repo root:

```bash
rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/application \
  opencollab/opencollab/domain
# no matches

rg -n "Session\\(|Team\\(|LLMClient\\(|SessionStore\\(|EventBus\\(|AutoSaveSubscriber\\(|ToolCallProcessor\\(|ContextCompactor\\(|SessionRunner\\(" \
  opencollab/opencollab/cli
# only kwargs-forwarding usages, ideally none

rg -n "^\\s*(?:from|import)\\s+opencollab\\.core\\.(?:llm|session|tracer)\\b" \
  opencollab/opencollab/application
# no matches
```

Manual smoke test (golden path):

```bash
OPENAI_API_KEY=$REAL_KEY uv run opencollab chat
# verify chat starts, streams text, runs at least one tool, and writes
# the auto-save JSONL.
```

## Acceptance Criteria

- `application/ports.py` exposes `LLMPort`, `SessionStorePort`,
  `TracePort`, `TokenEstimatorPort`, `RepoMapPort`, `WorktreePoolPort`
  as narrow protocols.
- `bootstrap/container.py` exposes `SessionRuntime` and
  `build_session_runtime(...)`.
- `core.session.session.Session.__init__` no longer contains direct
  construction of `EventBus`, `LLMClient`, `SessionStore`,
  `AutoSaveSubscriber`, `ToolCallProcessor`, `ContextCompactor`, or
  `SessionRunner`; all of these are produced by
  `build_session_runtime(...)`.
- `Session.snapshot()` builds a new session without inheriting the
  original `AutoSaveSubscriber` (Step13 preserves existing behavior).
- `Session.load(...)` still returns a session with messages loaded from
  disk and the system prompt preserved.
- `bootstrap/session_factory.build_chat_session(...)` public signature
  is unchanged; CLI behavior is unchanged.
- `cli/main.py` does not construct `Session`, `Team`, `LLMClient`,
  `SessionStore`, or `EventBus` directly.
- Boundary tests in `test_application_boundaries.py` pass.
- Full test suite (`pytest tests/ -q`) is green.

## Non-Goals

- Do not retire `Tool.execute(env=, interceptor=, confirm_fn=)` or
  `tool_runtime_from_legacy` in this patch. That is Step14 (REM-07).
- Do not extract `SessionRunUseCase` (REMAIN.md forbids combining this
  with composition-root work without expanded run-loop characterization).
- Do not move `Tracer`, `LLMClient`, or `SessionStore` into the
  `application/` or `domain/` layers; they are adapters.
- Do not change message persistence format or JSONL semantics.
- Do not introduce a DI framework.
- Do not change tool schemas.
- Do not change provider API behavior.

## Risks And Mitigations

- **Hidden ordering dependency inside `Session._build_runtime()`**.
  Today `Session` constructs `event_bus`, then `state`, then `llm`,
  then `tool_processor`/`compactor`/`runner` in that exact order, and
  some collaborators capture `event_bus` by reference. Mitigation: the
  characterization test in Step 1 asserts `event_bus.emit(...)` reaches
  the injected sink and that `AutoSaveSubscriber` fires on
  `user_message_appended`; `build_session_runtime(...)` must preserve
  the same ordering and wiring.
- **`snapshot()` regression on autosave isolation**.
  `Session.snapshot()` currently filters out `AutoSaveSubscriber` from
  `event_bus._targets` by isinstance. The new builder must build a fresh
  bus that doesn't include autosave when `auto_save_path` is `None`.
  Mitigation: characterization test asserts the snapshot's event bus
  has no `AutoSaveSubscriber` instance.
- **Circular import between `core.session.session` and
  `bootstrap.container`**. Mitigation: import inside the constructor
  body (function-local import), or use the registration-shim fallback
  documented in Step 5.
- **CLI argument behavior drift**. Mitigation: keep the manual smoke
  test in the verification list and do not change argparse plumbing.

## Next After This

Step14 (REM-07) — retire legacy tool execution compatibility:

- delete `Tool.execute(params, env=, interceptor=, confirm_fn=)`
  overrides in `tools/bash.py`, `tools/fs.py`, `tools/human.py`,
  `tools/mcp.py`;
- delete `application/tool_runtime.tool_runtime_from_legacy`;
- delete the legacy-fallback branch in `application/tool_dispatch.py`;
- migrate `test_tool_runtime_contract.py` away from
  `tool_runtime_from_legacy` and from `BashTool().execute(...)`;
- migrate `test_tool_call_processor_interceptor.py`,
  `test_session_characterization.py`, `test_tool_execution_use_case.py`,
  and `test_tool_dispatch.py` to the runtime-native call shape.

This is a single mechanical patch once Step13 is in.

Step15 (REM-02) — extract `SessionRunUseCase`:

- move the run loop, cancellation handling, budget checks, step tracing,
  compaction trigger, and tool execution coordination from
  `core.session.runner.SessionRunner` into
  `opencollab.application.session_run.SessionRunUseCase`;
- depend on `LLMPort`, `EventPublisherPort`, `TracePort`,
  `TokenEstimatorPort`, plus the existing tool/compaction use cases;
- keep `SessionRunner` as a facade so `bootstrap.container` can keep
  exporting it.

Step15 is the last big move. After it, `core.session` should hold only
storage, autosave subscriber, event-type re-exports, the `Session`
facade, and the `SessionRunner` facade.

Step16 — refresh `docs/repomap/repomap-v2.puml` and re-render PDFs to
match the post-Step15 dependency graph.
