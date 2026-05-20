# Step12 - Relocate the session-runtime wrappers into `application/`

Date: 2026-05-20
Branch: `refactor/step12-core-wrappers` off the Step 11 branch.

> Part 1 of the **core/session dissolution arc** (Steps 12-14), the gap
> Step 11's sequencing note flagged as "its own multi-step arc." The target
> repomap (`docs/repomap/repomap-target.puml`) has **no `core/` package**:
> the runtime collaborators belong in the application layer
> (`app.session_runner`, `app.tools`, `app.context`) and the `Session`
> facade in `app.session`.
>
> This step peels off the cleanest leaves first — the three runtime
> *wrappers* (`SessionRunner`, `ToolCallProcessor`, `ContextCompactor`),
> which already import only `application.*` and `domain.*` save for one
> stray `adapters.llm` call. The hard part — the `Session` facade's
> construction inversion — is deferred to Step 13.

## Goal

Move the three runtime-collaborator wrappers out of `core/session/` into
`application/`, and break the single `adapters.llm` dependency that keeps
`ContextCompactor` out of the application layer:

1. `core/session/runner.py:SessionRunner` → `application/session_runner.py`
   (target slot `app.session_runner`).
2. `core/session/tools.py:ToolCallProcessor` (+ `CallbackPermissionPolicy`,
   the `PermissionPolicy` alias, the `MAX_*` constants) →
   `application/tool_processor.py` (target slot `app.tools`).
3. `core/session/compactor.py:ContextCompactor` →
   `application/context_compactor.py` (target slot `app.context`), with its
   hard-coded `estimate_messages_tokens` import replaced by an **injected**
   `TokenEstimatorPort` (the port already exists at
   `application/ports.py:151`).

The three `core/session/{runner,tools,compactor}.py` files **stay as thin
re-export shims** so every test that imports them by submodule path keeps
passing untouched. `core/session/__init__.py` and `bootstrap/container.py`
repoint at the new application modules.

This is a near-pure relocation. The **only** behavior-adjacent change is
the `ContextCompactor` token-estimator injection — and that is a
construction-site change (one caller), not a logic change.

## Why this is the safe first step

The wrappers are leaves: nothing in `core/session` depends *outward* on
them except `__init__.py` and `runner.py`'s sibling imports. Their import
surfaces are already almost application-pure:

```text
runner.py    -> application.event_bus, application.session_run,
                core.session.compactor, core.session.tools,   # sibling swaps
                domain.events, domain.session
tools.py     -> application.event_bus, application.ports,
                application.tool_dispatch, application.tool_execution,
                application.tool_runtime,
                domain.events, domain.session, domain.tools   # already clean
compactor.py -> application.compaction, application.event_bus,
                adapters.llm.estimate_messages_tokens,         # the one leak
                domain.events, domain.compaction, domain.session
```

So `tools.py` moves with zero edits beyond the shim; `runner.py` needs only
sibling-import swaps; `compactor.py` needs the estimator inversion.

## Current Evidence

### Constructor inventory (who builds the wrappers)

```
ContextCompactor(  -> bootstrap/container.py:120                    (sole site)
SessionRunner(     -> bootstrap/container.py:127
                      tests/test_session_run_loop.py:133
ToolCallProcessor( -> bootstrap/container.py:111
                      tests/test_tool_call_processor_interceptor.py (x6)
```

- `ContextCompactor` is constructed in **exactly one** place
  (`container.py`). Injecting `estimate_tokens` there is a one-line change;
  no test constructs the wrapper directly.
- `SessionRunner` / `ToolCallProcessor` signatures **do not change** in this
  step, so the tests that build them directly stay green.

### Submodule-path importers that pin the shims (must keep working)

```
core/session/__init__.py            (compactor, runner, tools)
bootstrap/container.py              (compactor, runner, tools)
core/session/session.py            (compactor.DEFAULT_COMPACTION_THRESHOLD)
tests/test_session_construction.py            (compactor, runner, tools, tools.PermissionPolicy)
tests/test_session_run_loop.py                (runner.SessionRunner)
tests/test_tool_call_processor_interceptor.py (tools.ToolCallProcessor; `import ... tools as tools_mod`)
tests/test_domain_boundaries.py               (compactor, state, tools — pins domain identities)
```

`test_domain_boundaries.py` asserts identity re-exports through the
submodule paths:

```python
core_compactor.CompactResult is domain_compaction.CompactResult
core_tools.ToolProcessingResult is domain_tools.ToolProcessingResult
core_tools.MAX_CALL_HASH_WINDOW == domain_tools.MAX_CALL_HASH_WINDOW
```

These hold automatically if the shims re-export the moved symbols (which
themselves still trace back to the domain value objects).

### The `ContextCompactor` estimator leak

`core/session/compactor.py:12`:
```python
from opencollab.adapters.llm import estimate_messages_tokens
...
self._use_case = ContextCompactionUseCase(..., estimate_tokens=estimate_messages_tokens, ...)
```

`ContextCompactionUseCase` already takes `estimate_tokens` as a parameter —
the wrapper is the only thing hard-coding the concrete adapter. Move the
hard-coding up to the composition root.

## Target Shape For This Step

```text
opencollab/opencollab/application/
  session_runner.py     # new — SessionRunner (moved)
  tool_processor.py     # new — ToolCallProcessor, CallbackPermissionPolicy,
                        #        PermissionPolicy alias, MAX_* constants (moved)
  context_compactor.py  # new — ContextCompactor (moved, estimate_tokens injected)
  compaction.py         # unchanged use case (ContextCompactionUseCase)
  session_run.py        # unchanged use case (SessionRunUseCase)
  tool_execution.py     # unchanged use case
  ...
opencollab/opencollab/core/session/
  __init__.py           # repointed at the application modules
  runner.py             # shim -> application.session_runner
  tools.py              # shim -> application.tool_processor
  compactor.py          # shim -> application.context_compactor
  session.py            # unchanged this step (moves in Step 13)
  state.py / events.py  # unchanged shims
```

Dependency direction after this step:

```text
application/session_runner.py    -> application.{event_bus,session_run,tool_processor,context_compactor}, domain.*
application/tool_processor.py    -> application.{event_bus,ports,tool_dispatch,tool_execution,tool_runtime}, domain.*
application/context_compactor.py -> application.{compaction,event_bus}, domain.*   (no adapters)
bootstrap/container.py           -> application.{session_runner,tool_processor,context_compactor} (+ passes estimate_messages_tokens)
core/session/{runner,tools,compactor}.py -> application.*   (re-export shims)
```

`ContextCompactor.__init__` final signature (sketch):
```python
def __init__(self, *, state, llm, event_bus, estimate_tokens,  # now required
             tracer=None, compaction_threshold=DEFAULT_COMPACTION_THRESHOLD): ...
```

## Implementation Plan

Single branch, suggested four commits. Run the suite after each.

### 1. Move `ToolCallProcessor` → `application/tool_processor.py`

- `git mv opencollab/opencollab/core/session/tools.py opencollab/opencollab/application/tool_processor.py`.
- No internal edits needed (its imports are already `application.*` /
  `domain.*`).
- Replace `core/session/tools.py` with a shim re-exporting the **exact**
  public surface the old module exposed:
  ```python
  from opencollab.application.tool_processor import (
      CallbackPermissionPolicy, PermissionPolicy, ToolCallProcessor,
      MAX_CALL_HASH_WINDOW, MAX_SIMILAR_CALLS, MAX_TOOL_OUTPUT_CHARS,
      ToolProcessingResult,
  )
  __all__ = [...]  # identical to before
  ```
- Commit: `refactor(application): relocate ToolCallProcessor`.

### 2. Move `SessionRunner` → `application/session_runner.py`

- `git mv opencollab/opencollab/core/session/runner.py opencollab/opencollab/application/session_runner.py`.
- Swap its two sibling imports:
  - `from opencollab.core.session.compactor import ContextCompactor` →
    `from opencollab.application.context_compactor import ContextCompactor`
  - `from opencollab.core.session.tools import ToolCallProcessor` →
    `from opencollab.application.tool_processor import ToolCallProcessor`

  > Ordering note: `context_compactor` does not exist until commit 3. To
  > keep each commit green, do commit 3 **before** this swap, or land
  > commits 1-3 together and swap last. Suggested real order:
  > **1 (tools) → 3 (compactor) → 2 (runner) → 4 (rewire)**. The numbering
  > here is by target module; the commit sequence is in the note.
- Replace `core/session/runner.py` with a shim:
  ```python
  from opencollab.application.session_runner import SessionRunner
  __all__ = ["SessionRunner"]
  ```
- Commit: `refactor(application): relocate SessionRunner`.

### 3. Move `ContextCompactor` → `application/context_compactor.py`, inject estimator

- `git mv opencollab/opencollab/core/session/compactor.py opencollab/opencollab/application/context_compactor.py`.
- Delete `from opencollab.adapters.llm import estimate_messages_tokens`.
- Add `estimate_tokens: TokenEstimatorPort` (import the protocol from
  `application.ports`) as a **required** keyword-only `__init__` param; pass
  it through to `ContextCompactionUseCase(estimate_tokens=estimate_tokens)`.
- Replace `core/session/compactor.py` with a shim:
  ```python
  from opencollab.application.context_compactor import (
      COMPACTION_KEEP_RECENT, DEFAULT_COMPACTION_THRESHOLD,
      CompactResult, ContextCompactor,
  )
  __all__ = [...]
  ```
  (`COMPACTION_KEEP_RECENT` / `DEFAULT_COMPACTION_THRESHOLD` / `CompactResult`
  are themselves re-exported by `context_compactor.py` from
  `application.compaction` / `domain.compaction`, exactly as `compactor.py`
  does today.)
- Commit: `refactor(application): relocate ContextCompactor, inject estimator`.

### 4. Repoint the composition root and the package init

- `bootstrap/container.py`:
  - imports → `from opencollab.application.context_compactor import DEFAULT_COMPACTION_THRESHOLD, ContextCompactor`,
    `from opencollab.application.session_runner import SessionRunner`,
    `from opencollab.application.tool_processor import ToolCallProcessor`.
  - `ContextCompactor(...)` call gains
    `estimate_tokens=estimate_messages_tokens` (the `adapters.llm` import
    moves *here*, where it belongs — bootstrap already imports `adapters`).
- `core/session/__init__.py`: repoint the three `from opencollab.core.session.{runner,tools,compactor}`
  imports at the new `application.*` modules (or leave them pointing at the
  now-shimmed submodules — either keeps the `__init__` surface identical;
  prefer importing straight from `application` so the shims are pure
  back-compat).
- `core/session/session.py`: swap
  `from opencollab.core.session.compactor import DEFAULT_COMPACTION_THRESHOLD`
  → `from opencollab.application.context_compactor import DEFAULT_COMPACTION_THRESHOLD`.
  (`session.py` itself does not move this step.)
- Commit: `refactor(bootstrap): point session runtime at application wrappers`.

Final verify:
```bash
# application layer must not import any outer layer (esp. the new adapters-free compactor)
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_application_boundaries.py tests/test_domain_boundaries.py -q
rg "from opencollab\.adapters" opencollab/opencollab/application/   # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q                # expect 165 passed
```

## Acceptance Criteria

- `application/session_runner.py`, `application/tool_processor.py`,
  `application/context_compactor.py` exist and hold the moved classes.
- `application/context_compactor.py` imports **no** `adapters.*`; it receives
  the estimator via the `estimate_tokens` parameter typed as
  `TokenEstimatorPort`.
- `core/session/{runner,tools,compactor}.py` exist only as re-export shims;
  their public surface (names + identities) is byte-for-byte what
  `test_session_construction.py`, `test_session_run_loop.py`,
  `test_tool_call_processor_interceptor.py`, and `test_domain_boundaries.py`
  pin today.
- `bootstrap/container.py` constructs the wrappers from `application.*` and
  passes `estimate_messages_tokens` into `ContextCompactor`.
- `test_application_boundaries._offenders("application") == []` still holds
  (the relocated compactor does not introduce an adapter import).
- Full test suite passes: **165, no count change**.

## Non-Goals

- Do **not** move `core/session/session.py` (the `Session` facade) or touch
  its `adapters`/`bootstrap` imports — that is Step 13, the construction
  inversion the user chose to handle via a bootstrap-owned convenience
  `Session` exposed through a `core.session` shim.
- Do **not** rename the classes to the target's aspirational names
  (`SessionStateMachine`, `ExecuteToolCalls`/`ToolRegistry`, `CompactContext`).
  Relocation keeps names, matching Steps 09-11.
- Do **not** collapse the wrapper/use-case duplication
  (`context_compactor.py` wrapper vs `compaction.py` use case;
  `tool_processor.py` wrapper vs `tool_execution.py` use case). The wrappers
  exist to carry the `SessionRuntimeEvent` event-factory wiring and the
  characterization surface; merging them is a separate optional
  simplification, not part of the dissolution relocation.
- Do **not** delete the `core/` shims yet — Step 14.

## Rollback Plan

Four commits, revertible in reverse order. The riskiest is commit 3
(estimator injection): if a hidden constructor of `ContextCompactor` exists
that this plan missed, it surfaces immediately as a `TypeError` on the
missing `estimate_tokens` arg. The constructor inventory above shows
`container.py` is the sole site, so the blast radius is one line. Revert
commit 3 alone to restore the `adapters.llm` import inside the (still
relocated) compactor while keeping commits 1-2.

## Sequencing note — the rest of the arc (Steps 13-14)

**Step 13 — Promote the `Session` facade to `application/session.py`
(the hard fork).**
The facade today lazy-imports `bootstrap.container.build_session_runtime`
and type-hints `adapters.env.Environment` / `adapters.trace.Tracer` — all
three are forbidden to `application/` by `test_application_boundaries.py`.
Per the chosen approach (**bootstrap convenience + core shim**):
- `application/session.py` holds a **pure** `Session` that requires an
  injected `runtime` (the `SessionRuntime` bundle) and uses
  `EnvironmentPort` / `TracePort` type hints, not the concrete adapters.
- `bootstrap/session.py` holds a `Session` *subclass* whose `__init__`
  self-builds the runtime via `build_session_runtime(...)` when none is
  supplied — the auto-wiring convenience the REPL and the ~20
  characterization constructions rely on.
- `core/session/__init__.py` re-exports the **bootstrap** `Session`, so
  `from opencollab.core.session import Session` and the
  `Session is session_mod.Session` identity test stay green with zero test
  edits. `bootstrap.teammate_factory` / `bootstrap.session_factory` /
  `harness.evaluator` import the bootstrap `Session` (or the pure one +
  explicit runtime — bootstrap may import either).

**Step 14 — Delete the `core/` package, retarget the pins.**
Once Step 13 lands, the only thing keeping `core/` alive is the shim set and
`test_domain_boundaries.py` / `test_session_characterization.py` importing
through it. Retarget those imports at `application.*` (and the bootstrap
`Session`), delete `core/events.py`, `core/session/*`, and `core/__init__.py`,
and update `test_domain_boundaries.py`'s `core_*` identity assertions to the
application modules. Optionally introduce the target's `app.session` method
names (`RunSession` / `AddUserMessage` / `SnapshotSession`) and refresh
`docs/repomap/`. After Step 14 the repomap's "no `core/` package" rule holds
and every inner-layer dependency points the target's way.
