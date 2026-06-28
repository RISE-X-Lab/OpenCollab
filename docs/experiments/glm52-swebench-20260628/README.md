# GLM-5.2 SWE-bench Lite Experiment Archive

This directory is a compact, Git-trackable archive of the local OpenCollab + GLM-5.2 SWE-bench Lite experiments run in June 2026.

It intentionally keeps only the artifacts needed to understand and re-check the experiment outcomes:

- `report/` contains the final LaTeX report and compiled PDF.
- `summaries/` contains official evaluation summaries, empty-patch research notes, temperature-study summaries, token/cost notes, and loop/repetition analyses.
- `predictions/` contains the prediction JSONL files used for the official SWE-bench evaluations.

The raw working directory, `eval_work/`, is not tracked here. It contains session transcripts, event logs, Docker snapshots, command logs, LaTeX build files, and render-check images. Those files are useful for local forensic work, but they are too noisy for the repository history.

## Main Results

The final report in `report/final_report.pdf` is the canonical human-readable summary. Its headline numbers are:

| Experiment slice | Result |
|---|---:|
| Single-agent first 100, after empty-patch recovery | 56 / 100 resolved |
| Team mode over the first-100 unresolved set | 78 / 100 resolved overall |
| Team temperature=0.0 over 22 unresolved cases | 5 / 22 resolved |
| Team temperature=0.0, 1M-token rerun over 4 budget-limited cases | 1 / 4 resolved |

Patch application was not the dominant failure mode in the later experiments. Most remaining failures were semantic: missed edge cases, incomplete paired edits, or incorrect implementations near the right location.

## Reproducibility Notes

The JSONL files under `predictions/` are the compact inputs needed to re-run official SWE-bench evaluation for the corresponding slices, assuming the matching SWE-bench Lite dataset rows are available.

The summaries under `summaries/` preserve the official pass/fail tables and selected analysis. The raw logs remain local under `eval_work/` and should be treated as experiment artifacts rather than source files.

## Artifact Map

| File | Meaning |
|---|---|
| `report/final_report.pdf` | Final compiled experiment report. |
| `report/final_report.tex` | LaTeX source for the final report. |
| `summaries/first100_official_summary.json` | Machine-readable summary of the first 100 single-agent run. |
| `summaries/first100_error_deep_dive.md` | Local deep dive into first-100 failure causes. |
| `summaries/first100_empty_patch_research.md` | Empty-patch mechanism analysis. |
| `summaries/team_unresolved10_official_summary.*` | Official result for the first 10 Team-mode samples. |
| `summaries/team_unresolved34_official_summary.*` | Official result for the remaining 34 Team-mode samples. |
| `summaries/temp02_unresolved4_official_summary.*` | Official result for temperature=0.2 samples. |
| `summaries/temp00_prev21_official_summary.*` | Official result for the first 21 temperature=0.0 samples. |
| `summaries/temp00_django15738_retry_official_summary.*` | Official result for the final retried temperature=0.0 sample. |
| `summaries/temp00_budget1m_four_results.md` | Token/cost/process summary for the 1M-budget four-case rerun. |
| `summaries/temp00_budget1m_four_official_summary.*` | Official result for the 1M-budget four-case rerun. |
| `summaries/temp00_loop_repetition_analysis.md` | Repetition and budget-exhaustion analysis for temperature=0.0. |
| `predictions/*.jsonl` | Prediction files corresponding to the official summaries. |
