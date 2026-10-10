---
name: arcbench-runtime-signals
description: Emit ARC Bench runtime progress, requirement phase state, and frontend refresh signals through runner-events.jsonl. Use when an agent working inside an ARC Bench workspace needs to report run start/completion/failure/pause/resume, mark requirement design/implementation/test phase progress, or tell the frontend to refresh logs, submission status, commit history, preview, or traceability views.
---

# ARC Bench Runtime Signals

Use this skill when the task needs visible ARC Bench progress or frontend refresh behavior. Prefer the bundled script over hand-writing JSONL so event shape and path resolution stay consistent.

## Quick Start

Use the script path relative to this skill directory. Run from the target project workspace and keep `--project-dir .`, or prefix `scripts/arc_signal.py` with the relative path from the workspace to this skill directory.

```bash
python scripts/arc_signal.py run-started --message "Compilation started" --project-dir .
python scripts/arc_signal.py design-done --node-id REQ-1 --project-dir .
python scripts/arc_signal.py refresh --reason interfaces_updated --traceability-selected --traceability-all --project-dir .
```

The script writes to `.arc/runner-events.jsonl` relative to `--project-dir` unless `ARCBENCH_RUNNER_EVENTS_PATH` or `--events-path` overrides it.

## Action Groups

- `run-started`, `run-completed`, `run-failed`, `run-paused`, `run-resumed`: report lifecycle state.
- `design-started`, `design-done`, `design-failed`: update a requirement node design state.
- `implement-started`, `implement-done`, `implement-failed`: update a requirement node implementation state.
- `test-passed`, `test-failed`: update a requirement node test result.
- `traceability-changed`: request traceability refresh for selected and all views.
- `commit-history-changed`: request commit history refresh; add `--preview` if preview status should refresh.
- `refresh`: emit custom refresh flags with `--submission`, `--logs`, `--commit-history`, `--traceability-selected`, `--traceability-all`, `--preview`.

## Rules

- Always provide `--node-id` for requirement phase/test actions.
- Use short stable `--reason` values for frontend refresh signals, such as `interfaces_updated`, `tests_updated`, `git_commit`, or `preview_changed`.
- Do not write `runner-events.jsonl` by hand unless the runtime package is unavailable and the script cannot run.

## Reference

Read `references/actions.md` when you need the full action and flag list.
