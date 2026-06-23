# Closed-Loop Steering Layer — Design

**Date:** 2026-06-23
**Branch:** feat/analyst-solve-workflow
**Status:** design (approved to design; implementation scope TBD)

## Problem

OpenCollab's session loop is almost entirely **open-loop**: per turn it only
*trims* messages (`application/shaping/*`: eager-clear, context shaping) plus two
narrow reactive injections — `_EMPTY_STOP_NUDGE` (empty assistant turn) and the
loop-detector block warning. It never re-injects the objective or steers on
observed behaviour.

Evidence (live smoke, run_id `analyst-p6749`, code `dae2edf`): **django-11564**
made **55 `file_read` + 52 `grep` = 107 reads and 0 edits** over 118 steps,
produced the *correct* fix in prose at step 235 (`django/conf/__init__.py`
`LazySettings.__getattr__`), then reverted to reading until the 1800s wall →
empty patch. A textbook open-loop failure: nothing was watching "many reads, zero
writes" and intervening. `flask` (RESOLVED) over-explored too but eventually
committed (3 writes) — the difference was *pulling the trigger*.

We drive **kimi-k2.6**, whose agentic instincts are weaker than Claude's (markup
leaks, over-analysis), so a lean closed-loop steering layer must compensate. The
fix is **not** more bulk text (Claude Code spends 20-30k/turn on scaffolding —
expensive); it is a **lean (~100-150 token/turn) closed loop**: budget
self-awareness + a reads-without-write escalation.

## Load-bearing constraints (from the cache/cost review)

1. **Cache safety.** The steering text changes every turn. It MUST be the **last
   message**, after any (future) `cache_control` breakpoint (which sits on the
   last stable *system* message). Placed before the breakpoint it would bust the
   prefix cache every turn. No `cache_control` is wired today, but the placement
   must be cache-ready now so it doesn't have to move later.
2. **Ephemeral.** Steering is appended to the **shaped copy** of the message list
   at send time and is **never** written to `state.messages`. This keeps it out
   of the transcript/replay, out of the eager-clear index, and rebuilt fresh each
   turn (mirrors the `_EMPTY_STOP_NUDGE` placeholder discipline,
   `session_run.py` ~305-332, which never persists the nudge).
3. **Role/position.** A `role:"user"` message appended last (matches the existing
   nudge; recency-weighted; valid after a `role:"tool"` batch).

## Injection seam (unanimous across the design pass)

`application/session_run.py :: SessionRunUseCase.call_llm()` — AFTER
`self.shaper.shape(self.state.messages)` (~line 472), BEFORE `self._complete(...)`
(~line 477). A new `_build_steering_block()` returns the message (or `None`); the
caller appends it to the shaped list only. All session creation flows
(single-session, analyst-solve spawned coder/tester/scout) go through the same
`build_session()` factory, so the seam serves **every** mode uniformly.

State available at the seam without new wiring:
`self.state.used_tokens`, `self.max_budget_tokens`, `self.state.step_count`,
`self.max_steps`, and `self.agent.tools` (to detect write-tool availability).

## Counter: `reads_since_last_edit` (domain)

Add to `domain/session.py :: SessionState` a lifetime int, mirroring
`recent_call_hashes`:

```python
reads_since_last_edit: int = 0   # reset on a successful file_write/apply_patch
```

- **Increment** in `application/tool_execution.py :: process()` when a *read*
  tool (`file_read`, `grep`) executes successfully (after the loop-detector lets
  it through). Count `file_read`+`grep` only — NOT `bash` (heuristic, false
  resets). Record the delta on `ToolProcessingResult` and apply it to state in
  `domain/tools.py :: apply_to()`, exactly like `recent_hash_updates` (process()
  does not mutate state directly).
- **Reset to 0** when the batch contains a successful write
  (`file_write`/`apply_patch` whose result is not an error string).
- Because each analyst-solve coder is a **fresh one-shot session**, the counter
  is naturally per-coder-session — "this coder has read N times without writing."
  No per-phase reset needed.
- Read/write tool names live as module constants
  (`_READ_TOOLS`, `_WRITE_TOOLS`) next to `_PATH_NORMALIZED_TOOLS` so a rename is
  caught in one place.

## Steering block content (verbatim templates, ~100-150 tokens)

**Always (every session, every turn):** budget self-awareness.
```
[Status: {spent_k}k/{total_k}k tokens used, ~{steps_left} steps left. Spend them landing and verifying a fix, not exploring.]
```

**Conditional — soft write-nudge.** Appended ONLY when BOTH (a) the session's
toolset contains a write tool (`file_write`/`apply_patch`) — so it never fires
for read-only scouts/testers/planner — AND (b)
`reads_since_last_edit >= READS_NUDGE_SOFT`:
```
 You have read {reads} files/searches without making an edit. If you can describe the fix, make it now with file_write or apply_patch before reading more.
```

**Conditional — hard commit pressure.** When write-tool present AND
`reads_since_last_edit >= READS_NUDGE_HARD`, replace the soft line with:
```
 STOP reading. Your next action MUST be a file_write or apply_patch edit.
```
…and the loop sets `tool_choice="required"` for that call (force a tool call;
combined with the directive it pushes a write). MVP keeps the read tools
available (lower risk than removing them); if data shows the model still re-reads
under "required", a follow-up can restrict the toolset to write/test only.

Constants (tunable; `application/session_run.py` or a small steering module):
`READS_NUDGE_SOFT = 8`, `READS_NUDGE_HARD = 16`. (django did 107 reads/0 writes;
both would have fired early.)

## Escalation ladder — who fires when (composition)

| Rung | Trigger | Action | Lives in |
|------|---------|--------|----------|
| Status | every turn | budget/steps line | session loop (generic) |
| A soft | reads ≥ 8 + write tool | advisory nudge line | session loop |
| B hard | reads ≥ 16 + write tool | strong directive + `tool_choice="required"` | session loop |
| C bridge | coder round returns prose `stop` but `tree_changed()` is False | re-issue with `FORCED_PROMPT` immediately | analyst-solve (`_run_phase`) |
| (existing) loop-detector | same call ≥3 / same file ≥8 | block the call | tool_execution (unchanged) |
| (existing) budget-floor forced-write | budget/time low | last-resort guaranteed write | analyst-solve (unchanged) |

Composition rules:
- The loop-detector fires FIRST (blocks the call); only a call it lets through
  increments `reads_since_last_edit`. They are orthogonal (loop = *same* target
  repeated; reads-counter = *any* reads without a write).
- Rung C is the **early** version of the existing budget-floor forced-write
  (which stays the absolute last resort). C catches the django-235 case
  (correct-fix-in-prose, empty tree) per coder round instead of waiting for the
  wall.

## Phasing

- **P0 — MVP (session loop, generic, zero new ports):** budget self-awareness +
  `reads_since_last_edit` counter + Rung A (soft) + Rung B (hard, `tool_choice`).
  Ephemeral last-message placement. Applies to all modes. *Highest leverage, no
  cross-layer wiring.*
- **P1 — analyst-solve targeted:** Rung C (prose-stop + empty-tree → early
  `FORCED_PROMPT` re-issue) in `_run_phase`. Directly kills the django-235 mode.
- **P2 — objective re-inject:** plumb the phase/overall goal into the session
  (factory/ctx thread) for the always-on objective line. Deferred because it is
  the weakest field and the most invasive wiring.

## Integration points

- `domain/session.py`: `SessionState.reads_since_last_edit` field +
  `add_reads`/reset; reset in `reset_for_user_turn()`.
- `domain/tools.py`: `ToolProcessingResult` carries the reads delta / reset flag;
  `apply_to()` applies it.
- `application/tool_execution.py`: `_READ_TOOLS`/`_WRITE_TOOLS` constants;
  increment/reset in `process()`.
- `application/session_run.py`: `_build_steering_block()`; append in `call_llm()`
  after shape; set `tool_choice="required"` for Rung B.
- `workflows/analyst_solve.py` (P1): Rung C in `_run_phase`; thresholds as
  constants.

## Tests

Mirror `tests/test_session_run_loop.py` / `test_eager_tool_clear_shaper.py`:
- steering block built with correct budget/steps numbers;
- steering is **never** persisted to `state.messages` (ephemeral);
- steering appended **last** in the shaped list;
- write-nudge fires only when a write tool is present AND reads ≥ soft;
- hard rung sets `tool_choice="required"` at reads ≥ hard;
- `reads_since_last_edit` increments on file_read/grep, resets on file_write/
  apply_patch;
- read-only session (scout/tester) gets the status line but NO write-nudge;
- (P1) prose-stop + empty tree triggers an early forced-write re-issue.

## Risks & mitigations

- **Cache bust** → steering is last + ephemeral (see constraints). Verified: no
  `cache_control` wired yet; placement is cache-ready.
- **Token bloat** → cap the block at ~200 tokens; budget line is ~25 tokens.
- **Tool-name drift** → `_READ_TOOLS`/`_WRITE_TOOLS` constants, one place.
- **Read-only sessions mis-nudged** → write-nudge gated on write-tool presence.
- **Rung B usability cliff** (model needs one more read to verify its edit) →
  MVP keeps read tools available; `required` only forces *a* tool call.
- **Threshold tuning** → constants; tune on eval after P0.

## Open questions (resolved for MVP)

- Objective reachability → **deferred to P2**; MVP uses session-local state only.
- Counter home → **domain SessionState** (mirrors recent_call_hashes).
- bash-as-read → **excluded** (count file_read+grep only).
- Hard-rung toolset removal vs `tool_choice` → **`tool_choice="required"` first**;
  toolset restriction only if data demands it.
