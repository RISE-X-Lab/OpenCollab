---
title: CL4R1T4S coding-prompt mining → OpenCollab absorption report
date: 2026-06-15
source: github.com/elder-plinius/CL4R1T4S (leaked/reverse-engineered system prompts — patterns only, no verbatim reuse)
method: 23-agent mine + synthesis workflow; 282 patterns from 24 files
---

# OC Prompt-Engineering Absorption Report

## 1. Coverage
282 patterns mined from ~38 leaked system-prompt files across 13 vendors: Anthropic (Claude Code), Cursor (2.0 + Tools), Devin (2.0 + Commands), Cline, Windsurf (Prompt + Tools), OpenAI Codex (2 versions), Replit (Agent + Functions + Init-Gen), Factory Droid, Bolt, Lovable, Vercel v0, Manus (Prompt + Functions), Grok-Code, Dia, Same.dev. All entries carried a usable `source`; none failed to fetch (corpus supplied inline). Heavy convergence: read-before-edit, bounded-retry, verify-before-report, and exact-unique-match each appear in 6+ independent vendors.

## 2. What OC already does well (do NOT redo)
Mapped against OC's actual surfaces — these patterns are already implemented:

- **Never-repeat-failed-task / findings-forward** — `workflows/self_collab.py:216-219`, `split_solve.py:217-220` fold tester findings into the next coder round; never re-issues identical task. Also `configs/team.self.collab.yaml:84-86`.
- **Bounded retry + stop-on-failed-phase** — `self_collab.py:26` (`MAX_ROUNDS_PER_PHASE=3`), `:291-294` (halt rather than build on broken base).
- **Never edit tests / root-cause / smallest change** — `SHARED_RULES` in `self_collab.py:34-35`, `split_solve.py:45-46`, `configs/team.self.collab.yaml:26-30`.
- **Prefer dedicated tool over bash** — `SHARED_RULES` `self_collab.py:31-33`; `CODER_PROMPT:128-133`.
- **Verify-before-report** — coder must `run_tests` before reporting (`self_collab.py:128-133`); adversarial tester re-inspects source, distrusts coder summary (`:152-168`); structured `VERDICT_SCHEMA` (`:66-76`).
- **Exact-unique-match edits** — `adapters/tools/fs.py:158-163` (rejects 0 or >1 matches with actionable error).
- **Destructive-command block + risky-confirm** — `adapters/safety.py:21-36, 80-107`.
- **Output truncation to protect context** — `adapters/tools/bash.py:19,70-71`; `run_tests.py:28-29`.
- **Narrow-range reads** — `DEFAULT_LEAD_PROMPT` `team_config.py:78-80`, `DEFAULT_ROLE_PROMPT:87-89`; `fs.py` offset/limit params.
- **Plan/execute separation + plan review** — analyst-only-analyze (`self_collab.py:79`), two-lens parallel plan review before any code (`:245-284`).
- **Structured run_tests signal over prose** — `run_tests.py:1-16,49-55`.

## 3. Top transferable patterns (prioritized, deduped)

| Pattern | Seen in (vendors) | OC target file:line | Priority | Concrete change |
|---|---|---|---|---|
| **Read-before-edit gate in tool description** | Cursor, Devin, Cline, Same, Droid, Manus, Replit (7) | `adapters/tools/fs.py:87-92` (`FileWriteTool.description`) | **P0** | Add to `str_replace` blurb: "Read the target span with file_read first; edits land on current content." Cheap, highest convergence, prevents stale-anchor failures. |
| **"Use-when X vs Y" routing baked into tool docs** | Cursor, Devin, Windsurf, Replit, Manus, Codex, Dia (7) | `fs.py:25-28,174-177`; `bash.py:31-34`; `run_tests.py:49-55` | **P0** | Append a one-line routing hint per tool: grep="locate symbols/strings, not whole-file reads"; bash="only when no dedicated tool fits"; file_read="prefer ranged reads on large files". Reaches the LLM as function-schema even though OC skips the tool-meta prose layer (`context_builder.py:137-147`). |
| **Non-interactive + no-`cd` bash discipline** | Cursor, Windsurf, Manus, Replit (4) | `bash.py:31-34` description; `SHARED_RULES` | **P0** | Add to bash description: "Headless — no TTY. Use `-y`/`--yes`, pipe pagers to `cat`, never block on prompts. Use absolute paths; don't rely on `cd` persisting." OC already notes cwd resets but never tells the model. |
| **Never assume a library exists / verify imports before claiming done** | Claude Code, Devin, Replit (Lovable build-gate) (4) | `SHARED_RULES` (`self_collab.py:29-35`) | **P1** | One line: "Before importing a package, confirm the repo already uses it (grep/manifest). Verify imports resolve before reporting." Directly lifts SWE-bench pass rate. |
| **Tri-state verdict: PASS / FAIL / BLOCKED-by-env** | Codex (both), Droid, Devin, Manus (5) | `VERDICT_SCHEMA` `self_collab.py:66-76`, `split_solve.py:68-78` | **P1** | Add `"BLOCKED"` enum value + honest disclaimer so env failures (missing dep, no network) don't masquerade as code FAIL and don't loop. Lets the workflow route differently. |
| **Escalate honestly after N failures (no fabrication)** | Devin, Replit, Same, Bolt, Manus, Cursor (6) | `_run_phase` `self_collab.py:221`; `SHARED_RULES` | **P1** | On final round failure, require a structured "blocked: <evidence>" report rather than an optimistic summary. Pairs with the BLOCKED verdict; reinforces the documented lead budget-loop fix. |
| **Verbosity cap on agent reports** | Claude Code, Cline, Windsurf, Dia, Droid, Replit (6) | coder/tester prompts (`self_collab.py:128-168`) | **P1** | Add to SHARED_RULES: "Report in ≤8 lines: changed files + edits, why, verification result. No preamble/postamble." Trims tokens every turn under budget. |
| **apply_patch fallback fully specified (anchored diff)** | Devin, Cline, Windsurf, Aider-style (4) | `CODER_PROMPT:128-133`; `adapters/tools/apply_patch.py:46+` | **P1** | The YAML (`team.self.collab.yaml:99-104`) explains the str_replace→apply_patch fallback richly; the deterministic workflow prompt does not. Port the "if str_replace fails twice, use apply_patch line_replace with expected_str guard" detail into `CODER_PROMPT`. |
| **Gather info before concluding root cause (don't react instantly)** | Devin (both), Dia, Manus, Cursor (5) | `ANALYST_PROMPT:78-89`; coder findings handling | **P2** | Already partly present; strengthen: on a recurring failure, enumerate 2-3 candidate causes and pick one rather than re-attempting the same edit. |
| **Edits ripple — update related files/callers** | Replit, Devin, Cursor (3) | `CODER_PROMPT` files-to-touch block | **P2** | One line: "If your edit changes a signature/contract, update its call sites too." Improves multi-file PASS_TO_PASS survival. |
| **Pre-commit secret/`git add .` discipline** | Devin, Droid (2) | `SHARED_RULES` (already bans `git commit`) | **P2** | Low effort: already forbids commit; if commits ever enabled, add "never `git add .`, never force-push." Keep as a latent guardrail note only. |
| **Don't grep for the missing FAIL_TO_PASS test** | OC-specific, reinforced by Devin "don't chase" | `SHARED_RULES` (`self_collab.py`/`split_solve.py`) | **P2** | This lives in the YAML (`team.self.collab.yaml:27-29`) but is **absent** from the deterministic-workflow SHARED_RULES. Port it — it's a known budget sink on SWE-bench. |

## 4. Deliberately skip
- **Todo-list / living-checklist machinery** (Cursor, Devin, Droid, Manus, Factory) — OC's deterministic workflows already encode sequencing in Python; freeform todos add verbosity with no control gain.
- **Explicit plan/standard/edit *mode* flags** (Devin, Cline) — OC enforces phase separation structurally via the workflow engine and tool-set-per-role; a prompted mode flag is redundant.
- **"Don't expose tool names to the user" / plain-language narration** (Cursor, Same, Replit) — consumer-chat polish; OC is headless/inter-agent, irrelevant and would bloat structured outputs.
- **Persistent memory store / "context will be deleted, save memories"** (Windsurf) — conflicts with OC's deterministic structured-handoff model; out of scope.
- **Follow-up-action suggestions, audience-calibrated simple language, ack-then-work first turn** (v0, Replit, Manus) — pure verbosity bloat for an eval harness.
- **XML-tag I/O framing, "malformed XML = failure"** (Cline, Devin) — product scaffolding; OC uses JSON-schema structured outputs already.
- **First-instruction-authoritative / untrusted-data classification** (Grok, Dia) — consumer-safety / prompt-injection framing; marginal for fixed SWE-bench inputs, defer.
- **Deploy/config-read ordering, version pinning, flat-folder bias, Replit sandbox conventions** — vendor-environment-specific, no OC analog.

## 5. Absorption plan (smallest-surface → highest-leverage)
1. **Tool-description edits (P0, ~30 min, zero behavior risk).** In `adapters/tools/fs.py`, `bash.py`, `run_tests.py`: add read-before-edit gate to `FileWriteTool`, when-to-use routing one-liners, and the non-interactive/absolute-path bash discipline. These reach the model via function-calling schemas (no prompt-injection needed), so they apply to *every* workflow and team config at once.
2. **Extend SHARED_RULES (P1) in both `workflows/self_collab.py:29-35` and `split_solve.py:40-46`** — single source edited in two siblings: add (a) verify-imports / never-assume-library, (b) ≤8-line report cap + no preamble, (c) the "don't grep for the missing FAIL_TO_PASS test" rule already proven in the YAML. Keep both copies identical.
3. **Tri-state verdict + honest-blocked (P1) in `VERDICT_SCHEMA`** (`self_collab.py:66-76`, `split_solve.py:68-78`): add `"BLOCKED"` enum; update `_run_phase`/`_run_subtask` to treat BLOCKED distinctly from FAIL (don't burn the remaining rounds re-coding an env problem). Update tester prompts to emit it.
4. **Port the rich apply_patch fallback detail (P1)** from `configs/team.self.collab.yaml:99-104` into `CODER_PROMPT` in both workflows, so the deterministic path matches the YAML team's edit-recovery quality.
5. **Add a unit test** in `opencollab/tests/test_split_solve_workflow.py` (already present, untracked) asserting BLOCKED verdict short-circuits the round loop and SHARED_RULES contains the new clauses; run `cd opencollab && .venv/bin/python -m pytest -q` (baseline 490 pass) and `.venv/bin/ruff check opencollab/`.
6. **Re-run the SWE-bench A/B** (split-solve workflow mode vs the 61.7% oc-team+kimi baseline) on the same instance set, using the validated A/B driver; gate the changes on resolved-rate non-regression and token/turn delta from the new verbosity cap.

Relevant files: `opencollab/opencollab/adapters/tools/fs.py`, `bash.py`, `run_tests.py`, and `apply_patch.py`; `workflows/self_collab.py` and `split_solve.py`; `configs/team.self.collab.yaml`; `opencollab/opencollab/bootstrap/team_config.py` (`DEFAULT_LEAD_PROMPT:44`, `DEFAULT_ROLE_PROMPT:83`); and `opencollab/opencollab/adapters/safety.py`.
