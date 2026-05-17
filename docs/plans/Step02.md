# Step 02 — Collapse `Session.__init__` channels & drop the `importlib` LLM hack

Part of the refactor sequence identified in `docs/repomap/module_map.puml`.
This is **step 2 of 7**: simplify the `Session` (and `Team`) public API by
removing the three event channels, two permission channels, two LLM channels,
and the testability-driven `importlib` indirection. Same direction continues
in steps 3, 5, 6, 7.

---

## Goal

Today `Session.__init__` accepts the same concept under multiple names:

| Concept | Today (parameters) | After step 2 |
|---|---|---|
| Event consumer | `event_bus`, `event_sink`, `on_event` | `event_sink` only |
| Permission gate | `permission_policy`, `confirm_fn` | `permission_policy` only |
| LLM client | `llm`, `llm_client` + `importlib` hack | `llm` only |

`Team.__init__` has the same problem (`on_event`, `confirm_fn`, `permission_policy`,
`event_sink`). It is collapsed the same way in this PR.

This is a **breaking change** to the public API of `Session` and `Team`. The
codebase is pre-1.0, all internal callers are in-repo, so we remove the legacy
parameters outright rather than deprecating.

Carry-over from step 1: expose `Session.auto_save_path` as a public read-only
property, eliminating the `session._auto_save_path` private access in
`cli/main.py:224`.

---

## Concrete edits

### 1. `opencollab/core/session/session.py`

**`Session.__init__` parameter list — remove:**
- `on_event: EventCallback | None = None`
- `confirm_fn: Callable[[str], Awaitable[bool]] | None = None`
- `event_bus: EventBus | None = None`
- `llm_client=None`

**Keep:**
- `event_sink: EventSink | None = None`
- `permission_policy: PermissionPolicy | None = None`
- `llm=None`  (single injection point; `None` ⇒ build default `LLMClient`)

**Body simplifications:**

```python
# BEFORE
self.event_bus = event_bus if event_bus is not None else EventBus(
    event_sink if event_sink is not None else on_event
)
if event_bus is not None and (event_sink is not None or on_event is not None):
    self.event_bus.set_target(event_sink if event_sink is not None else on_event)
self._confirm_fn = confirm_fn
self._permission_policy = permission_policy or (
    CallbackPermissionPolicy(confirm_fn) if confirm_fn is not None else None
)
...
injected_llm = llm if llm is not None else llm_client
if injected_llm is not None:
    self._llm = injected_llm
else:
    llm_cls = getattr(import_module("opencollab.core.session"), "LLMClient")
    self._llm = llm_cls(model=..., ...)

# AFTER
self.event_bus = EventBus(event_sink)
self._permission_policy = permission_policy
...
if llm is not None:
    self._llm = llm
else:
    self._llm = LLMClient(
        model=agent.model,
        api_key=agent.api_key,
        base_url=agent.base_url,
        provider=agent.provider,
    )
```

Import `LLMClient` directly at the top of `session.py` instead of via
`importlib.import_module(...)`. Drop the `from importlib import import_module`
line.

**Remove dead members:**
- `self._confirm_fn` attribute (no longer set).
- `@property on_event` getter/setter (lines 114–120).
- `@property confirm_fn` getter/setter (lines 122–128).
- Keep `@property permission_policy` getter/setter — it still legitimately
  propagates writes to `self.tool_processor.permission_policy`.

**`Session.snapshot()` (lines 197–213):** stop forwarding the deleted
parameters. Replace with:

```python
def snapshot(self) -> Session:
    new = Session(
        agent=self.agent,
        env=self.env,
        tracer=self.tracer,
        max_budget_tokens=self.max_budget_tokens,
        max_steps=self.max_steps,
        compaction_threshold=self.compaction_threshold,
        event_sink=self.event_bus.sink,
        permission_policy=self.permission_policy,
    )
    new.messages = copy.deepcopy(self.messages)
    new.used_tokens = self.used_tokens
    new.step_count = self.step_count
    return new
```

**Add public auto-save property (step 1 carry-over):**

```python
@property
def auto_save_path(self) -> str | None:
    return self._auto_save_path
```

### 2. `opencollab/core/session/__init__.py`

No re-export changes needed (`LLMClient` is still re-exported from
`opencollab.core.llm` for tests and external users). Verify the line
`from opencollab.core.llm import LLMClient` stays.

### 3. `opencollab/team/orchestrator.py`

**`Team.__init__` — remove parameters:**
- `on_event: Callable[..., ...] | None = None`
- `confirm_fn: Callable[[str], Awaitable[bool]] | None = None`

**Keep:**
- `event_sink: EventSink | None = None`
- `permission_policy: PermissionPolicy | None = None`

**Remove fields:** `self.on_event`, `self.confirm_fn`.

**Update the two `Session(...)` constructions** (Lead at line 200-212 and
teammate at line 291-301) — drop `confirm_fn=...` and `on_event=...`; keep
`event_sink=self.event_bus` and `permission_policy=self.permission_policy`.

### 4. `opencollab/cli/main.py`

One line cleanup using the new public property:

```python
# BEFORE
if session._auto_save_path:
    console.print(f"[dim]Session auto-saving to {session._auto_save_path}[/dim]")

# AFTER
if session.auto_save_path:
    console.print(f"[dim]Session auto-saving to {session.auto_save_path}[/dim]")
```

### 5. `opencollab/bootstrap/session_factory.py`

No changes expected — bootstrap already passes `event_sink=` and
`permission_policy=` and never used the legacy names. Verify nothing slipped.

### 6. `opencollab/harness/evaluator.py`

Audit `run_eval_task`: it constructs `Session(...)` with only the modern
parameters today (no `on_event`, no `confirm_fn`, no `llm_client`), so no edit
expected. Verify.

### 7. `opencollab/tests/test_session_characterization.py`

This file is where almost all of the churn lives. ~30 call sites use the
deprecated parameters. Migration rules:

| Old call | New call |
|---|---|
| `Session(..., on_event=cb)` | wrap `cb` as an `EventSink` or pass via `EventBus`; `Session(..., event_sink=EventBus(cb))` |
| `Session(..., confirm_fn=fn)` | `Session(..., permission_policy=CallbackPermissionPolicy(fn))` |
| `Session(..., llm_client=fake)` | `Session(..., llm=fake)` |
| `Session(..., event_bus=bus)` | `Session(..., event_sink=bus)` (EventBus implements `.emit`) |

**The `install_fake_llm` fixture (lines 115–121) must be rewritten.** Today it
monkey-patches `session_mod.LLMClient`. After we import `LLMClient` directly
into `session.py`, that monkey-patch path no longer hits the real call site.

Replace the fixture with one that simply produces a fake and have each test
pass it explicitly:

```python
@pytest.fixture
def fake_llm_factory():
    def _make(fake_llm):
        return fake_llm
    return _make

# call sites change from
install_fake_llm(FakeLLMClient([...]))
session = Session(agent=FakeAgent())
# to
fake_llm = FakeLLMClient([...])
session = Session(agent=FakeAgent(), llm=fake_llm)
```

This is mechanical but high-volume. The worker should do it in one pass and
verify with `pytest opencollab/tests/test_session_characterization.py` before
moving on.

**Two tests test removed behavior:**

- The test that passes both `event_bus=...` and `event_sink=...` to verify
  override semantics (around line 378). The legacy override path no longer
  exists. **Delete this test.**
- Any test that asserts `session.on_event` or `session.confirm_fn` returns the
  injected value. **Delete or rewrite** to assert via the `event_bus.sink` /
  `permission_policy` properties instead.

The worker should grep for `session.on_event` and `session.confirm_fn` reads
(not just writes/init) and handle each.

---

## What is NOT in this PR

- `Session._step` / `_advance` / `_handle_assistant_response` / etc.
  compatibility shims — that's step 3.
- `TUI._active_instance` singleton — step 4.
- `SandboxInterceptor` unification — step 5.
- `Team` decomposition beyond the parameter cleanup above — step 6.
- Moving `auto_save` from a callback to an event subscriber — step 7.

---

## Tests

All existing tests must pass after migration. Specifically check:

- `pytest opencollab/tests/test_session_characterization.py` — bulk of the churn.
- `pytest opencollab/tests/test_bootstrap.py` — should pass unchanged.
- Any other test file under `opencollab/tests/` that imports `Session` or
  `Team` (run the full suite).

Add one new test in `test_session_characterization.py`:

- `test_session_auto_save_path_is_public` — construct a Session with
  `auto_save_path="foo.jsonl"`, assert `session.auto_save_path == "foo.jsonl"`,
  assert `Session(...).auto_save_path is None` when not passed.

---

## Acceptance checklist

- [ ] `Session.__init__` parameter list is reduced to the modern surface
      (`agent`, `env`, `tracer`, `max_budget_tokens`, `max_steps`,
      `compaction_threshold`, `repo_map`, `auto_save_path`, `event_sink`,
      `permission_policy`, `llm`, `store`).
- [ ] `Team.__init__` no longer accepts `on_event` or `confirm_fn`.
- [ ] `Session.on_event` and `Session.confirm_fn` properties are removed.
- [ ] `Session.auto_save_path` exists as a read-only property.
- [ ] `cli/main.py` uses `session.auto_save_path` (no underscore).
- [ ] `from importlib import import_module` is gone from `session.py`;
      `LLMClient` is imported directly.
- [ ] No file under `opencollab/` references `llm_client=`, `on_event=`,
      `confirm_fn=`, or `event_bus=` as a keyword argument *to Session or
      Team*. (Tool-level `confirm_fn=` parameters in `tools/*` are
      unaffected — those are a different concept.)
- [ ] `install_fake_llm` fixture is rewritten; all test call sites migrated.
- [ ] Full pytest suite passes.
- [ ] `python -m opencollab chat` and `python -m opencollab team "<task>"`
      still work end-to-end (smoke test).

---

## Risk & rollback

- This is a breaking change to `Session`/`Team`'s public API. Any out-of-tree
  callers — if they exist — will break. Search the PR description for a note
  about this.
- The largest source of risk is the `install_fake_llm` rewrite. If a test
  silently still references `session_mod.LLMClient` after the monkey-patch is
  gone, it will hit the real OpenAI SDK and fail with a network error rather
  than a clear AssertionError. Mitigation: run the test suite under a fake
  `OPENAI_API_KEY` so any miss surfaces as an auth error immediately.
- Rollback: single `git revert` of the PR.

---

## Out of scope clarifications

- **Tool-level `confirm_fn`**: `Tool.execute(..., confirm_fn=...)` and
  `SandboxInterceptor.check_cmd_interactive(..., confirm_fn=...)` are
  **unchanged**. That parameter is how a Tool gets a callback for asking the
  user at execution time. It is wired by `ToolCallProcessor._tool_confirm_fn`
  from the session's `permission_policy.confirm`. Do not touch.
- **`EventBus` class itself**: keep the `on_event` getter/setter on `EventBus`
  for now (`events.py:35-41`). Removing those is fine but is not required for
  this step. If the worker chooses to remove them, also delete the
  `set_target`'s callable-vs-sink branching since only `EventSink` is left.
  Recommended: defer this trim — it's cosmetic and unrelated to the API
  collapse.

Human-readable summary for the PR description: *"Collapse Session/Team to a
single event/permission/LLM injection channel each, and drop the `importlib`
test hack. Breaking change; all internal callers and tests migrated."*
