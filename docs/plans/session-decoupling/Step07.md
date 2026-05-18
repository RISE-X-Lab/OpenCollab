# Step 07 — Move `auto_save` from constructor callback to `EventSink` subscriber

Part of the refactor sequence identified in `docs/repomap/module_map.puml`.
This is **step 7 of 7** (the last): replace the cross-cutting `auto_save`
callback threaded through `Session → Runner` and `Session → Compactor` with
an `AutoSaveSubscriber` that listens to the event bus.

---

## Goal

Today persistence is plumbed via a callback that crosses three module
boundaries:

```
Session._auto_save (method)                    core/session/session.py:190
   ↑ passed as           Session._build_runtime() → ContextCompactor.__init__(auto_save=…)
   ↑ passed as           Session._build_runtime() → SessionRunner.__init__(auto_save=…)
   ↑ called from         Runner._run_compaction()           (after did_compact)
   ↑ called from         Runner._finish_step()              (every step end)
   ↑ called from         Session.add_user_message()         (after appending)
```

Two of those (`Runner._finish_step` and `Runner._run_compaction`) are already
adjacent to an `event_bus.emit(...)` call. The callback is doing exactly what
the event bus is built for — broadcasting a state-change notification to a
listener that doesn't otherwise belong to the runtime's hot path.

Symptoms of the current shape:

- **`auto_save` parameter pollutes two constructors** (`SessionRunner`,
  `ContextCompactor`) that have no other persistence responsibilities.
- **`ContextCompactor.auto_save` is dead code today.** Line 80 reads
  `if apply and self.auto_save: self.auto_save()` — but the only caller
  (`Runner._run_compaction`) passes `apply=False`, so the branch never
  executes. The auto-save in the compaction path goes through `Runner` only.
  We've been carrying a parameter that does nothing.
- **EventBus is single-target.** Sinks and callbacks are mutually exclusive
  (`set_target` clears the other). The TUI uses the slot; an in-process
  AutoSave subscriber can't coexist without fan-out.
- **Mixed lifecycles.** TUI subscription comes in via `event_sink=…`;
  AutoSave subscription comes in via `auto_save_path=…`. Same kind of
  thing — listener on a session lifecycle event — two different wiring
  protocols.

After this PR:

- **`EventBus` becomes multi-subscriber.** `subscribe(target)` appends; the
  bus fans out to every subscriber on each `emit`. Per-subscriber try/except
  preserves the "consumers must not break the loop" guarantee.
- **`AutoSaveSubscriber`** (new file `core/session/autosave.py`) implements
  `EventSink` and persists on three event types: `user_message_appended`,
  `compaction_applied`, `step_end`.
- **Two new events** are emitted by the runtime:
  `user_message_appended` (in `Session.add_user_message`) and
  `compaction_applied` (in `SessionRunner._run_compaction` for runner-owned
  compaction, and in `ContextCompactor.compact(apply=True)` for direct
  compatibility calls; only when compaction actually replaced messages).
- **`auto_save` parameter is removed** from both `SessionRunner.__init__`
  and `ContextCompactor.__init__`. `Session._auto_save()` private helper is
  retained as the subscriber's save function, but no longer crosses runner or
  compactor constructor boundaries.
- **`Session.__init__` keeps `auto_save_path: str | None`** — it's still
  the user-facing knob. Internally it constructs an `AutoSaveSubscriber`
  and subscribes it to the bus alongside the (optional) external sink.

---

## Concrete edits

### 1. `opencollab/core/session/events.py` — multi-subscriber `EventBus`

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass
class SessionEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


EventCallback = Callable[[SessionEvent], Awaitable[None] | None]


class EventSink(Protocol):
    async def emit(self, event: SessionEvent) -> None: ...


class EventBus:
    """Fan-out broadcaster. Multiple subscribers; failures isolated per-sink."""

    def __init__(self, target: EventSink | EventCallback | None = None):
        self._targets: list[EventSink | EventCallback] = []
        if target is not None:
            self.subscribe(target)

    def subscribe(self, target: EventSink | EventCallback) -> None:
        self._targets.append(target)

    @property
    def sink(self) -> EventSink | EventCallback | None:
        """First subscribed target (for back-compat with snapshot/build code)."""
        return self._targets[0] if self._targets else None

    async def emit(self, event: SessionEvent) -> None:
        for target in self._targets:
            try:
                if hasattr(target, "emit"):
                    result = target.emit(event)  # type: ignore[union-attr]
                else:
                    result = target(event)  # type: ignore[operator]
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                # Subscriber failure must not break siblings or the loop.
                continue
```

**Drop** the `on_event` property + setter and the `set_target` method.
In-repo callers:

```bash
grep -rn "set_target\|\.on_event" opencollab/opencollab opencollab/tests
```

Expected: zero. (If a hit appears, migrate it to `subscribe()`.)

**Why per-subscriber `try/except` rather than per-batch:** today a single
sink that throws halts emission. With fan-out, one bad subscriber must not
silence others. The previous `try/except` wrapped the whole emit; the new
shape moves it inside the loop.

### 2. `opencollab/core/session/autosave.py` — new file

```python
"""AutoSaveSubscriber — persists session messages on lifecycle events.

Listens for the three events that change persisted state:
- `user_message_appended` — user added a turn
- `compaction_applied`   — compactor rewrote message history
- `step_end`             — assistant finished a step

Save failures are caught and swallowed (the EventBus already isolates
subscribers, but we belt-and-brace here so a disk-full does not log noise
on every step).
"""

from __future__ import annotations

import logging
from typing import Callable

from opencollab.core.session.events import EventSink, SessionEvent


SAVE_TRIGGERS = frozenset({
    "user_message_appended",
    "compaction_applied",
    "step_end",
})


class AutoSaveSubscriber(EventSink):
    def __init__(self, save_fn: Callable[[], None]):
        self._save = save_fn

    async def emit(self, event: SessionEvent) -> None:
        if event.type not in SAVE_TRIGGERS:
            return
        try:
            self._save()
        except Exception as exc:
            logging.getLogger(__name__).debug("auto-save failed: %s", exc)
```

`save_fn` is a zero-arg closure (`lambda: self.save(self._auto_save_path)`)
so the subscriber stays decoupled from `Session`'s file path semantics.

### 3. `opencollab/core/session/runner.py`

**Constructor — drop `auto_save`:**

```python
# BEFORE
def __init__(
    self, *,
    agent, state, llm, event_bus, tool_processor, compactor,
    tracer=None, max_budget_tokens=200_000, max_steps=100,
    auto_save=None,
):
    ...
    self.auto_save = auto_save

# AFTER
def __init__(
    self, *,
    agent, state, llm, event_bus, tool_processor, compactor,
    tracer=None, max_budget_tokens=200_000, max_steps=100,
):
    ...
    # no self.auto_save
```

**`_run_compaction` — replace callback with event emit:**

```python
# BEFORE
async def _run_compaction(self) -> None:
    result = await self.compactor.compact(apply=False)
    result.apply_to(self.state)
    if result.did_compact and self.auto_save:
        self.auto_save()
    self.state.set_phase(SessionPhase.CALLING_LLM)

# AFTER
async def _run_compaction(self) -> None:
    result = await self.compactor.compact(apply=False)
    result.apply_to(self.state)
    if result.did_compact:
        await self.event_bus.emit(
            SessionEvent(type="compaction_applied",
                         data={"tokens_after": self.state.used_tokens})
        )
    self.state.set_phase(SessionPhase.CALLING_LLM)
```

**`_finish_step` — drop the callback line:**

```python
# BEFORE
async def _finish_step(self, latency: float) -> None:
    await self.event_bus.emit(
        SessionEvent(type="step_end",
                     data={"step": self.state.step_count, "latency": latency})
    )
    if self.auto_save:
        self.auto_save()

# AFTER
async def _finish_step(self, latency: float) -> None:
    await self.event_bus.emit(
        SessionEvent(type="step_end",
                     data={"step": self.state.step_count, "latency": latency})
    )
    # AutoSaveSubscriber listens for step_end on the bus.
```

### 4. `opencollab/core/session/compactor.py`

**Drop the dead `auto_save` parameter and field** (lines 39, 46, 80–81).

```python
# BEFORE
def __init__(
    self, *,
    state, llm, event_bus, tracer=None,
    compaction_threshold=DEFAULT_COMPACTION_THRESHOLD,
    auto_save=None,
):
    ...
    self.auto_save = auto_save

# AFTER
def __init__(
    self, *,
    state, llm, event_bus, tracer=None,
    compaction_threshold=DEFAULT_COMPACTION_THRESHOLD,
):
    ...
    # no self.auto_save
```

In `compact()`, delete:

```python
if apply and self.auto_save:
    self.auto_save()
```

(This branch was never reachable — `apply=False` everywhere — so behavior is
unchanged.)

### 5. `opencollab/core/session/session.py`

**Imports:**

```python
from opencollab.core.session.autosave import AutoSaveSubscriber
from opencollab.core.session.events import EventBus, EventSink, SessionEvent  # add SessionEvent
```

**`__init__` — keep the public `auto_save_path` param, wire it via subscribe:**

```python
# REPLACE the auto_save plumbing portion of __init__:
self.event_bus = EventBus(event_sink)
if auto_save_path:
    self.event_bus.subscribe(AutoSaveSubscriber(self._auto_save))
self._permission_policy = permission_policy
self._auto_save_path = auto_save_path
```

**`_build_runtime` — drop `auto_save=` arguments to Compactor and Runner:**

```python
def _build_runtime(self) -> None:
    self.tool_processor = ToolCallProcessor(
        agent=self.agent, env=self.env, state=self.state,
        event_bus=self.event_bus, tracer=self.tracer,
        permission_policy=self.permission_policy,
    )
    self.compactor = ContextCompactor(
        state=self.state, llm=self._llm, event_bus=self.event_bus,
        tracer=self.tracer, compaction_threshold=self.compaction_threshold,
    )
    self.runner = SessionRunner(
        agent=self.agent, state=self.state, llm=self._llm,
        event_bus=self.event_bus,
        tool_processor=self.tool_processor, compactor=self.compactor,
        tracer=self.tracer,
        max_budget_tokens=self.max_budget_tokens,
        max_steps=self.max_steps,
    )
```

**`add_user_message` — emit an event instead of calling the callback:**

```python
# BEFORE
async def add_user_message(self, content: str) -> None:
    self.state.append_message({"role": "user", "content": content})
    self.state.reset_for_user_turn()
    self._auto_save()

# AFTER
async def add_user_message(self, content: str) -> None:
    self.state.append_message({"role": "user", "content": content})
    self.state.reset_for_user_turn()
    await self.event_bus.emit(SessionEvent(type="user_message_appended"))
```

**`_auto_save` — keep the method**, since `AutoSaveSubscriber` holds a
reference to it as `save_fn`. Move it next to `save()`:

```python
def save(self, path: str) -> None:
    self.store.save(path, self.messages)

def _auto_save(self) -> None:
    if self._auto_save_path:
        self.save(self._auto_save_path)
```

(The public-vs-private boundary doesn't change. We're only removing the
**callback plumbing through Runner/Compactor**, not the helper itself.)

**`snapshot()` — `EventBus.sink` may now be a callback; pass it through:**

`self.event_bus.sink` already returns the first subscriber. `Session.__init__`
accepts `event_sink: EventSink | None`. If the first subscriber is the
AutoSaveSubscriber (because no external sink was passed), `snapshot()` would
re-wire that to the new session — which is wrong. Fix:

```python
def snapshot(self) -> "Session":
    external_sink: EventSink | None = None
    for target in self.event_bus._targets:
        if not isinstance(target, AutoSaveSubscriber):
            external_sink = target  # type: ignore[assignment]
            break
    new = Session(
        agent=self.agent, env=self.env, tracer=self.tracer,
        max_budget_tokens=self.max_budget_tokens,
        max_steps=self.max_steps,
        compaction_threshold=self.compaction_threshold,
        event_sink=external_sink,
        permission_policy=self.permission_policy,
    )
    new.messages = copy.deepcopy(self.messages)
    new.used_tokens = self.used_tokens
    new.step_count = self.step_count
    return new
```

Reaching into `_targets` is the lesser evil — the alternative is a public
`EventBus.iter_external_sinks()` method whose only caller is `snapshot()`.
Add a brief comment noting the leak.

Alternative considered: expose `EventBus.subscribers` as a tuple property
and filter in `snapshot()`. Equivalent ergonomics, slightly cleaner. **Pick
whichever the worker prefers** — both fine.

### 6. Tests

#### `opencollab/tests/test_session_characterization.py`

The `test_session_auto_save_path_is_public` test (line 180) asserts the
`auto_save_path` property reads back what was passed. Still passes — that
property is unchanged.

The `test_save_and_load_round_trip_only_messages` test creates `Session(...,
auto_save_path=...)` and exercises save. Should still pass.

Search the test file for `_auto_save\b` and ensure there are no callers of
the private method. (There shouldn't be — it was always private.)

#### New file `opencollab/tests/test_autosave_subscriber.py`

```python
"""Unit tests for AutoSaveSubscriber and Session→event_bus wiring."""

from __future__ import annotations

import asyncio

import pytest

from opencollab.core.session import Session
from opencollab.core.session.autosave import AutoSaveSubscriber, SAVE_TRIGGERS
from opencollab.core.session.events import EventBus, SessionEvent


# Pull FakeAgent/FakeLLMClient out of test_session_characterization to avoid
# duplicating the fakes here. Import path matches the existing test file's
# definitions.
from tests.test_session_characterization import FakeAgent, FakeLLMClient


@pytest.mark.parametrize("trigger", sorted(SAVE_TRIGGERS))
def test_autosave_subscriber_fires_on_each_trigger(trigger):
    calls = []
    sub = AutoSaveSubscriber(lambda: calls.append("saved"))

    asyncio.run(sub.emit(SessionEvent(type=trigger)))
    assert calls == ["saved"]


def test_autosave_subscriber_ignores_other_events():
    calls = []
    sub = AutoSaveSubscriber(lambda: calls.append("saved"))
    asyncio.run(sub.emit(SessionEvent(type="text_delta", data={"content": "x"})))
    asyncio.run(sub.emit(SessionEvent(type="tool_start", data={"tool": "bash"})))
    assert calls == []


def test_autosave_subscriber_swallows_save_errors(caplog):
    def boom():
        raise OSError("disk full")
    sub = AutoSaveSubscriber(boom)
    # Must not raise.
    asyncio.run(sub.emit(SessionEvent(type="step_end")))


def test_event_bus_fans_out_to_multiple_subscribers():
    bus = EventBus()
    a, b = [], []
    bus.subscribe(lambda evt: a.append(evt.type))
    bus.subscribe(lambda evt: b.append(evt.type))
    asyncio.run(bus.emit(SessionEvent(type="ping")))
    assert a == ["ping"] and b == ["ping"]


def test_event_bus_failure_in_one_subscriber_does_not_silence_others():
    bus = EventBus()
    seen = []

    def bad(evt):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(lambda evt: seen.append(evt.type))
    asyncio.run(bus.emit(SessionEvent(type="ping")))
    assert seen == ["ping"]


def test_session_with_auto_save_path_writes_on_user_message(tmp_path):
    path = tmp_path / "auto.jsonl"
    session = Session(
        agent=FakeAgent(), llm=FakeLLMClient(),
        auto_save_path=str(path),
    )
    asyncio.run(session.add_user_message("hello"))
    assert path.exists()
    contents = path.read_text()
    assert "hello" in contents


def test_session_without_auto_save_path_does_not_subscribe(tmp_path):
    """No path → no AutoSaveSubscriber → no file written."""
    session = Session(agent=FakeAgent(), llm=FakeLLMClient())
    asyncio.run(session.add_user_message("hello"))
    # Nothing to assert beyond "didn't crash" — just confirm no subscriber.
    from opencollab.core.session.autosave import AutoSaveSubscriber
    assert not any(
        isinstance(t, AutoSaveSubscriber) for t in session.event_bus._targets
    )


def test_session_external_sink_and_autosave_coexist(tmp_path):
    path = tmp_path / "auto.jsonl"
    received: list[str] = []

    class CapturingSink:
        async def emit(self, event):
            received.append(event.type)

    session = Session(
        agent=FakeAgent(), llm=FakeLLMClient(),
        auto_save_path=str(path),
        event_sink=CapturingSink(),
    )
    asyncio.run(session.add_user_message("hi"))
    assert path.exists()
    assert "user_message_appended" in received
```

(Adapt `FakeAgent`/`FakeLLMClient` imports if the test layout differs. The
existing characterization file defines them at module scope.)

### 7. `opencollab/bootstrap/session_factory.py` — unchanged

Still passes `auto_save_path=…` into `Session(...)`. The public knob is the
same. No edits required.

---

## Verification

```bash
# auto_save parameter gone from runner and compactor.
grep -n "auto_save" opencollab/opencollab/core/session/runner.py opencollab/opencollab/core/session/compactor.py
# Expected: zero matches.

# AutoSaveSubscriber is the only owner of the save-on-event policy.
grep -rn "auto_save" opencollab/opencollab/core/session/
# Expected: only autosave.py, the auto_save_path field/property in session.py,
# and the _auto_save method in session.py.

# New events are emitted exactly where described.
grep -n "user_message_appended\|compaction_applied" opencollab/opencollab/core/session/
# Expected: session.py:add_user_message emits user_message_appended;
# runner.py:_run_compaction and compactor.py:compact(apply=True)
# emit compaction_applied.

# EventBus is multi-subscriber.
grep -n "set_target\|on_event" opencollab/opencollab opencollab/tests
# Expected: zero matches (both APIs removed).

# Public surface still exports the same names.
python -c "from opencollab.core.session import EventBus, EventSink, SessionEvent; print('ok')"
```

---

## Acceptance checklist

- [ ] `EventBus` has `subscribe(target)`; `set_target` and `on_event`
      removed.
- [ ] `EventBus.emit` fans out to every subscriber with per-subscriber
      `try/except`.
- [ ] `core/session/autosave.py` exists with `AutoSaveSubscriber` and
      `SAVE_TRIGGERS = {"user_message_appended", "compaction_applied",
      "step_end"}`.
- [ ] `SessionRunner.__init__` no longer accepts `auto_save`.
- [ ] `ContextCompactor.__init__` no longer accepts `auto_save`.
- [ ] `Session.add_user_message` emits a `user_message_appended` event
      instead of calling `_auto_save()` directly.
- [ ] `SessionRunner._run_compaction` emits a `compaction_applied` event
      when `result.did_compact`.
- [ ] `ContextCompactor.compact(apply=True)` also emits
      `compaction_applied` after direct compatibility-path compaction.
- [ ] `SessionRunner._finish_step` no longer calls `self.auto_save()`;
      relies on the existing `step_end` emit.
- [ ] `Session._auto_save()` private method still exists (it is the
      `save_fn` for the subscriber).
- [ ] `Session.snapshot()` does not propagate the internal
      `AutoSaveSubscriber` to the new Session (it constructs its own from
      its `auto_save_path`, which `snapshot()` does not copy — intentional).
- [ ] `tests/test_autosave_subscriber.py` added.
- [ ] All existing tests pass.
      `cd opencollab && OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q`
      → **57 passing** after the direct-compaction autosave regression test.
- [ ] Smoke test: `python -m opencollab chat`, type a message, verify
      `.opencollab/sessions/<id>.jsonl` exists and grows. Trigger a manual
      `/save` — works. Kill, restart with `--session .opencollab/sessions/<id>.jsonl`
      — restores.

---

## What is NOT in this PR

- **Tracer-as-subscriber.** `Tracer.log_step(...)` is called directly from
  runner/compactor today. Converting it to an EventSink is a natural next
  step but doubles the diff and isn't required to reach the goal of step 7.
  Defer.
- **Removing `Session._auto_save_path` field.** The path is still stored on
  the Session because `auto_save_path` is a public property the CLI prints
  ("Session auto-saving to …"). Removing the field would force the CLI to
  thread the path itself; not worth it.
- **Per-event subscriber filtering at the EventBus.** Today every subscriber
  sees every event and decides itself. That's the right shape for two
  subscribers (TUI + AutoSave); revisit if a third arrives.
- **Renaming events.** `step_end`/`tool_start`/etc. keep their current
  names. Out of scope.

---

## Risk & rollback

- **Behavior change: AutoSave errors are now logged and swallowed.** Before
  step 7, a disk-full or permission error during auto-save would propagate
  up out of `Session.add_user_message` (or the runner step), aborting the
  loop. After step 7, `AutoSaveSubscriber.emit` catches and debug-logs.
  This is the right shape — auto-save is a best-effort durability hint, not
  a synchronous commit — but flag it in the PR description for reviewers.
- **Behavior change: AutoSave now also runs after a compaction.** Before,
  the runner explicitly called `self.auto_save()` only when
  `result.did_compact`. After, the new `compaction_applied` event is
  emitted under the same condition, so the trigger is unchanged. Confirm in
  review that the if-guard moved correctly.
- **EventBus API removal.** `set_target` / `on_event` are gone. In-repo
  callers: none after migration. Out-of-repo: anyone subclassing or
  reflecting on EventBus breaks. We're pre-1.0; acceptable.
- **`snapshot()` no longer carries AutoSave.** Snapshots are used by tests
  and by hypothetical replay tooling, neither of which should re-trigger
  writes to the original session's auto-save path. Intentional.
- **`_targets` is a private attribute on EventBus.** `snapshot()` reads it
  to distinguish the internal AutoSaveSubscriber from external sinks. If
  this offends, expose `EventBus.subscribers` as a tuple property; the
  worker may choose either.
- Rollback: single `git revert`.

Human-readable summary for the PR description: *"Replace the `auto_save`
callback threaded through `SessionRunner` and `ContextCompactor` with an
`AutoSaveSubscriber` on the event bus. `EventBus` is now multi-subscriber.
Adds `user_message_appended` and `compaction_applied` events. Net: two
constructor parameters removed, one private method demoted to an event
sink, persistence wired the same way as the TUI."*
