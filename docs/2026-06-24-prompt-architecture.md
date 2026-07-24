# OpenCollab Prompt Fragments — workflow path, compaction & steering

> **Historical architecture note.** The concepts remain useful, but branch and
> `file:line` references below may have drifted. Current public behavior is
> defined by the package API, tests, and maintained README files.

> **What.** The LLM-facing prompt fragments that are *not* covered by the context
> lifecycle chapter: the one-shot **workflow** agent path, **structured-output**
> enforcement, the **compaction summarizer** prompt, the per-turn **steering**
> block, and the **precheck** stop gates.
>
> **For the core model** — assembly, the `_ctx` handshake, injection, the eval
> goal via the SDK boundary, the per-turn loop, and the 5 shaper rungs — see
> **`2026-07-16-context-lifecycle.md`**. That chapter supersedes this file's
> original §0–§5 (the pre-S3 four-axis / `LoadTiming` / 7-layer model, now
> deleted).
>
> **Status.** Original 2026-06-24; **revised 2026-07-16** and slimmed to the
> sections above after Lane S3. Grounded in code; `file:line` current as of
> the post-S3 implementation at the time of writing.

---

## 0. Scope — what moved out

The original version of this doc classified *every* prompt fragment under a
four-axis model (`Timing × Position × Layer × Persistence`). Lane S1/S3 deleted
the machinery that abstraction described — the `LoadTiming` axis (lazy loaders),
the `TOOL_META`/`MEMORY` layers, and two identity shapers — so that framing no
longer matches the code and has been removed. The assembly → injection → shaping
story now lives, code-accurate, in the lifecycle chapter.

What remains below is the set of fragments that chapter deliberately does *not*
detail, because they are side paths (workflows), one specific tool's prompt
(the summarizer), or the exact text/thresholds of the per-turn nudges.

---

## 1. The workflow one-shot path

A team/chat session layers `lead.md`/`role.md` + team + skills + project into its
system prompt (see the lifecycle chapter §1). **Workflow agents do not** — every
`ctx.agent()` call builds a *fresh, minimal* one-shot session so that
`parallel()`/`pipeline()` can spin up dozens cheaply. Its system prompt is a fixed
three-sentence constant (`bootstrap/_workflow_runtime_state.py:5`):

```python
WORKFLOW_AGENT_PROMPT = (
    "You are an autonomous agent invoked as one step of a larger workflow. "
    "Complete the task described in the user message. Use your tools as needed. "
    "Be concise and finish with a clear final answer."
)
```

The session is seeded with it as the system message
(`bootstrap/_workflow_runtime_session.py:113`); the actual work is the `prompt`
argument the workflow supplies, which becomes the first **user** message. There is
no identity/team/skill layering — this is a parallel, deliberately terse path.

### Structured output enforcement

When a `ctx.agent(prompt, schema=…)` call requires a validated object, a two-pass
protocol forces it (`application/workflow_structured.py`):

1. **Seed.** The first pass appends `_STRUCTURED_INSTRUCTION` (`:25`) to the
   prompt — *"Finish by calling the `structured_output` tool — do not answer in
   free text."* (`seeded_prompt = prompt + _STRUCTURED_INSTRUCTION`, `:124`).
2. **Corrective retry.** If the first pass yields no valid payload, a retry
   session is restricted to the single capture tool and seeded with
   `_STRUCTURED_RETRY` (`:38`, applied at `:214`) — a MUST-call/no-prose
   imperative telling the model to commit *now* from what it already gathered.
   `tool_choice` is forced to the **named** function via `_named_tool_choice`
   (`:45`), stricter than a bare `"required"`.

The tool is `structured_output` (`application/structured_output.py:26`). The
forcing is **graceful, not guaranteed**: an endpoint may 400-reject a named
`tool_choice` and degrade to `"auto"`, after which the model can still answer in
prose — hence the imperative wording rather than relying on the API alone.

---

## 2. The compaction summarizer prompt

The `AutoCompactShaper` (rung B, **default-off**; see lifecycle §6) is the only
place a model is asked to *summarize* history. When active, it hands the droppable
span to a summarizer invoked with OpenCollab's `BASE_COMPACT_PROMPT`
(`application/compaction_prompt.py`):

```
Goal                       User directions              Completed work
Technical state            Decisions and constraints    Failures and diagnostics
Remaining work             Immediate next action
```

Two fields guard continuity. **User directions** preserves every human or
teammate instruction in order and makes later corrections supersede earlier
ones. **Immediate next action** anchors the handoff to the latest unfinished
request.

The instruction is wrapped by `NO_TOOLS_PREAMBLE` and `NO_TOOLS_TRAILER`, then
assembled by `get_compact_prompt`. The model returns one `<summary>` block.
`format_compact_summary` unwraps it and accepts legacy `<analysis>` prefixes so
stored sessions remain readable. `build_summary_request` and
`build_continuation_message` frame the request and response.

> This is the very prompt shape that produces the `[Context auto-compacted …]`
> markers — and the session summaries the harness itself emits between context
> windows.

---

## 3. The per-turn steering block

Steering is a pure, import-free module (`application/steering.py`, extracted in
Lane S1) — the run loop gathers live counters and asks it for a block plus any
`tool_choice` force. *How* the block attaches (folded into a trailing user turn
and persisted, else ridden ephemerally on the shaped copy) is covered in the
lifecycle chapter §5; what it **contains** is here.

Every turn `build_steering_block` (`:22`) emits a lean budget-awareness line
(`:51`):

```
[Budget: ~{remaining}k/{total}k tokens left, ~{steps_left} steps left.]
```

built fresh from live counters, plus a **reads-without-write** escalation gated on
two thresholds (`READS_NUDGE_SOFT = 8`, `READS_NUDGE_HARD = 16`, `:18`):

| Condition | Added clause | `tool_choice` | level |
|---|---|---|---|
| `has_write` & `reads ≥ 16` | *"STOP reading — your next action MUST be a file_write or apply_patch edit."* | `"required"` | hard (`:56`) |
| `has_write` & `reads ≥ 8` | *"If you can describe the fix, make it now … before reading more."* | — | soft (`:63`) |
| `has_structured_output` & `reads ≥ 8` | *"STOP reading — your next action MUST be structured_output …"* | named `structured_output` | hard (`:70`) |

`reads` (`reads_since_last_edit`) resets to 0 on a successful write, so on a fresh
post-user turn only the status line is emitted. `fold_steering` (`:81`) returns a
copy of the trailing user message with the line folded in (string or content-part
form); it never mutates the original.

---

## 4. Precheck stop gates

Before spending a model call, `precheck` (`application/session_run.py:556`) runs a
fixed ladder of guards. Each guard **appends a visible system message** and stops
the session via `_stop_precheck(reason)` — a single `STOPPED` terminal carrying a
reason string (Lane S1 collapsed the former per-cause terminal phases into one
`STOPPED(reason)`). In order:

1. **Cancellation** — `cancel_event` set → `"[Session interrupted by user]"` (`:563`).
2. **Loop block** — `loop_blocked_since_progress ≥ DEFAULT_LOOP_BLOCKED_LIMIT`
   (repeated identical tool calls) → stop with a loop-block reason (`:570`).
3. **Enforcement gate** — `_apply_enforcement_gate()` may stop the turn (`:578`).
4. **Token budget** — `used_tokens ≥ max_budget_tokens` (`:581`).
5. **Team budget** — the aggregate ceiling: even if this session is under its own
   cap, stop when the *team total* reaches the global cap (`:590`).
6. **Step limit** — `step_count ≥ max_steps` (`:595`).

If every guard passes it transitions to `CALLING_LLM` (`:600`). **Context overflow
is handled separately**, downstream in `run_llm_call` (`:619`): `call_llm` already
force-compacts and retries once (lifecycle §5); if the retry still overflows it
raises `_ContextOverflowStop`, caught here and degraded to a graceful
`CONTEXT_OVERFLOW` stop (`_stop_on_context_overflow`) rather than an unhandled
error.

---

## Appendix — file:line index

| Concept | File | Line |
|---|---|---|
| `WORKFLOW_AGENT_PROMPT` | `bootstrap/_workflow_runtime_state.py` | 5 |
| Workflow session seed | `bootstrap/_workflow_runtime_session.py` | 113 |
| `_STRUCTURED_INSTRUCTION` / seed | `application/workflow_structured.py` | 25 / 124 |
| `_STRUCTURED_RETRY` / retry | `application/workflow_structured.py` | 38 / 214 |
| `_named_tool_choice` | `application/workflow_structured.py` | 45 |
| `structured_output` tool name | `application/structured_output.py` | 26 |
| `NO_TOOLS_PREAMBLE` / `NO_TOOLS_TRAILER` | `application/compaction_prompt.py` | current definitions |
| `BASE_COMPACT_PROMPT` (8 fields) | `application/compaction_prompt.py` | current definition |
| `get_compact_prompt` | `application/compaction_prompt.py` | current definition |
| `format_compact_summary` | `application/compaction_prompt.py` | current definition |
| `build_continuation_message` / `build_summary_request` | `application/compaction_prompt.py` | current definitions |
| `READS_NUDGE_SOFT` / `READS_NUDGE_HARD` | `application/steering.py` | 18 / 19 |
| `build_steering_block` / status line | `application/steering.py` | 22 / 51 |
| hard / soft / structured nudge | `application/steering.py` | 56 / 63 / 70 |
| `fold_steering` | `application/steering.py` | 81 |
| `precheck` ladder | `application/session_run.py` | 556 |
| cancel / loop / enforcement / budget / team / step | `application/session_run.py` | 563 / 570 / 578 / 581 / 590 / 595 |
| overflow → `_stop_on_context_overflow` | `application/session_run.py` | 619 |
