# OpenCollab Prompt Architecture

> How every LLM-facing prompt fragment attaches to a running session, and how a
> session evolves turn-by-turn — including shaping and compaction — for both the
> **team/chat** path and the **workflow** path.
>
> Date: 2026-06-24. Grounded in code; file:line refs in the appendix.

---

## 0. The abstraction: four axes

OpenCollab has no single "prompt template". Instead, prompt text reaches the model
as a set of **fragments**, each attached to the conversation at a different moment,
in a different position, for a different reason. The domain layer already encodes
three of these axes as enums (`domain/context.py`); persistence is the fourth.

| Axis | Question | Values (enum) |
|------|----------|---------------|
| **时机 Timing** | *When* is it attached? | `STARTUP` · `PER_TURN` · `DURING_EXECUTION` · `ON_DEMAND` (`LoadTiming`) |
| **位置 Position** | *Where* in the message list? | `SYSTEM` · `USER_CONTEXT` (`ContextPosition`) |
| **类型 Layer** | *What role* does it play? | `IDENTITY` `TEAM` `SKILL` `TASK` `TOOL_META` `PROJECT` `MEMORY` (`ContextLayer`) |
| **持久化 Persistence** | Saved to the transcript, or model-view only? | persisted · ephemeral (shaped-copy-only) |

`LAYER_PRIORITY` (`domain/context.py:60`) ranks the **类型** axis so that under
context pressure low-value layers are shed first and identity is shed last:

```
IDENTITY 100 > TEAM 90 > SKILL 85 > TASK 80 > TOOL_META 50 > PROJECT 30 > MEMORY 20
                         └──────────── PIN_FLOOR = 70 ────────────┘
                  (≥70 is "pinned": never compacted, never shed)
```

Everything below is just this 4-axis classification applied to the concrete
fragments, plus the machinery that injects them.

---

## 1. Complete prompt inventory (classified)

Every hardcoded LLM-facing fragment in the system, by the four axes. "Persist"
= written into `state.messages`/transcript; "ephemeral" = only in the reshaped
copy handed to the model for one call.

### Static context (assembled once, at session birth)

| Fragment | Source | Timing | Position | Layer | Persist |
|----------|--------|--------|----------|-------|---------|
| Lead identity | `bootstrap/prompts/lead.md` | STARTUP | SYSTEM | IDENTITY | ✅ |
| Worker identity | `bootstrap/prompts/role.md` | STARTUP | SYSTEM | IDENTITY | ✅ |
| Team topology | `_team_section()` | STARTUP | SYSTEM | TEAM | ✅ |
| Skill catalog | `_render_skill_catalog()` | STARTUP | SYSTEM | SKILL | ✅ |
| Project map | `_project_context` / loader | STARTUP*or*ON_DEMAND | SYSTEM*or*USER | PROJECT | ✅ |
| Delegation task | `DelegationTask.render()` | STARTUP | USER | TASK | ✅ |
| Memory recall | loader `"memory"` | ON_DEMAND | USER | MEMORY | ✅ |
| Tool schemas | loader `"tools"` | ON_DEMAND | SYSTEM† | TOOL_META | — |

\* Project is STARTUP+SYSTEM when content is supplied at build time, else a
deferred ON_DEMAND+USER placeholder. † Tool schemas actually reach the model
through the function-calling API, not as prose.

### Workflow path (one-shot agents)

| Fragment | Source | Timing | Position | Role | Persist |
|----------|--------|--------|----------|------|---------|
| Workflow identity | `WORKFLOW_AGENT_PROMPT` | STARTUP | SYSTEM | IDENTITY (terse) | ✅ |
| Per-step task | `ctx.agent(prompt,…)` arg | STARTUP | USER | TASK | ✅ |
| Structured seed | `_STRUCTURED_INSTRUCTION` | STARTUP | USER (suffix) | format enforce | ✅ |
| Structured retry | `_STRUCTURED_RETRY` | DURING_EXECUTION | USER | format enforce | ✅ |

### Per-turn & recovery injections (the run loop)

| Fragment | Source | Timing | Position | Role | Persist |
|----------|--------|--------|----------|------|---------|
| Budget/steps status | `_build_steering_block()` | PER_TURN | USER | steering | ✅ if foldable, else ephemeral |
| Soft read nudge | same (`reads ≥ 8`) | PER_TURN | USER | steering | ↑ |
| Hard read demand | same (`reads ≥ 16`, forces `tool_choice`) | PER_TURN | USER | steering | ↑ |
| Empty-stop nudge | `_EMPTY_STOP_NUDGE` | DURING_EXECUTION | USER | recovery | ❌ (trace only) |
| Cancel / budget / step stop | `precheck()` | DURING_EXECUTION | SYSTEM | terminal | ✅ |
| Context-overflow stop | `_stop_on_context_overflow()` | DURING_EXECUTION | SYSTEM | terminal | ✅ |

### Shaping / compaction (read-time projection over history)

| Fragment | Source | Timing | Position | Role | Persist |
|----------|--------|--------|----------|------|---------|
| Eager tool-output stub | `EagerToolOutputClearShaper` | DURING_EXECUTION | (replaces tool result) | compaction | ❌ (shaped copy) |
| Compaction marker | `COMPACTED_MARKER_PREFIX` | DURING_EXECUTION | SYSTEM | compaction | ❌ (shaped copy) |
| Compaction meta-prompt | `BASE_COMPACT_PROMPT` (+`NO_TOOLS_*`) | DURING_EXECUTION | — (asks summarizer LLM) | meta | — |

Two observations fall straight out of the table:

1. **The system prompt is built once and frozen.** Everything dynamic
   (budget, nudges, summaries) rides in **USER** position or in the
   model-view-only shaped copy — the SYSTEM block stays byte-stable across
   turns, which keeps the provider's prompt cache warm.
2. **`STARTUP` is the only timing that touches the persisted system prompt.**
   `PER_TURN`/`DURING_EXECUTION` fragments are either folded into a user turn or
   live only in the reshaped copy. `domain/context.py` declares `PER_TURN` and
   `DURING_EXECUTION` as `LoadTiming` members, but the context *builder* uses
   only `STARTUP`/`ON_DEMAND`; the per-turn dynamics live in `session_run.py`'s
   loop and the shaping pipeline, not in `ContextPlan`.

---

## 2. Two session shapes

Both paths produce the same runtime object (a `SessionState` driven by the FSM in
`domain/session.py`). They differ only in **how the first messages are seeded**.

```
                        TEAM / CHAT                          WORKFLOW
                 ┌───────────────────────────┐   ┌────────────────────────────┐
 SYSTEM  ──────► │ lead.md / role.md          │   │ WORKFLOW_AGENT_PROMPT      │
                 │  + team topology           │   │  (3 sentences, fixed)      │
                 │  + skill catalog           │   │                            │
                 │  + project map             │   │  ← NO layering, no lead.md │
                 │  (joined with "\n\n")      │   │                            │
                 ├───────────────────────────┤   ├────────────────────────────┤
 USER    ──────► │ (lead: empty — waits for   │   │ ctx.agent(prompt)          │
                 │  the human)                │   │  + _STRUCTURED_INSTRUCTION │
                 │ (spawn: DelegationTask)    │   │    (when schema= given)    │
                 └───────────────────────────┘   └────────────────────────────┘
                  long-lived, many user turns      one-shot, one task, then dies
```

The **team/chat** system prompt is assembled by `ContextBuilder.build_plan()`
into an ordered tuple of `ContextSource`s, then `ContextPlan.system_prompt()`
joins the `STARTUP`+`SYSTEM` sources with `"\n\n"`:

```python
# domain/context.py:120
def system_prompt(self) -> str:
    return "\n\n".join(s.content for s in self._startup(ContextPosition.SYSTEM))
# → lead.md  +  team section (omitted when allow_all)  +  skills  +  project
```

`_build_initial_state()` (`bootstrap/container.py:97`) then makes
`[{"role":"system", "content": agent.system_prompt}]` and appends any
`startup_user_messages()` (the task, for spawned agents; nothing, for a lead).

The **workflow** system prompt is the deliberately-terse constant — every
`ctx.agent()` call builds a *fresh* one-shot session with it:

```python
# bootstrap/workflow_runtime.py:49 — "Deliberately terse; the per-call prompt carries the task."
WORKFLOW_AGENT_PROMPT = (
    "You are an autonomous agent invoked as one step of a larger workflow. "
    "Complete the task described in the user message. Use your tools as needed. "
    "Be concise and finish with a clear final answer."
)
```

The actual work is the `prompt` argument supplied by an SDK consumer workflow,
which becomes the first **user** message. For `schema=` calls,
`_run_structured_agent` appends
`_STRUCTURED_INSTRUCTION`; if the first pass yields no structured result, a
corrective pass adds `_STRUCTURED_RETRY` with `tool_choice` forced to the
`structured_output` function.

> **Key contrast:** workflow agents never touch the `lead.md`/`role.md` layered
> context. They are a parallel, minimal path so `parallel()`/`pipeline()` can
> spin up dozens cheaply.

---

## 3. Session evolution — the per-turn loop

Both shapes, once seeded, run the same finite-state machine
(`domain/session.py`). The conversation is a single growing list
(`SessionState.messages`); each turn appends to it.

```
 SCHEDULED → IDLE → PRECHECK ──► CALLING_LLM ──► HANDLING_RESPONSE ──► EXECUTING_TOOLS
                       │              │                 │                     │
                       │              │                 ├─► DONE              ├─► AUTOSAVING ─┐
                       │              │                 └─► AUTOSAVING        └─► AWAITING_EVENTS
                       │              │                     (empty-stop retry)        │
                       │              ▼                                                │
                       │        CONTEXT_OVERFLOW (terminal)        AUTOSAVING ─────────┘
                       │                                              │
                       ├─► CANCELLED / BUDGET_EXCEEDED / STEP_LIMIT_EXCEEDED   └─► back to PRECHECK
                       │   (terminal, with a persisted SYSTEM stop message)
                       ▼
   DONE/terminal → IDLE (resume on the next user turn; step_count & used_tokens NOT reset)
```

What each turn does to the message list and what prompt fragments it injects:

**① PRECHECK** — guards before spending a model call. If a guard trips it
**appends a SYSTEM stop message** and goes terminal:

- `cancel_event` → `"[Session interrupted by user]"` → `CANCELLED`
- `used_tokens ≥ budget` → `"[Budget exceeded. Session stopped.]"` → `BUDGET_EXCEEDED`
  (or the team aggregate cap → `"[Team budget exceeded …]"`)
- `step_count ≥ max_steps` → `"[Step limit reached. Session stopped.]"` → `STEP_LIMIT_EXCEEDED`

**② CALLING_LLM** — the only place per-turn steering is injected
(`call_llm()`, `session_run.py:538`):

```python
status = f"[Budget: ~{remaining_k}k/{total_k}k tokens left, ~{steps_left} steps left.]"
```

Built fresh every turn from live counters, plus a **reads-without-write** clause:

- `reads ≥ 8` (soft): *"You have read N times without making an edit. If you can
  describe the fix, make it now …"*
- `reads ≥ 16` (hard): *"… STOP reading — your next action MUST be a file_write
  or apply_patch edit."* — and `tool_choice` is forced to `"required"`.

The clever bit is **how** it attaches (the persistence axis in action):

```python
# session_run.py:564 — fold IN PLACE iff history ends on a user turn
persisted = steering and state.messages and state.messages[-1]["role"] == "user"
if persisted:
    state.messages[-1] = _fold_steering(state.messages[-1], steering["content"])  # saved to transcript
messages = shaper.shape(state.messages)        # reshaped COPY for the model
if steering and not persisted:
    messages = [*messages, steering]            # continuation step → ephemeral, model-view only
```

So on the **first step of a user turn** the budget line is folded into the user
message and saved; on **continuation steps** (tail is a tool/assistant message)
it rides only in the shaped copy. Either way the model always sees current
budget; the transcript never accumulates duplicate status lines.

Then the model is called against `shaper.shape(state.messages)` — a reshaped
**copy** (see §4). `state.messages` itself is never mutated by shaping.

**③ HANDLING_RESPONSE** — appends the assistant message. If the response is
empty (no text, no tool call), it injects a `"[no output produced this turn]"`
placeholder + `_EMPTY_STOP_NUDGE` and retries **without** consuming a step. This
nudge is **not persisted** — it exists only to unstick the model.

**④ EXECUTING_TOOLS** — runs tool calls, **appends a tool-result message per
call**. This is the main driver of context growth. Deferred work (e.g. a spawned
sub-agent) suspends into `AWAITING_EVENTS`; the scheduler resumes to `PRECHECK`
when results land.

**⑤ AUTOSAVING → PRECHECK** — emits `step_end` (autosave hook) and loops.

Counters that drive all of the above (`SessionState`): `used_tokens` (budget),
`step_count` (lifetime step cap — *not* reset between user turns),
`reads_since_last_edit` (steering signal, reset to 0 on a successful write), and
`context_tokens` (the real measured input size from the last response, used by
the shaper).

---

## 4. Session evolution under pressure — shaping & compaction

The model never sees `state.messages` directly. Before every call,
`call_llm()` runs it through a **shaping pipeline** (`ShaperPipeline`,
`application/shaping/pipeline.py`) that returns a reshaped *copy*. The full
history stays on disk for a lossless resume. The pipeline is a chain of cheap→
expensive rungs:

```
state.messages
   │
   ├─[A] EagerToolOutputClearShaper   always-on, free, deterministic
   │        keep the 12 newest tool results verbatim; replace older ones with a
   │        byte-stable stub: "[Old tool result cleared: <tool> <target>] — you
   │        already ran this; full output is in the transcript above…"
   │
   ├─ … per-tool-result budget · low-priority shed · snip …
   │
   ├─[B] AutoCompactShaper            last resort, model-generated summary
   │        if estimate(messages) > 120k tokens:
   │            segment = droppable region (excl. last 4 groups, excl. pinned ≥70)
   │            summary = summarizer(segment)        ← LLM call, see below
   │            replace segment with ONE visible SYSTEM marker:
   │              "[Context auto-compacted — summary of N earlier messages]:\n…"
   │        target after compaction ≈ 90k (anti-thrash headroom)
   │
   └─► shaped copy → model
```

**A — eager tool-output clear** (`shaping/eager.py`) is the everyday lever: it
never calls a model, it just ages out stale tool outputs (file reads, bash, grep,
diffs) once they fall outside the newest-12 window, replacing the *content* while
keeping the `tool_call_id` skeleton intact. Monotonic and byte-stable, so a
cleared result stays identical on every subsequent call — cache-friendly.

**B — auto-compaction** (`shaping/reactive.py:236`) is the heavy lever, default
-off until a `summarizer` is injected. When estimated context crosses **120k**
tokens it hands the droppable span to the summarizer and swaps it for a single
self-announcing marker (`"compacted": True`). It always compacts **whole message
groups**, so the kept recent window starts on a group boundary and no
`tool_call_id` is orphaned; pinned identity/team/task (priority ≥ `PIN_FLOOR=70`)
are never folded in.

The summarizer is asked with the 9-section `BASE_COMPACT_PROMPT`
(`application/compaction_prompt.py`):

```
1 Primary Request & Intent   2 Key Technical Concepts   3 Files & Code Sections
4 Errors & fixes             5 Problem Solving           6 All user messages ◄ anti-drift
7 Pending Tasks              8 Current Work              9 Optional Next Step ◄ verbatim quotes
```

wrapped by `NO_TOOLS_PREAMBLE` / `NO_TOOLS_TRAILER` (*"text only — do NOT call
any tools"*). The model replies `<analysis>…</analysis><summary>…</summary>`;
`format_compact_summary()` strips the scratchpad and unwraps the summary, falling
back to a bounded raw excerpt if the block is missing. (This is the very prompt
that produced the summary at the top of *this* session.)

**Context-overflow safety net.** The 120k trigger is an *estimate* and can
under-count dense code/JSON/CJK. If the provider rejects a call as an overflow,
`call_llm()` runs a **forced** maximal compaction (compact every sheddable source
unconditionally) and retries **once**. If even that overflows — e.g. the pinned
seed alone exceeds the window — it raises `_ContextOverflowStop`, the loop appends
`"[Context overflow. Session stopped.]"`, and the session ends in the terminal
`CONTEXT_OVERFLOW` phase.

> **Same pipeline for workflows.** Workflow sessions are built through the same
> `build_session_runtime()` and get the same default shaper. A long-running
> workflow agent would compact at 120k exactly like chat — duration and model
> calls drive compaction, not the session *type*. In practice workflow agents are
> one-shot and rarely reach it.

---

## 5. Worked timeline

A single team-lead session from birth, to make the axes concrete:

```
t0  BIRTH
    SYSTEM = lead.md + team + skills + project            [STARTUP/SYSTEM/IDENTITY…]  persisted
    (no user message yet — lead waits)

t1  user: "fix the bug in X"                               [USER]                      persisted
    PRECHECK ok → CALLING_LLM
      fold budget into the user msg:
        "fix the bug in X\n\n[Budget: ~480k/500k tokens left, ~39 steps left.]"
                                                            [PER_TURN/USER/steering]    persisted (folded)
      model → reads file (tool call)
    EXECUTING_TOOLS appends tool-result                                                 persisted

t2  CALLING_LLM (continuation; tail is a tool msg)
      budget line rides in the shaped COPY only            [PER_TURN/USER/steering]    ephemeral
      eager-clear stubs out the t1 file read (>12 ago? not yet)
      model reads more files …

…   reads pile up → t9 steering gains the soft nudge, t17 the hard nudge (tool_choice=required)
…   tool results accumulate → estimate passes 120k

tN  CALLING_LLM
      shaper: eager-clear stubs the oldest reads;
      AutoCompact summarizes groups 1..(N-4) → one SYSTEM marker;
      model sees [system][marker: 9-section summary][last 4 groups][budget line]
      state.messages still holds the full history on disk
```

Persisted axis at a glance: the SYSTEM identity block and folded user-turn budget
lines survive in the transcript; continuation-step budget lines, eager stubs, and
the compaction marker exist only in the per-call reshaped copy.

---

## Appendix — file:line index

| Concept | File | Line |
|---------|------|------|
| `ContextLayer` / `LoadTiming` / `ContextPosition` | `domain/context.py` | 27–52 |
| `LAYER_PRIORITY`, `PIN_FLOOR` | `domain/context.py` | 60–68 |
| `ContextPlan.system_prompt()` | `domain/context.py` | 120 |
| `_load_default_prompt`, `DEFAULT_LEAD/ROLE_PROMPT` | `bootstrap/team_config.py` | 53–58 |
| `ContextBuilder.build_plan()` | `bootstrap/context_builder.py` | 100–207 |
| `_team_section()` / `_render_skill_catalog()` | `bootstrap/context_builder.py` | 254–292 |
| `_build_initial_state()` | `bootstrap/container.py` | 97 |
| Session FSM (`SessionPhase`, transitions) | `domain/session.py` | 15–106 |
| `SessionState` counters | `domain/session.py` | 118–154 |
| Run loop / FSM dispatch | `application/session_run.py` | 140–224 |
| `precheck()` stop messages | `application/session_run.py` | 226–269 |
| `_build_steering_block()` / `_fold_steering()` | `application/session_run.py` | 474–536 |
| `call_llm()` (steering fold + shape + overflow net) | `application/session_run.py` | 538–607 |
| Empty-stop nudge | `application/session_run.py` | 50–54, 325–347 |
| `_stop_on_context_overflow()` | `application/session_run.py` | 703–717 |
| `WORKFLOW_AGENT_PROMPT` | `bootstrap/workflow_runtime.py` | 49 |
| `build_workflow_session()` | `bootstrap/workflow_runtime.py` | 124–158 |
| `_STRUCTURED_INSTRUCTION` / `_STRUCTURED_RETRY` | `application/workflow.py` | 52–69 |
| `_run_structured_agent` (seed + corrective pass) | `application/workflow.py` | 383–457 |
| Consumer workflow role prompts | external SDK consumer, such as OpenCollab-Eval | package-defined |
| Shaping pipeline order | `application/shaping/pipeline.py` | 117–131 |
| Trigger/target/keep-recent defaults | `application/shaping/pipeline.py` | 14–17 |
| `EagerToolOutputClearShaper` + stub | `application/shaping/eager.py` | 47–200 |
| `AutoCompactShaper.shape()` | `application/shaping/reactive.py` | 236–256 |
| `BASE_COMPACT_PROMPT`, `NO_TOOLS_*`, `format_compact_summary` | `application/compaction_prompt.py` | 26–109 |
| `build_summary_request` / `build_continuation_message` | `application/compaction_prompt.py` | 121–164 |
