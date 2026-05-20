# Step13 - Promote the `Session` facade to `application/session.py`

Date: 2026-05-20
Branch: `refactor/step13-app-session` off the Step 12 branch.

> Part 2 of the **core/session dissolution arc** (Steps 12-14), and the hard
> fork the arc was named for. Step 12 moved the three runtime *wrappers*
> into `application/`; the only thing still living in `core/session/` with
> real logic is the `Session` facade. It cannot move as-is: it imports
> `adapters.env`, `adapters.trace`, and (lazily) `bootstrap.container` — all
> three forbidden to `application/` by `test_application_boundaries.py`.
>
> Per the chosen approach (**bootstrap convenience + core shim**), this step
> splits the facade in two: a **pure** `Session` in `application/` that
> requires an injected runtime, and a self-wiring `Session` *subclass* in
> `bootstrap/` that the REPL, the team factories, the harness, and the ~30
> characterization assertions keep using through the unchanged
> `from opencollab.core.session import Session` path. **Zero test edits.**

## Goal

1. Move `SessionRuntime` (the runtime-collaborator bundle) from
   `bootstrap/container.py` into `application/session.py`. After Step 12 all
   of its field types (`SessionState`, `EventBus`, `LLMPort`,
   `SessionStorePort`, `ToolCallProcessor`, `ContextCompactor`,
   `SessionRunner`) are application/domain types, so the bundle is now a
   pure application data type. `build_session_runtime` (which constructs
   concrete adapters) **stays in bootstrap**.
2. Create `application/session.py` holding `BudgetExceededError`,
   `LoopDetectedError`, and a **pure** `Session` that:
   - **requires** an injected `runtime: SessionRuntime` (no self-build, no
     `bootstrap` import);
   - type-hints `env` / `tracer` with `EnvironmentPort` / `TracePort` from
     `application.ports` instead of the concrete `adapters` classes;
   - keeps the full public surface used at runtime: the `state` / `event_bus`
     / `store` / `tool_processor` / `compactor` / `runner` attributes, every
     property (`messages`, `used_tokens`, `step_count`, `is_done`, `phase`,
     `_recent_call_hashes`, `permission_policy`, `auto_save_path`),
     `run_loop`, `add_user_message`, `save`, `_auto_save`.
3. Create `bootstrap/session.py` with `class Session(application.session.Session)`
   that adds back the **composition** concerns:
   - `__init__` self-builds the runtime via `build_session_runtime(...)` when
     no `runtime` is supplied (the auto-wiring the tests rely on);
   - `snapshot()` and `load()` — both of which construct a new session and
     therefore need the bootstrap builder.
4. Repoint `core/session/__init__.py` to re-export the **bootstrap**
   `Session` (and `BudgetExceededError` / `LoopDetectedError` from
   `application.session`), delete `core/session/session.py`, and point the
   bootstrap/harness importers at `bootstrap.session`.

Behavior is preserved exactly; this is a structural split, not a logic
change.

## Why `snapshot` / `load` go on the bootstrap subclass

`snapshot()` builds a fresh session (new runtime) and `load()` builds a
session then reads messages from disk — both are *construction*, which under
Option B lives in bootstrap. The evidence says this costs nothing:

```
.snapshot()  -> tests only (test_session_construction, test_session_characterization,
                test_tool_call_processor_interceptor) — no production caller
Session.load -> bootstrap/session_factory.py:72 only
```

The tests call `snapshot()` / `load()` on `core.session.Session`, which is
the bootstrap subclass under Option B, so they resolve the methods by
inheritance. No `application/` code calls either (`grep` confirms only
docstrings in `application/` mention `Session`).

## Current Evidence

`core/session/session.py` forbidden imports (the reason it can't be in
`application/` unchanged):

```python
from opencollab.adapters.env import Environment      # line 10  (type hint only)
from opencollab.adapters.trace import Tracer          # line 13  (type hint only)
if TYPE_CHECKING:
    from opencollab.bootstrap.container import SessionRuntime   # line 18
# line 70, inside __init__ when runtime is None:
from opencollab.bootstrap.container import build_session_runtime
```

> The `Environment` / `Tracer` imports are used **only** as parameter type
> hints (`env: Environment | None`, `tracer: Tracer | None`) — runtime
> behavior never touches the concrete classes, so swapping to
> `EnvironmentPort` / `TracePort` is purely cosmetic. The
> `TYPE_CHECKING`-guarded `SessionRuntime` import is still a
> `from opencollab.bootstrap` line and **would trip the boundary regex**, so
> it must go (resolved by moving `SessionRuntime` into application). The
> `build_session_runtime` call is the actual construction inversion.

`SessionRuntime` reference inventory (proves the move is contained):

```
bootstrap/container.py:42   class SessionRuntime  (definition)
bootstrap/container.py:140  return SessionRuntime(...)
bootstrap/container.py:83   -> SessionRuntime    (return hint)
core/session/session.py:18,55  TYPE_CHECKING hint + runtime param
```

Non-core `Session(...)` constructors (all already import via `core.session`,
so Option B leaves them green):

```
bootstrap/teammate_factory.py:71, 137
bootstrap/session_factory.py:74   (+ Session.load at :72)
harness/evaluator.py:105
```

## Target Shape For This Step

```text
opencollab/opencollab/application/session.py   # new — SessionRuntime, BudgetExceededError,
                                                #        LoopDetectedError, pure Session
opencollab/opencollab/bootstrap/session.py     # new — Session(AppSession): self-build + snapshot + load
opencollab/opencollab/bootstrap/container.py   # SessionRuntime import moves to application;
                                                #   build_session_runtime stays
opencollab/opencollab/core/session/
  __init__.py        # Session <- bootstrap.session; errors <- application.session
  session.py         # deleted (content moved)
  runner.py / tools.py / compactor.py / state.py / events.py   # unchanged Step-12 shims
```

Dependency direction after this step:

```text
application/session.py   -> application.{ports,event_bus,autosave,context_compactor}, domain.*   (no adapters, no bootstrap)
bootstrap/session.py     -> application.session + bootstrap.container.build_session_runtime
bootstrap/container.py   -> application.session.SessionRuntime + adapters.* (constructs concretes)
core/session/__init__.py -> bootstrap.session.Session + application.session errors  (shim)
```

No cycle: `application.session` imports nothing from `bootstrap`;
`bootstrap.session` and `bootstrap.container` both point inward at
`application.session`.

Pure `Session.__init__` signature (sketch):
```python
def __init__(self, agent, *, runtime: SessionRuntime,
             env: EnvironmentPort | None = None,
             tracer: TracePort | None = None,
             max_budget_tokens: int = 200_000, max_steps: int = 100,
             compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
             auto_save_path: str | None = None,
             permission_policy: PermissionPort | None = None,
             safety_policy: SafetyPolicyPort | None = None): ...
```
The build-time-only kwargs (`repo_map`, `event_sink`, `llm`, `store`) drop
off the pure constructor — they are inputs to `build_session_runtime`, so
they live on the **bootstrap** subclass constructor.

## Implementation Plan

Single branch, suggested four commits. Run the suite after each.

### 1. Move `SessionRuntime` into `application/session.py`

- Create `application/session.py`; move the `SessionRuntime` dataclass there
  verbatim (its imports are now all application/domain).
- In `bootstrap/container.py`: delete the dataclass, add
  `from opencollab.application.session import SessionRuntime`, keep
  `build_session_runtime` and the `__all__`.
- Update `core/session/session.py`'s `TYPE_CHECKING` import to
  `from opencollab.application.session import SessionRuntime` (temporary —
  the file is deleted in commit 4).
- Commit: `refactor(application): move SessionRuntime bundle inward`.

### 2. Add the pure `Session` to `application/session.py`

- Copy the `Session` body + `BudgetExceededError` / `LoopDetectedError` from
  `core/session/session.py` into `application/session.py`.
- Swap the `adapters` type hints for `EnvironmentPort` / `TracePort`.
- Delete the `runtime is None` self-build branch and the lazy
  `build_session_runtime` import; make `runtime` a required keyword arg.
- Drop `repo_map` / `event_sink` / `llm` / `store` from the signature; keep
  the `env is None -> self.env = self.tool_processor.env` fallback.
- **Do not** define `snapshot` / `load` here (they move to bootstrap).
- Commit: `refactor(application): add pure Session use case`.

### 3. Add the bootstrap `Session` subclass

- Create `bootstrap/session.py`:
  ```python
  from opencollab.application.session import Session as _AppSession
  from opencollab.bootstrap.container import build_session_runtime

  class Session(_AppSession):
      def __init__(self, agent, *, env=None, tracer=None,
                   max_budget_tokens=200_000, max_steps=100,
                   compaction_threshold=DEFAULT_COMPACTION_THRESHOLD,
                   repo_map=None, auto_save_path=None, event_sink=None,
                   permission_policy=None, safety_policy=None,
                   llm=None, store=None, runtime=None):
          if runtime is None:
              runtime = build_session_runtime(
                  agent=agent, env=env, tracer=tracer,
                  max_budget_tokens=max_budget_tokens, max_steps=max_steps,
                  compaction_threshold=compaction_threshold, repo_map=repo_map,
                  auto_save_path=auto_save_path, event_sink=event_sink,
                  permission_policy=permission_policy, safety_policy=safety_policy,
                  llm=llm, store=store, auto_save_callback=self._auto_save,
              )
          super().__init__(agent, runtime=runtime, env=env, tracer=tracer,
                           max_budget_tokens=max_budget_tokens, max_steps=max_steps,
                           compaction_threshold=compaction_threshold,
                           auto_save_path=auto_save_path,
                           permission_policy=permission_policy,
                           safety_policy=safety_policy)
  ```
  - `self._auto_save` is a valid bound method as soon as `self` exists; it is
    only *invoked* later (during a run), by which point `super().__init__`
    has set `self._auto_save_path` and `self.store`. Construction order is
    safe — same as today.
- Move `snapshot()` and `load()` from the old facade onto this subclass
  verbatim (they already construct `Session(...)`, which now self-builds).
- Commit: `refactor(bootstrap): add self-wiring Session subclass`.

### 4. Repoint `core.session`, delete the old facade, retarget importers

- `core/session/__init__.py`:
  - `from opencollab.bootstrap.session import Session`
  - `from opencollab.application.session import BudgetExceededError, LoopDetectedError`
  - keep the wrapper / value-object re-exports unchanged; `__all__` identical.
- `git rm opencollab/opencollab/core/session/session.py`.
- Point the bootstrap-internal + harness importers at the bootstrap Session:
  - `bootstrap/teammate_factory.py:21`, `bootstrap/session_factory.py:16`,
    `harness/evaluator.py:22`:
    `from opencollab.core.session import Session` →
    `from opencollab.bootstrap.session import Session`.
    (Leaving them on `core.session` also works, but importing the
    bootstrap Session directly keeps the dependency explicit; tests stay on
    `core.session`.)
- Commit: `refactor(core): re-export Session from bootstrap, drop facade module`.

Final verify:
```bash
# application/session.py must not import adapters or bootstrap
rg "^\s*(from|import)\s+opencollab\.(adapters|bootstrap|core|cli|tools|team)\b" \
   opencollab/opencollab/application/session.py            # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_application_boundaries.py -q
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q       # expect 165 passed
```

## Acceptance Criteria

- `application/session.py` defines `SessionRuntime`, `BudgetExceededError`,
  `LoopDetectedError`, and a pure `Session` requiring an injected `runtime`;
  it imports **no** `adapters.*` and **no** `bootstrap.*`
  (`test_application_boundaries._offenders("application") == []`).
- `bootstrap/session.py` defines `Session(application.session.Session)` with
  the self-build `__init__`, `snapshot()`, and `load()`.
- `bootstrap/container.py` imports `SessionRuntime` from `application.session`
  and still owns `build_session_runtime`.
- `core/session/session.py` is deleted; `core/session/__init__.py` re-exports
  the bootstrap `Session`; `from opencollab.core.session import Session` and
  the `Session is session_mod.Session` identity assertion still hold.
- All ~30 characterization assertions and the team-lead session tests pass
  **with no test-file edits**.
- Full test suite passes: **165, no count change**.

## Non-Goals

- Do **not** introduce a runtime-builder *port* injected into the pure
  `Session` (that was the rejected Option C). Construction stays in the
  bootstrap subclass.
- Do **not** rename `Session`'s methods to the target's `RunSession` /
  `AddUserMessage` / `SnapshotSession` names. The target lists those as
  concepts; the cohesive `Session` use case is acceptable, matching the
  "don't split `Team` into `RunTeam`/…" decision in Step 11.
- Do **not** delete the `core/session/{runner,tools,compactor,state,events}`
  shims or `core/events.py` yet — Step 14.
- Do **not** edit any test file. If a characterization test fails, the split
  diverged from current behavior — fix the split, not the test.

## Rollback Plan

Four commits, revertible in reverse order. Commits 1-2 are inert additions
(new application module; nothing imports it yet). Commit 3 adds the bootstrap
subclass (still unused). Only commit 4 flips `core.session` over to it — so a
behavioral regression shows up there and reverting commit 4 alone restores
the working facade while keeping the new modules. The most likely failure is
a missed attribute on the pure/bootstrap split (e.g. a property the tests
read that landed on the wrong class); the characterization suite pinpoints it
by name.

## Sequencing note — closing the arc (Step 14)

After Step 13, `core/` contains only re-export shims: `core/__init__.py`,
`core/events.py`, and `core/session/{__init__,runner,tools,compactor,state,events}.py`.
**Step 14** retargets the remaining pins and deletes the package:
- Update `test_domain_boundaries.py` (the `core_*` identity assertions) and
  `test_session_characterization.py` imports to point at `application.*` /
  `bootstrap.session`.
- Delete `core/` entirely.
- Optionally adopt the target's `app.session` method names and refresh
  `docs/repomap/`. After Step 14 the repomap's "no `core/` package" rule
  holds and every inner-layer dependency points the target's way — closing
  the target-alignment effort.
