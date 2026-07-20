# Report Material — The Session Lifecycle Before/After (Lane S1)

> **Purpose.** The evidence pack for the technical report's §1 *(The Session as a validated FSM)* and §5 *(Enforcement — the eval-integrity core)*. It is the concrete **before/after** the pattern catalog forecast as *"the headline before/after"* (`pattern-catalog.md` §7 S4) and *"the single cleanest before/after for the clean-arch thesis"* (§3.1). Every claim carries a `commit` and a `file:line` anchor so the report is assembled from the tree, not written alongside it.
>
> **Scope.** Slimming **Lane S1** (branch `refactor/slim-s1-fsm`, 9 commits `5c7525b..0e0a286`, baseline `1205dc3`). Behavior-preserving on every core invariant; the only intended behavior change is that four graceful-stop terminals now carry an *honest* reason instead of a mislabelled enum. **Not pushed, not merged** as of this writing.
>
> **Through-line.** This lane is the tightest demonstration of OC's ethic — **defend states, not races**. Its three moves all say the same thing: *make the state that must be defended a first-class, named, snapshot-as-a-unit structure, and delete the machinery that only guessed at races.*
>
> **Self-contained.** §2 captures the **complete current topology** — the full edge table, the `advance` dispatch, the `precheck` guard chain, and the three mutation primitives — so the report's §1 can be written from this doc alone, without reopening `session.py`. The before/after deltas (§2.1, §3, §4) sit on top of that reference.
>
> Verified green at the lane tip (`0e0a286`, measured this session): `1370 passed` · `ruff: All checks passed!` · architecture fitness `8 passed`.

---

## 1. The one-paragraph thesis

The pre-slim `Session` FSM defended its lifecycle with **breadth** — one enum member per way a turn could end (six terminals), one flat field per enforcement signal (~fourteen, smeared onto the domain value object), and two feed-forward heuristics (a predictive EWMA guard and an extension-valve negotiation) that tried to *win the race* against the budget. Lane S1 replaces breadth with **structure**: six terminals collapse to **three behaviors** (`DONE` / `STOPPED(reason)` / `ERROR`), the per-turn enforcement signals move into **one snapshot-as-a-unit value object** (`TurnEnforcementState`, shaped like a kernel PCB's saved-register set), and the two race-guessing heuristics are **deleted** so wind-down has one actuator fired by three honest triggers. The result reads the same to a self-regulating run (byte-for-byte, proven by test) but is **−476 lines of production code** and states its own contract in types instead of prose.

---

## 2. FSM topology — 14 → 10 states, 6 → 3 terminals

**Commit** `c1108c1` (`refactor(slim): collapse FSM to 10 states / 3 terminals`). **Anchor** `domain/session.py:18-46,300-376`.

| | Before (`c1108c1^`) | After (`c1108c1`) |
|--|--|--|
| **States** | 14 | **10** |
| **Terminals** | 6 | **3** |
| **Non-terminal live states** | IDLE, PRECHECK, CALLING_LLM, HANDLING_RESPONSE, EXECUTING_TOOLS, AWAITING_EVENTS, AUTOSAVING **+ SCHEDULED** | IDLE, PRECHECK, CALLING_LLM, HANDLING_RESPONSE, EXECUTING_TOOLS, AWAITING_EVENTS, AUTOSAVING |
| **Terminals** | DONE, ERROR, **CANCELLED, BUDGET_EXCEEDED, STEP_LIMIT_EXCEEDED, CONTEXT_OVERFLOW** | DONE, ERROR, **STOPPED** |

Two independent simplifications:

1. **`SCHEDULED` deleted.** A pre-run transitional state with no behavior of its own; a spawned session goes straight to `IDLE`. (Migration note kept as a comment at `application/session.py:414` so old persisted snapshots carrying `"scheduled"` still load.)
2. **Four graceful terminals → one `STOPPED(reason)`.** `CANCELLED`, `BUDGET_EXCEEDED`, `STEP_LIMIT_EXCEEDED`, `CONTEXT_OVERFLOW` were four enum members for what is behaviorally **one** outcome — *"the turn stopped cleanly and delivered a result to its parent."* They collapse to a single `STOPPED` terminal whose cause lives in a human-readable `terminal_reason` string, not in a parallel set of enum labels (`domain/session.py:52-55,199,315-316`).

### 2.1 The honesty fix (catalog §8 seam #1, resolved)

The catalog flagged a real defect: **two terminal labels lied.** A loop-block halt was labeled `STEP_LIMIT_EXCEEDED`; a *successful* wind-down commit was labeled `BUDGET_EXCEEDED`. Because the enum member *was* the reason, the only way to report "stopped by the loop detector" was to borrow an unrelated label.

`STOPPED(reason)` fixes this **as a side effect of the collapse** — the reason is now a free-text string set at the transition, so each stop tells the truth:

| Trigger | Before (lying enum) | After (`terminal_reason`, `session_run.py`) |
|--|--|--|
| Budget spent past cap | `BUDGET_EXCEEDED` | `"budget exceeded: {n} tokens used"` (`:582`) |
| Team aggregate ceiling | *(none / reused BUDGET)* | `"team budget exceeded: aggregate spend reached the global cap"` (`:590`) |
| Step cap | `STEP_LIMIT_EXCEEDED` | `"step limit reached: {n} steps"` (`:595`) |
| Loop block | **`STEP_LIMIT_EXCEEDED`** (lie) | `"loop block limit reached: N repeated tool calls"` (`:571`, via `_stop_precheck`→`:507`) |
| Context overflow | `CONTEXT_OVERFLOW` | `"context overflow: prompt exceeds the model context window even after compaction"` (`:1124`) |
| Wind-down success | **`BUDGET_EXCEEDED`** (lie) | honest wind-down reason (`:507`) |

**Report line:** *collapsing the terminal taxonomy did not just remove four enum members — it removed the structural pressure that forced two of them to lie.*

### 2.2 The complete transition table (the topology, in full)

`PHASE_TRANSITIONS` is the single source of truth for legal edges; `transition_to` validates every run-loop move against it (`domain/session.py:71-93`). All ten phases and their legal successors — this is the whole graph, no source re-read needed:

| From | → legal successors | Meaning of the branch |
|--|--|--|
| `IDLE` | `PRECHECK` | rest-between-runs; a new user turn starts the step |
| `PRECHECK` | `CALLING_LLM` \| `STOPPED` | the per-step guard chain (§2.4): pass → call, halt → stop |
| `CALLING_LLM` | `HANDLING_RESPONSE` \| `STOPPED` | LLM answered, or provider rejected an over-window prompt (context overflow) |
| `HANDLING_RESPONSE` | `EXECUTING_TOOLS` \| `DONE` \| `AUTOSAVING` | tool calls to run / a final answer / an empty turn (autosave + retry) |
| `EXECUTING_TOOLS` | `AUTOSAVING` \| `AWAITING_EVENTS` | all tools ran inline / at least one deferred (spawn) → suspend |
| `AWAITING_EVENTS` | `PRECHECK` | children answered; drain the pending table and resume the step |
| `AUTOSAVING` | `PRECHECK` | snapshot persisted; loop to the next step |
| `DONE` | `IDLE` | terminal; `resume_to_idle` for a fresh turn / re-run |
| `STOPPED` | `IDLE` | terminal; `resume_to_idle` for a fresh turn / re-run |
| `ERROR` | `IDLE` | terminal; `resume_to_idle` for a fresh turn / re-run |

The table makes the **two-level loop** explicit — the report's §1 backbone:

```
run   (one user message):  IDLE ─▶ PRECHECK ─▶ … ─▶ terminal ─▶ IDLE
step  (one LLM call):       PRECHECK ─▶ CALLING_LLM ─▶ HANDLING_RESPONSE ─▶ EXECUTING_TOOLS ─▶ AUTOSAVING ─▶ PRECHECK
final answer:               HANDLING_RESPONSE ─▶ DONE
empty turn (retry):         HANDLING_RESPONSE ─▶ AUTOSAVING ─▶ PRECHECK
suspend on children:        EXECUTING_TOOLS ─▶ AWAITING_EVENTS  ⇢(all pending filled)⇢  PRECHECK
in-loop halt:               PRECHECK ─▶ STOPPED(reason)   |   CALLING_LLM ─▶ STOPPED(context overflow)
out-of-band escape:         any ─▶ ERROR (fail)   |   any ─▶ STOPPED (cancel)
```

`AWAITING_EVENTS` is the only non-terminal **suspend** state: the loop returns the task there and the scheduler re-activates the session at `PRECHECK` once every pending row is filled (delegation/spawn results arrive as this tool call's answer — the FSM needs no new state for a child).

### 2.3 The run loop: dispatch + suspend/resume

`run_loop` (`session_run.py:286-306`) is `_prepare_turn()` then `while not _should_suspend(): await advance()`. `_should_suspend` = terminal **OR** `AWAITING_EVENTS` (`:343-348`). `advance()` (`:363-385`) is a match-on-phase dispatcher; every handler exits via a validated `transition_to`:

| Phase | Handler |
|--|--|
| `IDLE` | `transition_to(PRECHECK)` — pure edge, no handler |
| `PRECHECK` | `precheck()` — the guard chain (§2.4) |
| `CALLING_LLM` | `run_llm_call()` |
| `HANDLING_RESPONSE` | `handle_pending_response()` |
| `EXECUTING_TOOLS` | `execute_pending_tools()` |
| `AWAITING_EVENTS` | *never dispatched* — the loop suspends first; reaching it is a `RuntimeError` |
| `AUTOSAVING` | `autosave_pending_step()` |
| terminal | `RuntimeError` — cannot advance a finished turn |

Entry logic (`_prepare_turn`, `:308-324`): entering at `AWAITING_EVENTS` drains the *complete* pending table into history as one contiguous tool-result block and resumes at `PRECHECK` (`_resume_from_awaiting`, `:350-361`); entering at `DONE` continues a re-run; any other entry calls `resume_to_idle()` and resets the once-per-turn empty-response retry.

### 2.4 The precheck guard chain (`PRECHECK → CALLING_LLM | STOPPED`)

The single branch that can halt a step — a Chain-of-Responsibility whose every halt funnels through `_stop_precheck` → `STOPPED(reason)` (`session_run.py:556-600`). Order matters (first match wins):

| # | Guard | Condition | `terminal_reason` |
|--|--|--|--|
| 1 | user cancel | `cancel_event` set | `"interrupted by user"` |
| 2 | loop block | `turn.loop_blocked_since_progress >= DEFAULT_LOOP_BLOCKED_LIMIT` | `"loop block limit reached: N repeated tool calls"` |
| 3 | enforcement gate | `_apply_enforcement_gate()` chose the next phase (the wind-down funnel, §4) | *(gate-owned)* |
| 4 | session budget | `used_tokens >= max_budget_tokens` | `"budget exceeded: N tokens used"` |
| 5 | team ceiling | `_team_budget_exhausted()` (defense-in-depth aggregate cap) | `"team budget exceeded: aggregate spend reached the global cap"` |
| 6 | step limit | `step_count >= max_steps` | `"step limit reached: N steps"` |
| — | pass | none matched | → `transition_to(CALLING_LLM)` |

`max_budget_tokens` / `max_steps` are session-*lifetime* caps (not per-turn). This is where the §2.1 honesty fix lands: guard #2 now says `"loop block limit reached…"` instead of borrowing `STEP_LIMIT_EXCEEDED`. The context-overflow halt is *not* here — it fires one phase later, in `CALLING_LLM`, after a forced maximal compaction pass + one retry still overflows (`:1124`).

### 2.5 The three mutation primitives + two escapes

The FSM is mutated through exactly three primitives (`domain/session.py:299-338`) — a discipline the report should name, because it is what makes "the FSM cannot silently grow an illegal edge" true:

- **`transition_to(phase, reason=)`** — the only validated run-loop edge; raises `InvalidPhaseTransition` on an edge absent from the table; stamps `terminal_reason` when crossing into a terminal.
- **`set_phase(phase)`** — unchecked, deliberately reserved for out-of-band sets: scheduler process birth/enqueue, snapshot/restore, tests.
- **`resume_to_idle()`** — the single validated terminal → `IDLE` reset; clears `terminal_reason` so it never leaks into the next turn.

Two named escapes fire from **any** phase: **`fail(reason)` → ERROR** (ERROR has *no* inbound validated edge — reachable only this way, paired with a raised exception) and **`cancel(reason)` → STOPPED** (out-of-band scheduler kill; the in-loop cancel uses the validated `PRECHECK → STOPPED` edge instead). Topology-completeness is itself a fitness test: `test_session_phase_fsm.py` asserts `PHASE_TRANSITIONS` keys == the `SessionPhase` enum, so no state can be added or dropped without the table noticing.

---

## 3. `TurnEnforcementState` — the per-turn window as a kernel PCB

**Commit** `ce65053` (`refactor(session): extract TurnEnforcementState VO (xv6-shape)`). **Anchor** `domain/session.py:106-152` (the `TurnEnforcementState` dataclass) + `:344-376` (`checkpoint_user_turn` / `restore_user_turn` / `reset_for_user_turn`).

### 3.1 The design: nest exactly the snapshot-unit, keep lifetime latches flat

The catalog called `SessionState` *"a Value Object fighting a bolted-on control plane"* — genuine short-term memory **and** ~14 enforcement fields, the per-turn/lifetime split living only in prose comments. The debt was not the number of fields; it was that **the boundary along which they are saved, restored, and reset had no type structure.**

The fix is deliberately shaped after **xv6's `struct proc`**: you carve out a sub-struct precisely for the register set that *one mechanism* saves and restores as a unit (`struct context` / `struct trapframe` via `swtch`/trap), and you leave the rest (`sz`, `pid`, `state`) flat because their lifetime is different. Here:

- **Nested into `TurnEnforcementState` (8 fields)** — exactly the window that `checkpoint_user_turn` snapshots, `restore_user_turn` rolls back, and `reset_for_user_turn` clears **together**, at the one user-turn boundary:
  `recent_call_hashes`, `reads_since_last_edit`, `low_yield_since_progress`, `distinct_evidence_count`, `seen_result_hashes`, `scout_ledger`, `steps_since_progress`, `loop_blocked_since_progress`.
- **Kept flat on `SessionState`** — the session-*lifetime* latches `wind_down_done` / `wind_down_token_mark`, because their reset discipline differs (they survive a user-turn reset, like `pid` survives a context switch). The docstring says so explicitly.
- **Deleted** — the dead field `forced_unsatisfied` (write-only, never read) and the now-orphaned method `clear_recent_tool_hashes` (its only caller now swaps the whole box).

No accessors were added — call sites read `state.turn.reads_since_last_edit` directly, the way xv6 writes `p->context`, never `p->get_context()`.

### 3.2 The payoff: three hand-maintained field-lists collapse to one `deepcopy`

This is the exhibit to show. The checkpoint/restore/reset trio each hand-copied all eight fields — 24 lines that had to be kept in lock-step, each a place for a new field to be forgotten (a silent false-green risk on a non-`slots` dataclass).

**Before** (`ce65053^`, `checkpoint_user_turn`):
```python
return _UserTurnCheckpoint(
    message_count=len(self.messages),
    timestamp_count=len(self.message_timestamps),
    phase=self.phase,
    terminal_reason=self.terminal_reason,
    recent_call_hashes=tuple(self.recent_call_hashes),
    reads_since_last_edit=self.reads_since_last_edit,
    low_yield_since_progress=self.low_yield_since_progress,
    distinct_evidence_count=self.distinct_evidence_count,
    steps_since_progress=self.steps_since_progress,
    loop_blocked_since_progress=self.loop_blocked_since_progress,
    seen_result_hashes=frozenset(self._seen_result_hashes),
    scout_ledger=tuple(dict(card) for card in self.scout_ledger),
)
```

**After** (`ce65053`):
```python
return _UserTurnCheckpoint(
    message_count=len(self.messages),
    timestamp_count=len(self.message_timestamps),
    phase=self.phase,
    terminal_reason=self.terminal_reason,
    # Deep-copy the whole per-turn window as one pristine, reusable unit.
    turn=copy.deepcopy(self.turn),
)
```

`restore_user_turn` likewise drops from 8 field-assignments to one `self.turn = copy.deepcopy(checkpoint.turn)`; `reset_for_user_turn` from 8 clears to one `self.turn = TurnEnforcementState()`. This is exactly the xv6 `swtch` move — *save/restore the context struct, not its registers one at a time.* Adding a ninth per-turn signal now touches **one** place (the dataclass) and is automatically checkpointed, restored, and reset.

### 3.3 The net that made it safe

Before moving a single field, a shape-agnostic characterization test was laid (`0d5e58e`, `test_checkpoint_and_restore_user_turn_roll_back_per_turn_enforcement`) that asserts the round-trip **through public methods only** (checkpoint → mutate → restore yields the original; restore-again still yields the original, proving `deepcopy` on both ends keeps the checkpoint reusable). It is shape-agnostic on purpose: it passed identically before and after the field regrouping, which is what makes "pure regrouping, no behavior change" a *checked* claim rather than a hope.

**Wire-format invariant preserved:** the JSON persistence keeps its **flat** keys — the in-memory nesting is not the on-disk schema, so old snapshots round-trip unchanged (`application/session.py` serialize/restore blocks read `state.turn.X` but emit/consume flat keys).

---

## 4. Deleting the race-guessers — wind-down 4 → 3 triggers

**Commit** `82088e8` (`refactor(slim): delete extension valve + predictive EWMA heuristics`). Footprint: `application/extension_valve.py` **−206 (whole module)**, `application/session_run.py` **−263**, `tests/test_predictive_extension.py` **−559**. Net for the commit: **+13 / −1086**.

Two feed-forward heuristics tried to *beat the budget race* and were removed:

1. **Predictive EWMA guard.** Kept an exponentially-weighted moving average of per-turn cost and tripped wind-down *one turn early* when `used_tokens + ewma_turn_cost` would breach the reserve. It was the **fourth** OR'd wind-down trigger and the source of a cluster of tuning knobs (`ewma_alpha`, `predictive_cap`, `predictive_margin`). A forker had to understand a control-theory feed-forward term to reason about when a run stops.
2. **Extension valve.** A one-appeal negotiation: on hitting the brake the agent could *justify one more read*, granted only for a novel + concrete reason, hard cap 1 (an entire 206-line module + predicate judge). The subsystem's one genuine negotiation branch.

**After:** wind-down is a plain **Sensor → Controller → Actuator** funnel — *"THREE triggers → ONE actuator"* (`session_run.py:509-522` docstring). Three independent brakes converge on the single `_enter_wind_down` actuator (narrow toolset to submit-only + force `tool_choice`):

| # | Trigger | Signal |
|--|--|--|
| 1 | token budget past the commit reserve | `used_tokens` |
| 2 | progress watchdog | `steps_since_progress >= K` |
| 3 | low-yield sensor | `low_yield_since_progress >= M` |

The always-on **sensors** are untouched and remain byte-identical whether enforcement is on or off (proven by test); only the *controller* was ever gated, and now the controller is three honest thresholds instead of three thresholds + a predictor + a negotiation. **Defend the state (spent-past-reserve) — do not race the prediction.**

---

## 5. Metrics

Measured on the current tree, lane range `1205dc3..HEAD` (branch `refactor/slim-s1-fsm`).

| Slice | + | − | **net** | files |
|--|--:|--:|--:|--:|
| **Production code** (`opencollab/**`) | 334 | 810 | **−476** | 12 |
| Tests (`tests/**`) | 316 | 1347 | **−1031** | 17 |
| Docs (roadmap + catalog) | 352 | 0 | +352 | 2 |
| **Total** | 1002 | 2157 | **−1155** | 31 |

- **FSM:** states 14 → **10**, terminals 6 → **3**, wind-down triggers 4 → **3**, per-turn field-lists hand-maintained 3× → **1** (`deepcopy`).
- **Dead code removed:** 1 module (`extension_valve.py`), 1 field (`forced_unsatisfied`), 1 method (`clear_recent_tool_hashes`), 1 state (`SCHEDULED`), 4-into-1 terminal enum members.
- **New surface:** 1 value object (`TurnEnforcementState`, 8 fields), 1 pure module (`application/steering.py`, 93 lines — extracted, boundary-safe).
- **Suite:** `1370 passed` · `ruff: All checks passed!` · fitness functions `8 passed`. The test net is negative because 35 over-coupled enforcement tests were deleted (`5d52d22`) under the "lock the core, drop the presentation" discipline (roadmap iron-law #2, `40a7552`) — the golden-master + characterization nets that lock the FSM topology, checkpoint round-trip, and persistence round-trip stayed and were extended.

The 9 commits, in order:

| commit | subject |
|--|--|
| `5c7525b` | docs(slim): add Phase 0 roadmap + pattern catalog |
| `bc2390f` | test(slim): lay Lane S1 golden-master net before FSM collapse |
| `82088e8` | refactor(slim): delete extension valve + predictive EWMA heuristics |
| `c1108c1` | refactor(slim): collapse FSM to 10 states / 3 terminals (STOPPED(reason)) |
| `40a7552` | docs(slim): refine iron-law #2 — align-then-net, lock core only |
| `5d52d22` | test(slim): drop 35 over-coupled enforcement tests before the VO move |
| `0d5e58e` | test(slim): net checkpoint/restore per-turn roll-back before the VO move |
| `ce65053` | refactor(session): extract TurnEnforcementState VO (xv6-shape); drop dead field/method |
| `0e0a286` | refactor(session): extract steering to application/steering.py; funnel docstring |

---

## 6. Catalog reconciliation — prediction → realized

`pattern-catalog.md` was authored at baseline `cc04a36`, *before* the slimming, as a forward-looking plan; it deliberately describes the pre-slim tree and names the changes it expected. Lane S1 turns the following predictions into realized before/afters. (Stage labels S1–S4 in the catalog predate the roadmap's re-sequencing; the VO extraction the catalog files under "S4" landed here in Lane S1.)

| Catalog location | Wrote (pre-slim) | Now (`0e0a286`) |
|--|--|--|
| §3.1 *Terminal taxonomy → 3 behaviors* | *"Six terminals… Two current labels lie (loop-block→STEP_LIMIT, wind-down success→BUDGET_EXCEEDED) — see §8."* | **Realized.** Three terminals; labels no longer lie — `STOPPED(reason)`. §2.1 above. |
| §3.1 *SessionState as two jobs* — latent-tension | *"The single cleanest before/after… once the enforcement fields extract to a value object."* | **Realized.** 8 per-turn fields extracted to `TurnEnforcementState`; per-turn/lifetime split is now type structure, not prose. §3 above. |
| §3.1 *`_UserTurnCheckpoint` transaction* — Memento (clean) | Snapshots ~8 flat fields. | **Sharpened.** Snapshots one `turn` VO; Memento is now a single `deepcopy`. |
| §3.2 *Sensor→Controller→Actuator wind-down* | *"fired by **four** OR'd triggers."* | **Three** OR'd triggers. §4 above. |
| §3.2 *Predictive EWMA guard* | Present, *(clean)*. | **Deleted** (`82088e8`). |
| §3.2 *Extension valve* | *"the subsystem's… clearest slimming candidate."* | **Deleted** (`82088e8`) — the candidate acted on. |
| §6 honest-boundaries | *"The ~14-field enforcement control plane on the domain SessionState is a debt (latent-tension)… the cleanest before/after exhibit once extracted."* | **Extracted.** No longer a standing debt; now the headline before/after (this doc). |
| §7 S4 docstring plan | *"the headline before/after… extract the ~14 enforcement fields… into a `TurnEnforcementState` value object."* | **Done**, plus the interim-banner step was skipped by going straight to the VO. |
| §8 seam #1 | *"Two terminal labels lie… Collapsing… into STOPPED(reason) (slimming S1)… fixes this honesty bug."* | **Resolved.** §2.1 above. |

**Unchanged / still open (out of Lane S1 scope):**
- **ADP coverage matrix (§1) grades do not move.** The patterns are the same; their exhibits got cleaner. P12 (Exception Handling) stays ●, now with a *three*-behavior terminal taxonomy as cleaner evidence.
- **§8 seam #2 (`git_diff` skips the path jail)** is untouched — it belongs to the LLM/tools stage, not this lane. Still open.

**Recommendation for the report author:** the catalog remains the code-first source of truth; when you next regenerate or hand-edit it, apply the eight rows above and bump its "Generated … baseline `cc04a36`" line to note the S1 tip. This doc is the delta; the catalog stays the index.

---

## 7. Exhibits (anchors for the report)

All paths under `opencollab/`. Verified against the `refactor/slim-s1-fsm` tip.

| Exhibit | Anchor | Feeds report § |
|--|--|--|
| `SessionPhase` — 10 states, 3 terminals | `domain/session.py:18-28` | §1 |
| `TERMINAL_PHASES` frozenset + `is_terminal` | `domain/session.py:30-46` | §1 |
| **`PHASE_TRANSITIONS` — the full edge table** | `domain/session.py:71-93` | §1 |
| `transition_to` sets `terminal_reason` on crossing | `domain/session.py:307-316` | §1, §5 |
| Mutation primitives + escapes (`set_phase`/`fail`/`cancel`/`resume_to_idle`) | `domain/session.py:299-338` | §1 |
| `run_loop` + `advance` phase dispatcher | `application/session_run.py:286-385` | §1 |
| `precheck` guard chain (6 guards → STOPPED \| CALLING_LLM) | `application/session_run.py:556-600` | §1, §5 |
| `TurnEnforcementState` (8-field VO + xv6 docstring) | `domain/session.py:106-152` | §1 |
| `_UserTurnCheckpoint` holding one `turn` field | `domain/session.py:154-162` | §1 |
| `checkpoint`/`restore`/`reset` — the `deepcopy` collapse | `domain/session.py:344-376` | §1 (headline) |
| Enforcement funnel docstring — 3 triggers → 1 actuator | `application/session_run.py:509-522` | §5 |
| Honest `STOPPED` reasons | `application/session_run.py:507,582,590,595,1124` | §1, §5 |
| Steering extracted to a pure, boundary-safe module | `application/steering.py:1-93` | §5 |
| Checkpoint round-trip characterization net | `tests/test_session_characterization.py` (`…roll_back_per_turn_enforcement`) | §1 |
| FSM topology-completeness test (table keys == enum) | `tests/test_session_phase_fsm.py` | §1 |

---

*Generated 2026-07-15 from the `refactor/slim-s1-fsm` tip (`0e0a286`). Companion: `docs/pattern-catalog.md` (index), `docs/slimming-roadmap.md` (the execution manual + iron-laws).*
