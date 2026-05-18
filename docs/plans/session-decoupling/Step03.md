# Step 03 — Remove `Session` compatibility shims; runner is the only owner of the loop

Part of the refactor sequence identified in `docs/repomap/module_map.puml`.
This is **step 3 of 7**: delete the 11 private compatibility methods on
`Session` that duplicate or delegate to `SessionRunner`, `ContextCompactor`,
and `ToolCallProcessor`. After this PR, `Session` is purely a facade for
construction + the small public API (`run_loop`, `add_user_message`,
`snapshot`, `save`, `load`, plus property mirrors of `SessionState`).

This is a small, mechanical PR — the surgery is concentrated in one file and
no test changes are expected.

---

## Goal

Today `Session` carries a tail of private methods labelled
`# Compatibility helpers for tests and older internal callers.` (lines
200–248 of `core/session/session.py`):

```
_advance, _step, _should_compact, _compact, _process_tool_calls,
_build_tool_schemas, _call_llm, _record_llm_trace, _append_assistant_message,
_handle_assistant_response, _finish_step
```

A repo-wide search (see "Verification" below) confirms **none** of these are
called from outside `Session` itself. Existing tests already exercise the loop
through `session.runner.*`, `session.compactor.*`, and
`session.tool_processor.*` directly. The shims are dead weight that obscure
which methods are the canonical entry points.

`_handle_assistant_response` is special: it is *not* pure delegation — it
duplicates a slice of `SessionRunner._handle_pending_response`. Keeping it
risks divergence (whichever path a future change touches first will be the
"correct" one). Removing it forces `Runner` to remain the single source of
truth.

---

## Concrete edits

### 1. `opencollab/core/session/session.py`

**Delete lines 200–248 wholesale** — the entire "Compatibility helpers"
block. Specifically remove these methods:

| Method | What it did | Why safe to remove |
|---|---|---|
| `_advance` | `await self.runner._advance(...)` | Pure delegation; no callers. |
| `_step` | Composed 4 runner calls into one "do a whole step". | No callers; tests drive `run_loop` or `runner._advance` directly. |
| `_should_compact` | `return self.compactor.should_compact()` | Pure delegation; tests call `session.compactor.should_compact()` directly (test line 637). |
| `_compact` | Ran compaction + auto-save. | No callers; tests use `session.compactor.compact()` (test lines 646, 661, 679). |
| `_process_tool_calls` | `await self.tool_processor.process(...)` | No callers; tests use `session.tool_processor.process(...)` directly (test line 455). |
| `_build_tool_schemas` | `return self.runner._build_tool_schemas()` | Pure delegation; no callers. |
| `_call_llm` | `return await self.runner._call_llm(...)` | Pure delegation; no callers. |
| `_record_llm_trace` | `self.runner._record_llm_trace(...)` | Pure delegation; no callers. |
| `_append_assistant_message` | `self.runner._append_assistant_message(...)` | Pure delegation; no callers. |
| `_handle_assistant_response` | **Duplicated** runner logic (emit text_delta + dispatch tool_calls or mark_done). | No callers; `SessionRunner._handle_pending_response` is the canonical path. Tests already exercise the runner path (test line 400). |
| `_finish_step` | `await self.runner._finish_step(...)` | Pure delegation; no callers. |

**Drop the now-unused imports** at the top of `session.py`:

```python
# BEFORE
from opencollab.core.llm import LLMClient, LLMResponse
from opencollab.core.session.events import EventBus, EventSink, SessionEvent

# AFTER
from opencollab.core.llm import LLMClient
from opencollab.core.session.events import EventBus, EventSink
```

`LLMResponse` was only used in the `_call_llm`, `_record_llm_trace`,
`_append_assistant_message`, and `_handle_assistant_response` type hints.
`SessionEvent` was only used inside `_handle_assistant_response`. Verify with
`grep` after the deletion that neither symbol appears in `session.py`.

### 2. `opencollab/core/session/__init__.py`

No re-export changes needed. The package-level `LLMResponse` and `SessionEvent`
exports come from `opencollab.core.llm` and
`opencollab.core.session.events` respectively, not from `session.py`. Verify
the `__init__.py` lines:

```python
from opencollab.core.llm import LLMClient
from opencollab.core.session.events import EventBus, EventCallback, EventSink, SessionEvent
```

remain untouched.

### 3. Nothing else changes

- `core/session/runner.py` — untouched. It already owns the loop.
- `core/session/compactor.py` — untouched.
- `core/session/tools.py` — untouched.
- `core/session/state.py` — untouched. The state-mirror properties on
  Session (`messages`, `used_tokens`, `step_count`, `is_done`,
  `_recent_call_hashes`, `phase`) **stay** — they are heavily used by tests
  and are not part of the "compatibility helpers" block.
- `bootstrap/`, `cli/main.py`, `team/orchestrator.py`,
  `harness/evaluator.py` — untouched.
- Tests — untouched. If a test breaks, that test was using a shim and the
  worker needs to migrate it to the canonical path (`session.runner.*`,
  `session.compactor.*`, `session.tool_processor.*`).

---

## Verification

Before the deletion, the worker should run these greps to confirm no
out-of-`session.py` callers exist:

```bash
grep -rn '\._step(\|\._advance(\|\._compact(\|\._should_compact(\|\._process_tool_calls(\|\._build_tool_schemas(\|\._call_llm(\|\._record_llm_trace(\|\._append_assistant_message(\|\._handle_assistant_response(\|\._finish_step(' \
  opencollab/opencollab opencollab/tests
```

Expected matches (all internal to runner.py or session.py itself):

- `core/session/runner.py` — runner calls its own `_advance`,
  `_build_tool_schemas`, `_call_llm`, `_record_llm_trace`,
  `_append_assistant_message`, `_finish_step`. These are runner methods, not
  Session shims — leave them.
- `core/session/session.py` — the shim definitions themselves (these are
  what we're deleting).

If any test file appears in the grep output, **stop and update the plan** —
something invokes a shim that the audit missed.

---

## Tests

No new tests. All 36 existing tests must continue to pass without
modification. Run:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Expected: `36 passed`.

---

## Acceptance checklist

- [ ] `core/session/session.py` no longer contains the comment line
      `# Compatibility helpers for tests and older internal callers.`
- [ ] None of the 11 method names listed above appear in
      `core/session/session.py`.
- [ ] `LLMResponse` is no longer imported in `core/session/session.py`.
- [ ] `SessionEvent` is no longer imported in `core/session/session.py`.
- [ ] `core/session/__init__.py` still re-exports `LLMResponse` (from
      `core.llm`) and `SessionEvent` (from `core.session.events`).
- [ ] Full test suite passes (`36 passed`) with no test edits.
- [ ] `python -m opencollab chat` smoke-test still runs.
- [ ] Net diff is a deletion: ~50 lines removed from `session.py`, no other
      files changed.

---

## Risk & rollback

- The lowest-risk PR in the refactor sequence. Pure dead-code removal in one
  file. No public API removed (the shims are private, underscore-prefixed).
- Risk: a downstream caller in a sibling project may rely on
  `session._step()` or similar. Search confirms no in-repo callers; out-of-repo
  callers, if they exist, are using a private API and accept the breakage.
- Rollback: single `git revert`.

---

## What is NOT in this PR

- The state-mirror properties on `Session` (`messages`, `used_tokens`,
  `step_count`, `is_done`, `_recent_call_hashes`, `phase`). These are
  legitimate public API — tests and `snapshot()` both use them. Touching
  them would force test rewrites and belongs in a separate "tighten the
  Session ↔ State boundary" PR if ever needed.
- `TUI._active_instance` singleton — step 4.
- `SandboxInterceptor` unification — step 5.
- `Team` decomposition — step 6.
- `auto_save` event subscriber — step 7.

Human-readable summary for the PR description: *"Remove `Session`'s 11
compatibility shim methods. Runner is now the only owner of the loop.
No behavior change; no test edits."*
