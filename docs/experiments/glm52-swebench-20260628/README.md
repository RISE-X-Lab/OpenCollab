# GLM-5.2 SWE-bench Lite Experiment Archive

A compact, Git-trackable archive of the OpenCollab + GLM-5.2 SWE-bench Lite
experiments run in June 2026. It keeps only what is needed to read the outcome
and re-check it against the official SWE-bench harness:

- `report/final_report.pdf` — the canonical human-readable report, written in
  Chinese (LaTeX source in `report/final_report.tex`).
- `predictions/*.jsonl` — the prediction files fed to the official SWE-bench
  evaluation, one per experiment slice.

Raw working data (session transcripts, event logs, Docker snapshots, LaTeX
build files) and the detailed per-slice analysis notes are intentionally not
tracked here; earlier revisions of this directory remain in Git history if you
need them.

## Main results

From `report/final_report.pdf`:

| Experiment slice | Result |
|---|---:|
| Single-agent, first 100, after empty-patch recovery | 56 / 100 resolved |
| Team mode over the first-100 unresolved set | 78 / 100 resolved overall |
| Team, temperature=0.0, over 22 unresolved cases | 5 / 22 resolved |
| Team, temperature=0.0, 1M-token rerun over 4 budget-limited cases | 1 / 4 resolved |

Most remaining failures were semantic (missed edge cases, incomplete paired
edits, or incorrect implementations near the right location) rather than patch
application errors.

## Predictions

| File | Slice |
|---|---|
| `first100_predictions.jsonl` | Single-agent first-100 run |
| `prior_unofficial17_predictions.jsonl` | 17 prior unofficial-patch cases |
| `team_unresolved10_predictions.jsonl` | First 10 Team-mode samples |
| `team_unresolved34_final_predictions.jsonl` | Remaining 34 Team-mode samples |
| `temp00_prev21_predictions.jsonl` | First 21 temperature=0.0 samples |
| `temp00_django15738_retry_predictions.jsonl` | Final retried temperature=0.0 sample |
| `temp02_unresolved4_predictions.jsonl` | Temperature=0.2 samples |
| `temp00_budget1m_four_predictions.jsonl` | 1M-token budget four-case rerun |
| `django14238_repaired_prediction.jsonl` | Repaired single case |

Each JSONL is a compact input for re-running the official SWE-bench Lite
evaluation on the matching dataset rows.
