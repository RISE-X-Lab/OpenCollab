# Step14 - Delete the `core/` package, retarget the test pins

Date: 2026-05-20
Branch: `refactor/step14-drop-core` off the Step 13 branch.

> Part 3 (final) of the **core/session dissolution arc** (Steps 12-14), and
> the step that makes the target repomap's "**no `core/` package**" rule
> literally true. After Steps 12-13, `core/` holds **only re-export shims**
> and has **zero production importers** — `grep` for
> `from opencollab.core` / `import opencollab.core` across `opencollab/`
> (excluding `core/` itself) returns only a stale docstring line in
> `application/tool_processor.py:25`. Everything else pointing at `core` is a
> test file.
>
> This is therefore the **first step in the whole effort that edits test
> files** (the zero-churn property of the Option-B path ends here, by
> design). The edits are mechanical import retargets — no test *logic*
> changes — plus the removal of a few assertions that existed solely to pin
> the shim layer being deleted.

## Goal

1. Retarget every test import of `opencollab.core.*` at the real homes the
   symbols live in now (`application.*`, `bootstrap.session`, `domain.*`).
2. Remove the assertions whose only purpose was to verify the `core` shim
   re-exports (they become meaningless once `core` is gone).
3. Delete the `core/` package entirely.
4. Tidy the one stale docstring reference in `application/tool_processor.py`.

End state: `opencollab/core/` does not exist; no module or test imports
`opencollab.core`; the repomap's layer set (domain / application / adapters /
bootstrap, with tools+cli still pending cosmetic moves) matches the target.

## Symbol → new home map

| Symbol(s) | Import from |
|---|---|
| `EventBus`, `EventCallback`, `EventSink` | `opencollab.application.event_bus` |
| `SessionEvent` | `opencollab.domain.events` (`SessionRuntimeEvent as SessionEvent`) |
| `Session` | `opencollab.bootstrap.session` |
| `BudgetExceededError`, `LoopDetectedError` | `opencollab.application.session` |
| `SessionRunner` | `opencollab.application.session_runner` |
| `ContextCompactor`, `COMPACTION_KEEP_RECENT`, `DEFAULT_COMPACTION_THRESHOLD`, `CompactResult` | `opencollab.application.context_compactor` |
| `ToolCallProcessor`, `CallbackPermissionPolicy`, `PermissionPolicy`, `MAX_CALL_HASH_WINDOW`, `MAX_SIMILAR_CALLS`, `MAX_TOOL_OUTPUT_CHARS`, `ToolProcessingResult` | `opencollab.application.tool_processor` |
| `SessionState`, `SessionPhase` | `opencollab.domain.session` |

## Current Evidence

`core/` after Step 13 (all shims):

```
core/__init__.py          # re-exports Agent + Session
core/events.py            # re-exports EventBus/SessionEvent (compat)
core/session/__init__.py  # re-exports the wrappers + Session (+ `session` alias to bootstrap.session)
core/session/events.py    # compat EventBus/SessionEvent
core/session/compactor.py # shim -> application.context_compactor
core/session/runner.py    # shim -> application.session_runner
core/session/tools.py     # shim -> application.tool_processor
core/session/state.py     # shim -> domain.session
```

Test-only importers of `core.*` (the full retarget set):

```
test_session_run_loop.py:7-8                core.session.events.EventBus, core.session.runner.SessionRunner
test_session_construction.py:13,15,16,18,36,80   Session, compactor, runner, tools, tools.PermissionPolicy, events.SessionEvent
test_autosave_subscriber.py:9,11            core.session.Session; core.session.events.{EventBus,SessionEvent}
test_team_decomposition.py:11               core.session.events.EventBus
test_session_characterization.py:6,7,8,10   core.session pkg (session_mod), core.events.{EventBus,SessionEvent}, core.session.{...}
test_tool_call_processor_interceptor.py:10,11,12,13,261   bootstrap.session via core alias; events; state; tools; tools as tools_mod
```

Two important non-surprises confirmed by reading the code:

- `test_tool_call_processor_interceptor.py:244` walks
  `("opencollab.tools", "opencollab.team")` — **not** `core` — so the
  package-walk test is unaffected by the deletion.
- `session_mod` in `test_tool_call_processor_interceptor.py:10`
  (`from opencollab.core.session import session as session_mod`) resolves to
  the **`bootstrap.session` module** via the `from opencollab.bootstrap import session as session`
  alias Step 13 added to `core/session/__init__.py`. It is used only as
  `session_mod.Session` (lines 51/65/76) and via `inspect.getsource` (line
  267). Retarget it straight to `from opencollab.bootstrap import session as session_mod`.

### Assertions that die with the shim layer

- `test_domain_boundaries.py:37` `test_core_session_reexports_domain_value_objects`
  and `:44` `test_legacy_core_session_modules_reexport_domain_value_objects`
  — they assert the `core` shims re-export domain identities. Delete both
  (and the `core_session` / `core_compactor` / `core_state` / `core_tools`
  imports at lines 3-6). The remaining tests in that file
  (`test_domain_modules_do_not_import_outer_layers`, the two `ToolSpec`
  tests) read files / `application` directly and stay.
- `test_session_characterization.py:162`
  `test_session_package_and_compat_event_imports_are_preserved` — once
  everything imports the canonical classes, `Session is session_mod.Session`
  / `CompatEventBus is EventBus` are tautologies. Delete it (and the
  `core.events` compat imports at lines 7-8).

## Test count change (read this)

This step **intentionally lowers the test count** — the first time in the
arc. Removing the three shim-pinning tests above takes the suite from
**165 → 162**. That is expected and correct; do not "restore" the count by
re-adding shim tests. If you prefer, the two `*reexport*` assertions in
`test_domain_boundaries.py` may instead be *repurposed* to assert the
**application** modules expose the same domain identities
(`application.context_compactor.CompactResult is domain.compaction.CompactResult`,
etc.) — but that is testing a trivial re-export and adds little; deletion is
the recommended call. Pick one and state the resulting count in the commit.

## Implementation Plan

Single branch, suggested three commits.

### 1. Retarget the straightforward test imports

Edit imports only (no logic) in:
- `test_session_run_loop.py`, `test_session_construction.py`,
  `test_autosave_subscriber.py`, `test_team_decomposition.py`,
  `test_tool_call_processor_interceptor.py` — per the symbol map above.
- In `test_tool_call_processor_interceptor.py`: point `session_mod` at
  `opencollab.bootstrap.session`; point the `tools_mod` import at
  `opencollab.application.tool_processor` (line 261). Optionally rename
  `test_core_session_tools_does_not_import_concrete_sandbox` →
  `test_tool_processor_does_not_import_concrete_sandbox` and
  `test_core_session_session_does_not_import_bootstrap_safety` →
  `test_bootstrap_session_does_not_import_bootstrap_safety` (the assertions
  hold unchanged against the new targets).
- Commit: `test: retarget session imports off core shims`.

### 2. Retarget characterization + drop the shim-pin assertions

- `test_session_characterization.py`: replace the `from opencollab.core import session as session_mod`
  and `from opencollab.core.events import ...` and `from opencollab.core.session import (...)`
  blocks with direct imports per the map. Replace each `session_mod.X`
  (lines 163, 164, 319, 485, 624, 626, 640, 642, 709) with the bare name now
  imported directly. Delete
  `test_session_package_and_compat_event_imports_are_preserved`.
- `test_domain_boundaries.py`: delete the `core_*` imports (lines 3-6) and
  the two `*reexport*` tests (lines 37, 44).
- Commit: `test: drop core-shim reexport assertions`.

### 3. Delete `core/` and tidy the stale docstring

- `git rm -r opencollab/opencollab/core`.
- `application/tool_processor.py:23-27`: drop the docstring sentence that
  claims `from opencollab.core.session import PermissionPolicy` still works
  (it no longer does).
- Leave the `"opencollab.core"` entries in the **boundary tests**
  (`test_application_boundaries.py` FORBIDDEN regex,
  `test_domain_boundaries.py` forbidden list) — they are string guards that
  keep future code from importing a (now non-existent) `core`, which is
  still a valid rule to enforce.
- Commit: `refactor(core): delete the dissolved core package`.

Final verify:
```bash
rg "from opencollab\.core|import opencollab\.core" opencollab/ tests/   # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q                    # expect 162 passed
```

## Acceptance Criteria

- `opencollab/opencollab/core/` does not exist.
- No `from opencollab.core` / `import opencollab.core` statement remains in
  `opencollab/` or `tests/` (the only surviving `opencollab.core` *strings*
  are the boundary-test guards).
- All retargeted tests pass with logic unchanged; the only removed tests are
  the three that pinned the deleted shim layer.
- `test_application_boundaries.py` and `test_domain_boundaries.py` still pass
  (domain/application import nothing outward).
- Full suite: **162 passed** (165 minus the 3 shim-pin tests).

## Non-Goals

- Do **not** rename `Session` methods to the target's `RunSession` /
  `AddUserMessage` / `SnapshotSession`, or split the `ToolCallProcessor` /
  `ContextCompactor` wrappers into their use cases — those simplifications
  are independent of the `core` deletion.
- Do **not** touch `cli/`, `tools/`, `team/`, or `harness/` — the remaining
  cosmetic relocations are a separate effort (see closing note).
- Do **not** weaken the boundary tests' forbidden-list guard on
  `opencollab.core`; keeping it prevents accidental resurrection.

## Rollback Plan

Three commits, revertible in reverse order. Commits 1-2 are test-only and
keep `core/` alive, so they pass on their own (the shims still back the new
targets too). Only commit 3 removes the package; if a `core` importer was
missed, it surfaces immediately as a collection-time `ModuleNotFoundError`
that names the offending test, and reverting commit 3 alone restores the
package while keeping the retargets.

## Closing note — the dissolution arc is done; what's left for full target parity

After Step 14 the **core/session dissolution arc (Steps 12-14) is complete**
and the target's inner-layer dependency rules hold with no `core/` package.
The remaining gaps to a byte-for-byte target repomap are **cosmetic
relocations**, each a self-contained future step:

- `cli/` → `adapters.cli` (target slot `adapters.cli`).
- top-level `tools/` → `adapters.tools` (needs the `application`/`domain`
  boundary-test regex updated, since it currently forbids `opencollab.tools`,
  plus broad import rewrites across `bash`/`fs`/`human`/`mcp`/`delegation`/`base`).
- delete the `team/` re-export shims once the last callers point at
  `application.team`.
- decide `harness/` — it is **not in the target map**; either relocate it as
  out-of-scope eval tooling or leave it as a clearly-labelled non-architectural
  package.

None of these change the dependency *direction*; they are package renames
for repomap fidelity.
