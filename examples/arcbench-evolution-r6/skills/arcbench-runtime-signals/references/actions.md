# ARC Bench Runtime Signal Actions

## Paths

- `--project-dir`: generated project root. Defaults to `ARCBENCH_OUTPUT_DIR`, `ARCBENCH_PROJECT_DIR`, `ARCBENCH_TEMPLATE_DIR`, then current directory.
- `--events-path`: optional runner events path. Relative values resolve under project dir.
- `--traceability-dir`: optional traceability directory. Needed only when phase state should also update `.arc/traceability/node_states.json`.

## Lifecycle Actions

- `run-started`
- `run-completed`
- `run-failed`
- `run-paused`
- `run-resumed`

Flags: `--message`.

## Requirement State Actions

- `design-started`: writes state `DESIGNING`.
- `design-done`: writes state `DESIGNED`.
- `design-failed`: writes state `FAILED`.
- `implement-started`: writes state `IMPLEMENTING`.
- `implement-done`: writes state `IMPLEMENTED`.
- `implement-failed`: writes state `FAILED`.
- `test-passed`: writes state `PASSED`.
- `test-failed`: writes state `FAILED`.

Flags: `--node-id`, optional `--message`.

## Refresh Actions

- `traceability-changed --reason <reason>`
- `commit-history-changed --reason <reason> [--preview]`
- `refresh --reason <reason> [--submission] [--logs] [--commit-history] [--traceability-selected] [--traceability-all] [--preview]`

Use refresh signals after writing assets outside the SDK so the frontend fetches fresh data.
