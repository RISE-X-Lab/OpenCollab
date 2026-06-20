# The OpenCollab Budget System

Reference for how OpenCollab meters, divides, and enforces a **token budget**
across an agent session and a team of spawned agents, and how that budget
relates to (but is distinct from) the model **context window**.

Status: current as of `main` @ `58a29a8` (2026-06-18). Citations are by file +
symbol so they survive line drift; paths are relative to the repo root.

---

## 1. What "budget" is

The budget is a cap on **tokens spent**, not on wall-clock, dollars, or API
calls. The unit is *real provider tokens* — `input_tokens + output_tokens` as
reported by the API on each call — accumulated for the life of a session.

It answers one question: *"How much model work may this session (and its team)
consume before it must stop?"* It is a cost/effort ceiling, deliberately
graceful (see §5), not a hard kill switch.

Two distinct counters live on `SessionState`
(`opencollab/opencollab/domain/session.py`):

| Field | Meaning | Used for |
|-------|---------|----------|
| `used_tokens` | **Cumulative** tokens spent across every call so far. | The budget meter. |
| `context_tokens` | The **current** prompt size (last call's input tokens). | Observability; tracing. |

Keeping these separate is intentional: `used_tokens` is *spend over time*,
`context_tokens` is *size right now*. They are not interchangeable.

> **Why `used_tokens` grows super-linearly is correct, not a bug.** Every API
> call is billed for the *entire* input context (all prior turns) plus the new
> output. A 20-step session re-sends its growing history 20 times, and the
> provider charges for all of it. Summing `total_tokens` per call therefore
> measures *true billed spend*. The practical consequence: a large-context
> agent burns budget fast because most of each call is re-transmitted history —
> that reflects real cost, by design.

---

## 2. Accounting — where tokens are counted

There is exactly **one** increment site:
`SessionRunUseCase.run_llm_call` (`opencollab/opencollab/application/session_run.py`)
calls `self.state.add_used_tokens(response.usage.total_tokens)` once per LLM
call, and `self.state.set_context_tokens(response.usage.input_tokens)`.

`Usage.total_tokens = input_tokens + output_tokens`
(`opencollab/opencollab/adapters/llm/types.py`). The tokens come from the real
provider `usage` object, not an estimate — except as a defensive fallback (§6).

Scope is **per session**: each `SessionState` has its own `used_tokens`. The
scheduler also exposes a team **aggregate** (`Scheduler.used_tokens` →
`SessionTable.total_used_tokens`, `opencollab/opencollab/domain/scheduler.py`),
which is what the aggregate ceiling (§5) enforces.

---

## 3. The limit — defaults and override chain

1. **Config default `200_000`** — `Settings.budget`
   (`opencollab/opencollab/bootstrap/config.py`), env var `OPENCOLLAB_BUDGET`
   (default `"200000"`).
2. **CLI `--budget`** (`opencollab/opencollab/adapters/cli/main.py`) — default
   `None`.
3. **The interactive/team bump:** when `--budget` is *not* passed, agent 0 can
   spawn a whole team, so the CLI raises the effective budget:
   `cfg["budget"] = max(cfg["budget"], 500_000)`. **So the effective default for
   an interactive/team run is 500k, not the 200k in config.** An explicit
   `--budget N` is respected verbatim.
4. The resolved value is handed to the scheduler as `max_budget_tokens`
   (`opencollab/opencollab/bootstrap/scheduler_factory.py`).

Defaults differ by entry point (only the resolved value at the composition root
matters in practice): `Scheduler` defaults to `500_000`, `SessionRunUseCase`
to `200_000`. The workflow engine uses its own model: unbounded sessions get
`UNBOUNDED_SESSION_BUDGET = 1_000_000` and the pool is genuinely shared, raising
`WorkflowBudgetExceeded` at the run boundary
(`opencollab/opencollab/application/workflow.py`).

---

## 4. Dividing the budget across a team — reserve-at-allocation

When the Lead (aid 0) spawns children they do **not** share one live pool;
each child gets its own per-session cap, carved out so the **sum of all grants
can never exceed the global pool**. This is the *reserve-at-allocation* model.

Two pure domain functions (`opencollab/opencollab/domain/scheduler.py`):

```python
def lead_reserve(total):       # headroom the Lead keeps for its own turns
    return max(10_000, total // 4)

def split_budget(total, allocated):   # what the next child gets
    return max(10_000, total - allocated)   # the unallocated remainder, floored
```

The scheduler (`opencollab/opencollab/application/scheduler.py`) tracks a running
`_allocated_tokens`:

- At Lead registration it is **seeded with `lead_reserve(total)`**, so the first
  child is granted `total - total//4` (e.g. 375k of a 500k pool).
- `_reserve_child_budget(aid)` grants `split_budget(total, _allocated_tokens)`
  and books it **synchronously, before the first `await`** in `spawn`
  (`opencollab/opencollab/application/scheduler_lifecycle.py`) — so a *batched or
  concurrent* set of spawns each see the *updated* allocation and cannot all
  draw against an empty pool.
- `_release_child_budget(aid)` reclaims a child's grant when it reaches a
  terminal phase (idempotent; pops then clamps at 0), so an early-finishing
  child's headroom is returned to the pool for later spawns.

The Lead itself holds the full `max_budget_tokens` as its own per-session cap
(it is the parent pool). Because grants are floored at `10_000`, the *sum* of
grants can exceed `total` only in the exhausted tail, where each starving child
still gets the 10k minimum; above the floor the running sum never exceeds the
pool.

> **History:** before 2026-06-18, `split_budget` divided against live *spend*,
> so concurrent spawns all saw `used≈0` and each received ~75% of the pool —
> the global budget was not a real ceiling under fan-out (3 children × 375k vs a
> 500k cap). The reserve-at-allocation model fixed this (commit `158820b`).

---

## 5. Enforcement — graceful, never a hard cut

Enforcement lives in `SessionRunUseCase.precheck`
(`opencollab/opencollab/application/session_run.py`), which runs **between
steps**, at a clean boundary — *after* the previous step fully finished and
*before* the next LLM call. Two budget guards fire there:

1. **Per-session cap** — `used_tokens >= max_budget_tokens`.
2. **Aggregate team ceiling** (defense-in-depth) — `team_budget_exhausted()`,
   an injected callable derived from `Scheduler.budget_exhausted`
   (`aggregate used >= global cap`, exposed via `SchedulerPort.budget_exhausted`
   in `opencollab/opencollab/application/ports.py`). This stops a session even
   when it is under its own cap, so fan-out can't collectively blow the pool.

On either, the guard **appends a visible system message**, emits a
`budget_exceeded` event, and transitions to the terminal phase
`SessionPhase.BUDGET_EXCEEDED`. This is the same shape as the `max_steps` guard
(`STEP_LIMIT_EXCEEDED`).

**It is a graceful stop, not an abrupt kill:**

- The check is *before* a call, so any in-flight call / tool execution always
  finishes; nothing is abandoned mid-step.
- Prior steps are already persisted (autosave on `step_end`); the transcript
  stays intact; `run_loop` returns the last assistant text.
- **A spawned child that hits the ceiling reaches a terminal phase, not
  `ERROR`.** In `scheduler_lifecycle._drive_agent` the child→parent delivery
  maps `status = FAILED if phase is ERROR else DONE`, so a budget-stopped child
  delivers a **`DONE` row carrying its partial result** and *re-activates its
  parent*. No silent death, no stranded/deadlocked parent.
- The Lead hitting the ceiling shows the user a clean
  `[Budget exceeded. Session stopped.]` message — no exception.

**Known boundary behavior — the one-call overshoot:** because the guard checks
*before* the call and tokens are added *after*, a session sitting just under its
cap can issue one more full call and finish a few-k tokens *over* the cap. This
is inherent to check-before-call and is backstopped by the aggregate ceiling; it
is bounded, not unbounded.

---

## 6. Token-accounting fidelity

The meter is only as honest as the `usage` it reads. Two provider-level fixes
keep it accurate (`opencollab/opencollab/adapters/llm/`):

- **Anthropic prompt-cache tokens** (`anthropic_provider.py::_parse_usage`).
  When caching is active the API splits input into `input_tokens` (uncached),
  `cache_read_input_tokens`, and `cache_creation_input_tokens`; the first
  *excludes* the cached parts. `_parse_usage` folds all three into the accounted
  input (read defensively via `getattr(..., 0) or 0`; the cache sub-fields are
  retained on `Usage` purely for tracing). Prompt caching is **not enabled in
  the repo today**, so this is correct-by-construction for the moment it is.
- **Missing/zero OpenAI `usage`** (`openai_provider.py::_parse_usage`). Some
  OpenAI-compatible endpoints (proxies, some streaming configs, vLLM/Ollama)
  omit the `usage` block; left untreated the call would add **0** to the meter
  and the budget would never trip. When counts are missing or zero, it falls
  back to a non-zero estimate (`estimate_messages_tokens` for input, response
  text + tool-call args for output) and flags `Usage.estimated = True`. OpenAI
  `prompt_tokens` already includes cached tokens, so **no** cache field is added
  on this side (avoids double-counting).

The `len // 3` char estimator (`adapters/llm/types.py`) is used for the
fallback above and for context-shaping triggers (§7) — it is **not** used for
normal budget accounting, which always uses real `usage`.

---

## 7. Budget vs. the context window — two different limits

These are easy to conflate but are orthogonal:

- **Budget** caps cumulative *spend over the whole session* (§1–5).
- **Context window** caps the *size of a single prompt* the model will accept.

A session can be well under budget yet still build a prompt too large for the
window. That is handled by a separate machinery.

### 7a. Reactive compaction (proactive)

History is trimmed before each call by a reactive compaction chain
(`opencollab/opencollab/application/shaping/`). It no-ops until an estimate
crosses a trigger, then degrades progressively. The trigger is window-derived
(`history_trigger_target`, `shaping/pipeline.py`):

```
trigger = context_window - output_reserve(20k) - buffer(13k)
target  = 0.75 × trigger          # compact down to here, to avoid thrashing
```

(falling back to fixed `120_000 / 90_000` if the model's window is unknown).
The layers, cheapest first: per-tool-result budget → shed low-priority context
sources → clear old tool output → snip whole old tool-exchange turns →
auto-compact the non-pinned span to a summary → identity-stub collapse. Every
step is a read-time projection over a **copy** — the persisted transcript is
never mutated. Sources at/above `PIN_FLOOR = 70` (identity / team / task) are
**pinned** and never folded.

The trigger is driven by the **char estimate**, which can *under*-count on dense
content (code, JSON, CJK) and let a real prompt slip over the window. That is
the gap the safety net below closes. (Calibrating the trigger with the real
`input_tokens` was considered and deferred — it would require a stateful shaper
across the clean-arch boundary; the safety net prevents the crash regardless.)

### 7b. Context-overflow safety net (reactive)

If a prompt still exceeds the real window, the provider raises an HTTP 400. A
three-layer net (commit `58a29a8`) prevents that from crashing the session:

1. **Classifier** — `is_context_overflow_error` (`adapters/llm/errors.py`): a
   *conservative* match requiring a 400 **and** a context-length signal
   (message fragment or error `code`), so an unrelated 400 (bad schema, bad
   model id) is never misread. `retry.py` keeps 400 out of the transient-retry
   set, so overflow is never futilely retried in backoff.
2. **Recompact-and-retry-once** — on an overflow rejection,
   `SessionRunUseCase.call_llm` runs `forced_shape` (a *maximal* compaction pass
   that compacts toward target unconditionally, regardless of the estimate —
   pinned sources still never folded) and retries the call **once**, emitting a
   `context_overflow_recompacted` event.
3. **Graceful stop** — if the retry still overflows, the session transitions to
   the terminal phase `SessionPhase.CONTEXT_OVERFLOW` (mirrors
   `BUDGET_EXCEEDED`): a visible message, a `context_overflow` event, and —
   because only `ERROR` maps to a FAILED row — an overflowed **child delivers a
   controlled `DONE` row and re-activates its parent**, rather than crashing it.

---

## 8. The safeguard stack

| Safeguard | Where | Protects against |
|-----------|-------|------------------|
| Per-session budget cap | `session_run.py::precheck` | Unbounded session spend |
| Aggregate team ceiling | `session_run.py::precheck` + `scheduler.py::budget_exhausted` | Fan-out collectively blowing the pool |
| Reserve-at-allocation | `scheduler.py` + `domain/scheduler.py::split_budget` | Sub-budgets oversubscribing the global cap |
| Reservation release on spawn failure | `scheduler_lifecycle.py::spawn` | Leaked budget/inflight when a spawn build fails |
| Step limit (`max_steps`) | `session_run.py::precheck` | Infinite-ish tool loops even if metering breaks |
| Reactive compaction | `application/shaping/` | Prompts growing past the window |
| Context-overflow net | `session_run.py::call_llm` + `adapters/llm/errors.py` | A surviving overflow crashing the session |
| LLM retry (transient only) | `adapters/llm/retry.py` | 408/409/429/5xx, rate-limit, overloaded |
| Missing-usage estimate fallback | `openai_provider.py::_parse_usage` | A 0-token call silently never tripping the budget |
| Workflow shared-pool boundary | `application/workflow.py` | A workflow fleet exceeding its shared budget |

---

## 9. Known limitations / residual gaps

- **One-call / one-agent overshoot.** Check-before-call lets a single turn (or a
  single workflow agent) finish slightly over its cap. Bounded; backstopped by
  the aggregate ceiling.
- **Budget is cumulative billed tokens.** Long-context agents exhaust budget
  quickly because re-transmitted history dominates. This is *correct* as a cost
  meter; size it accordingly.
- **Compaction trigger uses a char estimate**, not real tokens, so it can
  under-protect on dense content. Mitigated (not eliminated) by the overflow
  safety net; trigger calibration is a deferred follow-up.
- **An oversized *pinned* seed.** If the pinned set (task / identity / team,
  ≥ `PIN_FLOOR`) *alone* exceeds the window, compaction cannot shed it, so the
  session lands in the graceful `CONTEXT_OVERFLOW` stop: no crash, but no
  progress. The follow-up is to cap/truncate the TASK-layer source at spawn.
- **Cache weighting.** When prompt caching is eventually enabled, cache tokens
  are counted at *full token weight*. This is a **token** budget, not a
  dollar-weighted one (Anthropic bills cache reads at ~0.1×); converting to a
  cost budget would be a separate policy decision.

---

## 10. Quick file reference

| Concern | File |
|---------|------|
| Meter + counters | `opencollab/opencollab/domain/session.py` (`used_tokens`, `context_tokens`) |
| Accumulate + enforce | `opencollab/opencollab/application/session_run.py` (`run_llm_call`, `precheck`, `call_llm`) |
| Token shape | `opencollab/opencollab/adapters/llm/types.py` (`Usage`) |
| Provider usage parsing | `opencollab/opencollab/adapters/llm/{anthropic,openai}_provider.py` (`_parse_usage`) |
| Overflow classifier / retry | `opencollab/opencollab/adapters/llm/{errors,retry}.py` |
| Budget division | `opencollab/opencollab/domain/scheduler.py` (`split_budget`, `lead_reserve`) |
| Allocation bookkeeping | `opencollab/opencollab/application/{scheduler,scheduler_lifecycle}.py` |
| Aggregate ceiling port | `opencollab/opencollab/application/ports.py` (`SchedulerPort.budget_exhausted`) |
| Limit resolution | `opencollab/opencollab/bootstrap/config.py`, `opencollab/opencollab/adapters/cli/main.py` |
| Compaction / window | `opencollab/opencollab/application/shaping/{pipeline,reactive}.py` |
| Workflow budget | `opencollab/opencollab/application/workflow.py` |
