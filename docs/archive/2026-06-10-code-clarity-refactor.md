# Plan: Code Clarity & Modularity Refactor

**Status:** Implemented on 2026-06-10 — all phases including optional 3d;
`RuntimeLimits` param-grouping skipped (would ripple through `build_session`'s
many callers)
**Goal:** Make the *internals* of the code files clean and clear — smaller files,
shorter functions, one responsibility per module, fewer magic numbers, better
names and docstrings — **without changing behavior or the public API**.

> The cross-layer architecture (domain → application → adapters → bootstrap) is
> already clean and test-enforced (`test_application_boundaries.py`,
> `test_domain_boundaries.py`). This plan does **not** restructure layers; it
> cleans up *within* modules.

---

## Guardrails (apply to every step)

1. **Behavior-preserving.** No logic changes. Pure extraction / rename / move.
2. **Green after each step.** Run `cd opencollab && .venv/bin/python -m pytest -q`
   (baseline: **443 passed**) and `.venv/bin/ruff check opencollab/` after every
   commit. Never batch multiple risky moves into one commit.
3. **Preserve public surfaces.** Keep every name re-exported where it lives today
   (e.g. `from opencollab.adapters.llm import LLMClient` must keep resolving — use
   a package `__init__.py` re-export when splitting a module into a package).
4. **One concern per commit**, conventional-commit messages, on a feature branch.
5. **Update `docs/repomap/REPOMAP.md`** in the same commit whenever a module is
   split or moved.

---

## Severity-ranked findings (evidence)

| Sev | File | Issue | Evidence |
|-----|------|-------|----------|
| P0 | `adapters/llm.py` | 3 ruff errors: dead var + 2 long lines | `F841 start` @203, `E501` @430 (127ch); `E501` `cli/main.py:183` (129ch) |
| P1 | `adapters/llm.py` (475) | **Two providers in one class** — `_complete_openai`/`_complete_anthropic`, `_stream_openai`/`_stream_anthropic`, `_convert_to_anthropic_messages`, dual `_openai`/`_anthropic` handles | grep of `provider=="anthropic"` branches |
| P1 | `bootstrap/container.py` | `build_session_runtime` = **143 lines, 18 kw-params**, nested async defs | longest function in repo |
| P2 | `adapters/tui/renderer.py` (491) | One `TUI` class, ~30 methods, 3 concerns (events / display / live) | outline |
| P2 | `adapters/cli/main.py` (396) | toolbar + config-resolve + eval + REPL in one file | outline |
| P2 | `adapters/tools/apply_patch.py` (334) | thin tool + a real diff engine inline | `_parse_hunks`/`_find_block`/`_apply_unified_diff`/`_apply_line_replace` |
| P3 | `application/shaping.py` (415) | cohesive but big; one-class-per-file aids navigation | 6 classes + 4 helpers |
| P3 | repo-wide | deep nesting (`llm` 27, `fs` 24 lines ≥5 levels); ~30 magic numbers; 210/361 public funcs lack docstrings | scans |

`application/session_run.py` (378) is intentionally a method-per-phase FSM and is
already well-decomposed — **leave it** except the one 62-line method noted in P3.

---

## Phase 0 — Lint & dead code *(15 min, zero risk)*

Fix the 3 ruff violations; nothing else.

1. `adapters/llm.py:203` — remove unused `start` (or wire it into the latency it
   was presumably meant to measure; check `_complete_*` for an existing timer
   before deleting).
2. `adapters/llm.py:430` — wrap the 127-char `json.loads(...)` line.
3. `adapters/cli/main.py:183` — wrap the 129-char line.

**Done when:** `ruff check opencollab/` → "All checks passed!", suite green.
**Commit:** `style: fix ruff F841/E501 in llm and cli`

---

## Phase 1 — Split the two-provider LLM client *(highest modularity value)*

Turn `adapters/llm.py` into a small package so each provider lives alone.

```
adapters/llm/
  __init__.py        # re-export LLMClient, LLMResponse, Usage, StreamDelta,
                     # model_context_window  (keeps existing import paths working)
  types.py           # LLMResponse, Usage, StreamDelta, model_context_window
  retry.py           # _with_retry, _is_retryable_error, _extract_retry_after_seconds
  client.py          # LLMClient: __init__ provider switch + complete/stream dispatch
  openai_provider.py # _complete_openai, _stream_openai
  anthropic_provider.py # _complete_anthropic, _stream_anthropic, _convert_to_anthropic_messages
```

Suggested approach: keep `LLMClient` as the single public class, but move each
provider's request-build/parse into a small provider module that `client.py`
calls. The Anthropic message-conversion function moves next to its provider and
gets decomposed into per-role handlers (`_convert_assistant`, `_convert_tool_result`)
to kill the deepest nesting in the repo.

**Watch:** `tests/` and `bootstrap/container.py` monkeypatch/import `LLMClient`
(and `container.LLMClient`). The `__init__.py` re-export must preserve
`opencollab.adapters.llm.LLMClient` exactly. Search before moving:
`grep -rn "adapters.llm" opencollab tests`.

**Done when:** import paths unchanged, suite green, each new file < ~150 lines.
**Commits:** one per file-move group (types → retry → providers → client).

---

## Phase 2 — Decompose `build_session_runtime`

Break the 143-line factory into named sub-builders so the top level reads as a
sequence of intent-revealing steps:

- `_resolve_llm(agent, llm, llm_timeout) -> LLMPort`
- `_build_summarizer(agent, llm, resolved_llm, llm_timeout, auto_save_path) -> ReadTimeSummarizer`
  (owns the two nested async `_summary_complete` closures)
- `_build_default_shaper(resolved_llm, summarizer) -> ShaperPort`
  (owns the trigger/target + pipeline assembly)

Then `build_session_runtime` becomes ~40 lines of `x = _build_x(...)`.

**Optional (separate commit):** collapse the 18-kw-param signature by grouping
the resource caps into a small frozen dataclass, e.g.
`RuntimeLimits(max_budget_tokens, max_steps, compaction_threshold, llm_timeout)`.
Only do this if it doesn't ripple into many call sites — `grep -rn build_session_runtime`
first; if there are >5 callers, skip to keep the change small.

**Done when:** function bodies each < 50 lines, suite green.
**Commit:** `refactor: extract llm/summarizer/shaper builders from build_session_runtime`

---

## Phase 3 — Split the remaining oversized modules

Apply the same behavior-preserving pattern (extract cohesive seams; for one big
class, use mixins like `scheduler.py` did so `self` is unchanged).

### 3a. `adapters/tui/renderer.py` (491) — mixin split of `TUI`
- `renderer_events.py` — `_RendererEventsMixin`: `event_handler`,
  `_handle_session_event`, `_handle_scheduler_event`, `_mark_roster`,
  `_append_activity`, `_emit_status`, `_clear_thinking_status`, `_flush_*`.
- `renderer_display.py` — `_RendererDisplayMixin`: `_team_entries`,
  `_build_team_panel`, `_build_display`, `_build_live_display`, `_roster_state`,
  `_args_preview`, `_agent_label`, `_is_visible`.
- `renderer.py` keeps `TUI(__init__ + live lifecycle: start/suspend/resume/stop_live,
  select_agent, set_filter)` and composes the mixins.
- Note: `_LineViewport` is already a standalone helper — could move to
  `renderer_display.py`.

### 3b. `adapters/cli/main.py` (396) — split by concern
- `cli/toolbar.py` — `_format_team_toolbar`, `_display_team_state`, `_toolbar_style`.
- `cli/config_resolve.py` — `_resolve_config`, `_required_env_key`,
  `_missing_api_key`, `_print_missing_key_hint`, `_required_env_key`.
- `cli/eval.py` — `eval_cmd` + `_eval` (the headless eval command).
- `main.py` keeps the Typer app, `main_callback`, the chat REPL (`_run`, `turn`,
  `team_toolbar`), and `main()`.
- Move the existing `tests/test_cli_toolbar.py` expectations’ import target if it
  imports the toolbar helper from `main`.

### 3c. `adapters/tools/apply_patch.py` (334) — extract the engine
- `apply_patch_engine.py` — `_split_lines`, `_join_lines`, `_summary`,
  `_apply_line_replace`, `_parse_hunks`, `_find_block`, `_apply_unified_diff`.
- `apply_patch.py` keeps `ApplyPatchTool` (thin: parse params → call engine →
  format result). Engine becomes independently unit-testable.

### 3d. `application/shaping.py` (415) — `shaping/` package *(optional, P3)*
- `shaping/pipeline.py` — `ShaperPipeline`, `history_trigger_target`,
  `approx_messages_tokens`, span/region helpers (`_group_spans`, `_droppable_region`).
- `shaping/tool_budget.py` — `PerToolResultBudgetShaper`.
- `shaping/reactive.py` — `_ReactiveHistoryShaper` + `ToolOutputClearShaper`,
  `OldHistorySnipShaper`, `AutoCompactShaper`, `ContextCollapseShaper`.
- `shaping/__init__.py` re-exports everything `container.py` imports today.
- Lower priority — it's already one cohesive concern; do last or skip.

**Done when:** each resulting file < ~300 lines, suite green after each sub-step,
REPOMAP updated.
**Commits:** one per file (3a, 3b, 3c, 3d).

---

## Phase 4 — Clarity pass *(low risk, do opportunistically)*

Not a big-bang; fold into the phases above as you touch each file.

1. **Deep nesting → guard clauses / extraction.** Primary targets:
   `llm` message conversion (handled in Phase 1), `fs.py:execute_with_runtime`
   (FileWrite/Grep) — invert conditions, early-return.
2. **Magic numbers → named constants.** ~30 across the code (e.g. `12_000`/`6_000`
   diff-truncation in `scheduler.py:_append_worktree_diff`, `200_000`/`100`/`600.0`
   defaults, retry backoff numbers in `llm/retry.py`). Hoist to module-level
   `UPPER_CASE` constants with a one-line comment on the *why*.
3. **Docstrings on public methods.** Be selective — **skip** `application/ports.py`
   (Protocol stubs are self-documenting) and trivial one-liners. Prioritize the
   non-obvious behavior: `session_run.py` phase methods, `tool_execution.process`,
   `shaping` shapers, `apply_patch_engine` functions. Target the ~80 that carry
   real behavior, not all 210.
4. **Names.** Spot-fix abbreviations only where they hurt (`m`, `tc`, `n` loop
   vars in `llm` conversion → `message`, `tool_call`). Don't rename domain terms
   that have a glossary meaning (`aid`, `scb`).

**Commit:** small, per-file `refactor: …` / `docs: add docstrings to …`.

---

## Suggested order & rationale

1. **Phase 0** — clears the only hard failures (lint); unblocks a clean baseline.
2. **Phase 1** — biggest single modularity payoff and kills the worst nesting.
3. **Phase 2** — makes the composition root readable; small and self-contained.
4. **Phase 3a–3c** — independent, parallelizable; pick by what you touch most.
5. **Phase 3d / Phase 4** — polish; do as energy allows.

Each phase is independently shippable — stop anywhere and the repo is still green.

## Definition of done (whole effort)

- [ ] `ruff check opencollab/` → clean; `pytest -q` → 443+ passed.
- [ ] No source file > ~350 lines (currently 6 are; target: 0 over 400).
- [ ] No function > ~60 lines (currently `build_session_runtime` 143 is the outlier).
- [ ] `adapters/llm` is one provider per file behind a stable `LLMClient` facade.
- [ ] Public import paths unchanged (`grep`-verify before/after each split).
- [ ] `REPOMAP.md` reflects every new module.

## Explicitly out of scope

- Flattening the `opencollab/opencollab/` nesting (large, risky, low payoff).
- Any behavior/logic change, new feature, or dependency-direction change.
- Touching `application/session_run.py`'s FSM decomposition (already clean).
```
