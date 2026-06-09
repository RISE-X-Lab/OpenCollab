# Plan: Port `context-compaction-py` ideas into OpenCollab

**Status:** Implemented 2026-06-09 (Option B; all three steps)
**Author:** prep session 2026-06-09
**Scope:** Improve OpenCollab's compaction quality by natively porting two ideas
from the `context-compaction-py` library. **Do not vendor the library.**

> **Implementation note (2026-06-09):** Landed via **Option B** (read-time path),
> all three steps. New: `application/compaction_prompt.py` (9-section prompt +
> parser + continuation builder), `application/compaction_summary.py`
> (`ReadTimeSummarizer` async→sync bridge), `ToolOutputClearShaper` +
> `history_trigger_target` in `application/shaping.py`, and
> `model_context_window` in `adapters/llm.py`. The mutating
> `ContextCompactionUseCase` is retired via `SessionRunUseCase(compaction_enabled=False)`
> in `bootstrap/container.py`; `AutoCompactShaper` is now the sole active
> summarizer. 30 new unit tests; full suite 443 passing.

---

## TL;DR

OpenCollab already has structural equivalents to *both* of the library's layers,
so dropping the package in would duplicate infrastructure (a second `LLMClient`
abstraction, a second config, a second message model) and fight the clean
architecture. Two pieces of the library are genuinely better than what's here and
worth extracting **natively** (reimplemented against existing ports):

1. **The summary prompt** — the real Claude Code 9-section prompt. *High value,
   low cost.* Pure strings; no message-model conversion, no async issue.
2. **Clear-in-place tool-output strategy** — clear a tool result's *content* to a
   placeholder while keeping the call/answer skeleton. *Medium value.* Less lossy
   than the current whole-group deletion.

A third, optional refinement: replace fixed token thresholds with the library's
`effective_context_window` math.

---

## Background: why not vendor the library

`context-compaction-py` (`/home/xuzhenhua/git/context-compaction-py`) is a
provider-agnostic port of Claude Code's two compaction mechanisms:

| Library | Function | OpenCollab equivalent | Status today |
|---|---|---|---|
| Tool-output compaction | `microcompact_if_needed` | `OldHistorySnipShaper` + `PerToolResultBudgetShaper` | wired |
| Full compaction | `compact_conversation` | `ContextCompactionUseCase.compact()` | wired & **active** |
| Summarizer slot | `AutoCompactShaper` summarizer | `AutoCompactShaper(summarizer=None)` | dormant |
| LLM abstraction | `LLMClient` ABC | `application/ports.py:LLMPort` | duplicate |
| Message model | typed `Message`/`ContentBlock` | OpenAI-style dicts | **incompatible** |

Three hard frictions make wholesale adoption a net loss:

1. **Message model mismatch.** The library models tool calls/results as typed
   blocks (`ToolUseBlock`/`ToolResultBlock`). OpenCollab uses OpenAI dict messages
   with separate `role:"tool"` entries keyed by `tool_call_id`. Every library
   entry point would need a bidirectional adapter.
2. **Sync vs async.** `LLMClient.complete` is sync; OpenCollab's `LLMPort.complete`
   and `ContextCompactionUseCase` are async. The library's value-add
   (`compact_conversation`) can't drop into the async loop without a sync-in-async
   bridge.
3. **Clean-arch placement.** The library carries its own port + config + domain
   types; vendoring re-introduces a parallel stack the repomap explicitly
   centralizes in `application/ports.py` + `domain/`.

**Conclusion:** extract the *ideas* (prompt text, clearing strategy, trigger
math), reimplement against OpenCollab's existing dict model and async ports.

---

## Decision required FIRST (blocks step 1's landing site)

OpenCollab runs **two** compaction mechanisms in parallel:

- **Mutating path** — `ContextCompactionUseCase` (`application/compaction.py`).
  Async, rewrites `state.messages` → `[system, summary, ...recent]`. Fires today
  via the run-loop FSM: `precheck` → `should_compact()` →
  `SessionPhase.COMPACTING` → `run_compaction()`
  (`application/session_run.py:167`, `:173`). **This is the live path.** Uses the
  weak 3-line prompt (`compaction.py:96-107`).
- **Read-time path** — `ShaperPipeline` in `SessionRunUseCase.call_llm`, over a
  *copy* of history (transcript stays full → lossless resume). Contains
  `AutoCompactShaper`, currently identity because `summarizer=None`
  (`bootstrap/container.py:312`).

The two overlap. Before porting the prompt, **decide where it lands**:

- **Option A — mutating path** (`ContextCompactionUseCase`). Smallest diff; the
  prompt improves what fires today. But mutating path is lossy (original messages
  are replaced in `state.messages`).
- **Option B — read-time path** (`AutoCompactShaper`). Lossless resume is a stated
  design value (`shaping.py` module docstring). Wire a (sync-wrapped) summarizer
  into the dormant shaper, then retire/disable the mutating `ContextCompactionUseCase`.

**Recommendation: Option B**, but it's a larger refactor (consolidating the two
paths). If you want the quick win, do Option A first (prompt only), and treat the
consolidation to B as a follow-up. Either way, **do not leave both summarizers
active with different prompts.**

---

## Work items

### Step 1 — Port the summary prompt (HIGH value, LOW cost)

Source: `context-compaction-py/context_compaction/prompt.py`.

What the library's prompt has that OpenCollab's lacks:
- 9 named sections (vs 3 generic lines).
- `<analysis>` scratchpad / `<summary>` output split, with a parser that strips
  the scratchpad (`format_compact_summary`).
- **Section 6 "All user messages"** and a **verbatim next-step quote** — the two
  things that prevent task drift after a compact.
- A continuation message with an optional `transcript_path` recovery pointer
  (`build_continuation_message`).

Tasks:
- [ ] Add a `compaction_prompt.py` (or extend `application/compaction.py`) holding
      the 9-section prompt + `format_compact_summary` regex parser +
      `build_continuation_message`. Reword sections for OpenCollab's domain but
      **keep section 6 and the verbatim next-step quote**.
- [ ] **Landing site per the decision above:**
  - Option A: replace `ContextCompactionUseCase.build_compaction_prompt` system
    text; run the `<analysis>`/`<summary>` parser in `call_compaction_llm`;
    rebuild `build_compacted_messages` using the continuation message (with the
    transcript path, available from the tracer/session store).
  - Option B: provide a sync summarizer closure to `AutoCompactShaper` in
    `bootstrap/container.py:312` that calls the LLM with this prompt.
- [ ] Provenance: copy prompt text with an attribution comment pointing at the
      source repo / Claude Code.
- [ ] Tests: prompt contains all 9 sections; parser strips `<analysis>` and
      unwraps `<summary>`; continuation message includes the transcript pointer
      when a path is supplied; falls back gracefully when the model returns no
      `<summary>` block.

### Step 2 — Clear-in-place tool-output shaper (MEDIUM value, OPTIONAL)

Source idea: `context-compaction-py/context_compaction/tool_output.py`
(`clear_old_tool_outputs`).

Today `OldHistorySnipShaper` (`application/shaping.py:198`) deletes *whole*
tool-exchange groups. Clearing-in-place instead keeps the call/answer skeleton and
the assistant's reasoning, replacing only the bulky tool *content* with a
placeholder — less lossy, zero orphan risk.

Tasks:
- [ ] Add `ToolOutputClearShaper` to `application/shaping.py`: for each compactable
      tool result older than the last N, replace `content` with a placeholder
      (e.g. `[Old tool result content cleared]`). Operate directly on the dict
      model (`role:"tool"` messages); reuse `_group_spans` for grouping.
- [ ] Drive `compactable_tools` from the real tool names in
      `bootstrap/container.py:TOOL_REGISTRY` (`Bash`, `FileRead`, `Grep`,
      `GitDiff`, `RunTests`, …) — **not** the library's hardcoded set.
- [ ] Slot it **before** `OldHistorySnipShaper` in the pipeline
      (`bootstrap/container.py:308`) — cheaper / lower-loss runs first, matching
      the lazy-degradation ordering the `shaping.py` docstring already documents.
- [ ] Tests: clears only compactable tools beyond keep-recent; never orphans a
      `tool_call_id`; idempotent (re-clearing a placeholder is a no-op); produces a
      new list (immutability).

### Step 3 — Trigger math refinement (LOW value, OPTIONAL)

Source idea: `effective_context_window = window − reserved_output`,
`threshold = window − buffer` (`compaction.py:effective_context_window`).

Replace the fixed constants (`DEFAULT_COMPACTION_THRESHOLD = 64_000`,
`DEFAULT_HISTORY_TRIGGER_TOKENS = 120_000`) with values derived from the active
model's real context window / max-output (already reachable via `LLMPort` /
`adapters/llm.py`).

Tasks:
- [ ] Add a helper computing the threshold from the model's window + an
      output-reserve buffer; feed both `ContextCompactionUseCase` and the reactive
      history shapers.
- [ ] Tests: threshold scales with the model window; degrades to a sane default if
      the window is unknown.

---

## Out of scope / explicit non-goals

- Adding `context-compaction-py` as a dependency or vendoring its source.
- Introducing the library's typed `Message`/`ContentBlock` model.
- Image/document tool-result handling (the library itself stubs this).

## Reference files

OpenCollab:
- `opencollab/opencollab/application/compaction.py` — mutating compaction
- `opencollab/opencollab/application/shaping.py` — read-time shaper pipeline
- `opencollab/opencollab/application/session_run.py:167-180` — FSM compaction hook
- `opencollab/opencollab/bootstrap/container.py:292-316` — wiring (compactor + pipeline)

Library (read-only reference, do not import):
- `context-compaction-py/context_compaction/prompt.py` — the prompt to port
- `context-compaction-py/context_compaction/tool_output.py` — clearing strategy
- `context-compaction-py/context_compaction/compaction.py` — trigger math
