# Context: Assembly, Injection & Shaping — the runtime lifecycle

> **What.** How a session's context is *built once*, *injected once*, and then
> *re-shaped on every LLM call* — the full runtime path from "where does the goal
> come from" to "what the model actually sees this turn".
>
> **Who.** Anyone forming a mental model of OpenCollab's context handling, or
> extending it (new context source, new eviction rule).
>
> **Status.** Grounded in code, post-Lane-S3 (5 context layers, 5 shaper rungs).
> Supersedes the *runtime-flow* portions of `2026-06-24-prompt-architecture.md`,
> which still describes the pre-S3 lazy-loader / 7-layer model. `file:line`
> refs are current as of branch `refactor/slim-s3-context`.

---

## 0. The one-sentence model

There are **two subsystems**, and 90% of confusion comes from conflating them:

- **Assembly** — *build-time, once.* Decides **what goes in**. Pure `domain` +
  `bootstrap`. Produces a system prompt string and a list of startup user
  messages. (`domain/context.py`, `bootstrap/context_builder.py`)
- **Shaping** — *read-time, every LLM call, over a copy.* Decides **what to drop
  first when the window fills**. (`application/shaping/`)

They never touch each other's data. The only bridge is a `_ctx` provenance tag
plus a single priority floor (`PIN_FLOOR = 70`).

The anchor to keep:

> **`state.messages` is the full ledger** — persisted, append-only, lossless on
> resume. **The shaped view is a per-call ephemeral projection** — fed to the
> provider, then thrown away; next turn re-shapes from the full ledger again.
> Shaping is always *read-time / per-call / over a copy / never mutates the ledger.*

The rest of this chapter is that model, traced through real code, in the order
data actually flows.

---

## 1. Assembly — one plan, built once

`ContextBuilder.build_plan(role, task?, context?)` is the editorial step
(`bootstrap/context_builder.py`). It emits an ordered tuple of `ContextSource`
fragments — always `identity`, then conditionally the rest — and a source is
emitted **only when it has content** (no registered-but-empty placeholders; the
absence of memory/RAG is an honest gap, not a stub).

| Source (layer, priority) | Content producer | Ref |
|---|---|---|
| `identity` (IDENTITY, 100) | `RoleConfig.prompt` — from `team.yaml` or packaged `prompts/lead.md` / `prompts/role.md` | `context_builder.py:116` |
| `team` (TEAM, 90) | `_team_section` — topology-derived "## Your team" list; empty under `allow_all` or without a coordination tool | `:125`, `:222` |
| `skills` (SKILL, 85) | `_render_skill_catalog` — only when `use_skill` is in `role.tools` and the store has manifests | `:138`, `:242` |
| `project` (PROJECT, 30) | `build_repo_map(lead_workspace)` — a bounded directory tree; one map orients the whole team | `:153`, `session_factory.py:318` |
| `task` (TASK, 80) | `DelegationTask(role, task, context).render()` — only when a task is passed | `:162`, `domain/scheduler.py:56` |

Assembly is **generic over `position`, and never inspects `layer`** — the
consequence being: *adding a new kind of context = registering a new `Source`,
with no change to assembly code.*

`build_plan` returns `ContextPlan(sources=…)` (`:173`). `build_agent` then folds
it into the `Agent` value object by splitting on **position**:

- **SYSTEM sources** (identity/team/skills/project) → `plan.system_prompt()`
  joins their content with blank lines into **one string** → `Agent.system_prompt`
  (`context_builder.py:207`, `domain/context.py:98`).
- **USER_CONTEXT sources** (today only `task`) → `plan.startup_user_messages()`
  → provider-shaped user dicts, each stamped with `_ctx` (`domain/context.py:102`).

Note the asymmetry this creates: the system prompt is a *single opaque blob*
after the join; per-fragment identity survives only through the markdown headers
each producer emits. The task, by contrast, stays a *separate, individually
tagged* message — precisely so shaping can pin or shed it on its own.

---

## 2. The `_ctx` handshake

`startup_user_messages()` stamps each USER_CONTEXT message with an internal tag
(`domain/context.py:111`):

```python
{"role": "user", "content": s.content,
 "_ctx": {"layer": s.layer.value, "priority": s.effective_priority}}
```

This is the **only** thing connecting Assembly to Shaping. Its job: once these
seed messages are flattened into the live history, they look identical to any
ordinary runtime user turn — the tag lets the shaper tell "pinned startup source"
from "sheddable chatter" **by provenance**.

- Providers ignore the extra key (same convention as `tool_call_id` and the
  auto-compact `compacted` flag) — it is an in-memory sidecar, never sent to the
  model as-is.
- Shaping reads only one field, `priority`, via `ctx_priority` /
  `is_pinned` (`application/shaping/pipeline.py:171`, `:177`).
- `PIN_FLOOR = 70` (`pipeline.py:24`) is the **single load-bearing cut today**:

```
LAYER_PRIORITY (domain/context.py):
  IDENTITY 100 · TEAM 90 · SKILL 85 · TASK 80   ← all ≥ 70 → pinned
  ────────────────────── PIN_FLOOR = 70 ──────────────────────
  PROJECT 30                                     ← below → sheddable
```

The sub-floor ordering (only PROJECT sits below) becomes meaningful only once a
*second* deferred source (memory/RAG) actually carries content — an honest gap,
not a design that's already exercised.

---

## 3. Injection — where startup messages land

There is exactly **one** place that puts startup user messages into the live
list: `_build_initial_state` (`bootstrap/container.py:98`). Everything else just
threads the list to this seam.

```python
messages = [{"role": "system", "content": agent.system_prompt}]
if seed_user_messages:
    messages.extend(seed_user_messages)
return SessionState(messages=messages)
```

Two facts people misread:

1. **The system prompt lives in *both* places.** It is the source of truth on
   `Agent.system_prompt`, *and* it is materialized as `messages[0]` here. Not a
   dichotomy — both.
2. **Order is `[system, *seed]`.** The task (the only USER_CONTEXT seed today)
   is `messages[1]`, *after* the system prompt.

**Lead vs. spawn diverge here** — an important asymmetry:

- **Spawned child** — `build_spawn_session` builds the plan *with* the task and
  passes `plan.startup_user_messages()` as `seed_user_messages`
  (`session_factory.py:347`, `:369`). Opening history: `[system, task]`, where
  `task` is `_ctx`-tagged (priority 80) and therefore **pinned**.
- **Lead (agent-0)** — `create_lead_session` builds a *task-less* agent and seeds
  **nothing** (`session_factory.py:392`). Opening history: `[system]` only. Its
  goal arrives **later**, post-construction, via `add_user_message`
  (`application/session.py:188`), which appends a plain `{role:user, content}`
  with **no `_ctx`** — so the lead's goal is **not pinned**; it is governed by
  recency-based shaping like any other turn.

---

## 4. Where the eval goal comes from

During SWE-bench-style evaluation, a task carries `problem_statement`,
`hints_text`, `FAIL_TO_PASS`, `base_commit`, etc. **None of this assembly lives
in this repo** — a repo-wide grep finds those field names only in tests. The
concatenation into a single goal string happens in an **external harness**
(the prediction-generation workflow package).

That goal string crosses into OpenCollab through the **SDK boundary**, in one of
two plain calls:

- `OpenCollab.workflow(flow, inputs)` → the external workflow function reads
  `inputs[...]` and drives the workflow regime (`sdk/client.py`).
- `OpenCollab.agent(prompt)` → the single-agent bootstrap lifecycle
  (`bootstrap/programmatic.py`).

`OpenCollab.team(prompt)` is the symmetric scheduler-regime entry. The facade
only validates arguments and delegates; concrete assembly remains in bootstrap.

By the time it reaches OC it is **just a string**; OC knows nothing of SWE-bench
fields. Its fate inside OC — and whether it is pinned — is decided by *which
entry path* it takes (see §3):

- **As the lead's goal** → `add_user_message` → untagged, recency-governed, **not
  pinned**.
- **As a child's task** → `DelegationTask.render()` → TASK source →
  `_ctx{task, 80}`, **pinned**.

This is the practical upshot of the lead/spawn asymmetry: the same goal text is
pinned context for a delegated worker but sheddable history for the lead.

---

## 5. The per-turn loop

The driver is `run_loop → advance → call_llm` (`application/session_run.py:286`),
an FSM stepped until the turn suspends or finishes.

**The ledger only grows.** Each turn *appends* to `state.messages` and never
trims it here:

| Appended | Ref |
|---|---|
| assistant response | `session_run.py:1227` (`append_assistant_message`) |
| tool results | `:750` |
| system nudges (wind-down / empty-stop) | `:455`, `:505`, `:530`, `:674` |
| new user turn | `application/session.py:188` |

**Shaping happens inside `call_llm` (`:913`), just before the provider call, on a
copy.** The exact order:

1. Build a per-turn **steering block** from live counters — remaining budget,
   step count, consecutive reads-without-a-write (`:939`, `build_steering_block`).
2. If history ends on a user turn, **fold the steering in place** into that
   message (persisted, `:955`); otherwise let it **ride on the shaped copy only**
   — the model sees it, but it is not saved (`:964`).
3. **`messages = shaper.shape(state.messages)`** — reshape a **copy**; the ledger
   is untouched (`:960`). The docstring states it plainly: *"The shaper reshapes a
   copy for the model's view only; state.messages stays the complete, persisted
   history."*
4. `_complete(messages, …)` → send to the provider (`:976`).
5. **Overflow safety net.** If the provider rejects the call as a context
   overflow, run `forced_shape` (compact every sheddable source unconditionally
   toward target, ignoring the token estimate) and retry **once** (`:982`). If it
   *still* overflows — e.g. the pinned seed alone exceeds the window — raise
   `_ContextOverflowStop` and stop the session gracefully (`:1001`).

Because the shaped list is ephemeral, there is no state to keep consistent
between the model's view and the ledger: every turn projects fresh from the full
history.

---

## 6. What shaping actually does — the 5 rungs

The default pipeline is composed in `bootstrap/container.py:173`, cheapest /
lowest-loss first. The first two rungs are **unconditional** (run every call);
the last three are **reactive** — they no-op until a token estimate crosses the
trigger, then degrade progressively.

| # | Rung (file) | Gate | Action |
|---|---|---|---|
| ① | `EagerToolOutputClearShaper` (`eager.py:127`) | unconditional | Stub every compactable tool result older than the most recent **12** (`DEFAULT_EAGER_KEEP_RECENT`, `:47`); stubs are per-call distinct (carry the file/range already read) and deterministic, so the deep prefix stays cacheable. |
| ② | `PerToolResultBudgetShaper` (`tool_budget.py:12`) | unconditional | Cap any *single* tool result to **16 000 chars** (`DEFAULT_TOOL_RESULT_BUDGET`, `:9`), with a re-read notice. |
| A0 | `ToolOutputClearShaper` (`reactive.py:73`) | reactive | Clear content of old compactable tool results in place, keeping the last **5** (`DEFAULT_TOOL_CLEAR_KEEP_RECENT`, `:29`); keeps the call/answer skeleton. |
| A | `OldHistorySnipShaper` (`reactive.py:143`) | reactive | Delete whole *old tool-exchange groups* oldest-first down to target, keeping the last **4 groups** (`DEFAULT_HISTORY_KEEP_RECENT_GROUPS`, `pipeline.py:17`); never drops user / assistant-text turns. |
| B | `AutoCompactShaper` (`reactive.py:180`) | reactive, **default-off** | Summarize the largest *pinned-free* run of old groups into one visible `[Context auto-compacted …]` marker. `summarizer is None ⇒ identity` (`:198`). |

**Compactable tools** = `{bash, file_read, grep, git_diff, run_tests}`
(`DEFAULT_COMPACTABLE_TOOLS`, `reactive.py:30`).

**Trigger / target math** (`pipeline.py:34`, `history_trigger_target`):

```
trigger = context_window − 20_000 (output reserve) − 13_000 (safety buffer)
target  = trigger × 0.75
# unknown window → fixed fallback: trigger 120_000 / target 90_000
```

The trigger→target gap is deliberate **anti-thrash headroom**: a layer compacts
well below the trigger so the next turn does not immediately re-fire. The reactive
layers each re-estimate the already-reshaped input they receive, so a cheap rung
that pulls the total back under the line lets the expensive rungs stay dormant.

**Pinning** is priority-based (§2): messages with `_ctx.priority ≥ 70` are never
folded. Only rungs ① and B explicitly consult `is_pinned`; the middle three rely
on the structural fact that pinned sources are `role == "user"` context messages
— never tool results, never assistant-tool_calls leaders — so they are protected
implicitly.

**Every rung returns a new list of new dicts and never mutates `state.messages`.**
The persisted transcript keeps the full original history, so a resume is lossless
even after aggressive compaction.

---

## 7. Mental model recap + the honest gaps

Put together, the lifecycle is:

```
[eval harness] concat goal ─▶ SDK boundary (inputs | prompt)
      │
      ▼
§1 Assembly ─▶ build_plan ─▶ system_prompt() + startup_user_messages()
      │                              │
      │                       §2 stamp _ctx{layer,priority}
      ▼                              ▼
§3 Injection ─▶ _build_initial_state ─▶ state.messages = [system, *seed]
      │
      ▼
§5 per-turn loop ─▶ append(assistant/tool/nudge/turn) to the LEDGER
                    │
                    └─ call_llm: steering ▶ §6 shape(COPY) ▶ provider ▶ (forced retry?)
```

Two gaps worth stating out loud, so the model isn't mistaken for more than it is:

- **PROJECT=30 sub-floor ordering is not yet exercised.** With only one
  below-floor layer, `PIN_FLOOR` is the sole live cut. Priority ordering *below*
  the floor becomes real only when memory/RAG lands.
- **Assembly and Shaping speak different vocabularies** — `ContextSource`
  (typed fragment) vs. message dict (`role`/`content`) — bridged by a `_ctx` tag
  that collapses rich provenance into one priority number. Shaping therefore
  *re-infers* "is this compactable?" from tool names. A future unification
  (a single `ContextItem` carrying its own degrade policy through one ledger) is
  sketched separately; the minimal, non-speculative first step is to widen `_ctx`
  from carrying only `priority` to carrying a `policy` — extending the existing
  seam rather than adding a new abstraction. It should **not** be built until a
  second real deferred source justifies it.

---

## Appendix — file:line index

**Assembly** — `bootstrap/context_builder.py`: identity `:116`, team `:125`
(`_team_section :222`), skills `:138` (`_render_skill_catalog :242`), project
`:153`, task `:162`, `build_plan` return `:173`, `build_agent` `:207`.
`domain/context.py`: `_startup` `:92`, `system_prompt` `:98`,
`startup_user_messages` / `_ctx` stamp `:102`/`:111`, `LAYER_PRIORITY` `:48`.
`domain/scheduler.py`: `DelegationTask.render` `:56`.

**Injection** — `bootstrap/container.py`: `_build_initial_state` `:98`, default
pipeline `:173`. `bootstrap/session_factory.py`: repo map `:318`, spawn seed
`:347`/`:369`, lead (no seed) `:392`. `application/session.py`: `messages`
property `:131`, `add_user_message` `:188`.

**SDK boundary** — `sdk/client.py`: `OpenCollab.agent`, `team`, and `workflow`.
`bootstrap/programmatic.py`: shared composition and lifecycle evidence.

**Per-turn loop** — `application/session_run.py`: `run_loop` `:286`, `call_llm`
`:913`, steering `:939`/`:955`/`:964`, `shape` `:960`, `_complete` `:976`,
`forced_shape` retry `:982`, `_ContextOverflowStop` `:1001`, append assistant
`:1227`, append tool `:750`, system nudges `:455`/`:505`/`:530`/`:674`.

**Shaping** — `application/shaping/pipeline.py`: `PIN_FLOOR` `:24`,
`ctx_priority` `:171`, `is_pinned` `:177`, `history_trigger_target` `:34`,
`pinned_free_region` `:183`, `keep_recent_groups` `:17`. `eager.py`:
`EagerToolOutputClearShaper` `:127`, keep-12 `:47`. `tool_budget.py`:
`PerToolResultBudgetShaper` `:12`, 16k cap `:9`. `reactive.py`:
`ToolOutputClearShaper` `:73`, `OldHistorySnipShaper` `:143`,
`AutoCompactShaper` `:180`, keep-5 `:29`, compactable tools `:30`.
