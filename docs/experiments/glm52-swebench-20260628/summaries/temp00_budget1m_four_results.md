# Temperature 0.0, 1M Budget Four-Case Rerun

Run directory: `eval_work/team_temp00_budget1m_four_20260628`

Valid runner: `run_team_temp00_budget1m_four.py`

Invalid direct-workflow artifacts from the earlier attempt are `predictions_budget1m_four.jsonl` and `metrics_budget1m_four.jsonl`; they should not be used for official evaluation.

The valid Team rerun used `OPENCOLLAB_TEMPERATURE=0.0`, `OPENCOLLAB_THINKING=false`, and `OPENCOLLAB_BUDGET=1000000`. All four generated non-empty patches. Official SWE-bench evaluation resolved 1 of 4, with no patch apply failures.

| instance | tokens | cost all input | cost all output | patch bytes | wall seconds | official resolved | F2P fail | P2P fail |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `django__django-11019` | 706,356 | $0.989 | $3.108 | 5,475 | 573 | no | 14 | 0 |
| `django__django-14017` | 441,387 | $0.618 | $1.942 | 1,224 | 496 | yes | 0 | 0 |
| `django__django-15213` | 1,014,086 | $1.420 | $4.462 | 4,630 | 802 | no | 1 | 0 |
| `django__django-15252` | 531,489 | $0.744 | $2.339 | 915 | 477 | no | 2 | 0 |

Total generation tokens: 2,693,318.

Cost range without input/output split: $3.771 if all tokens are billed as input, $11.851 if all tokens are billed as output. If all tokens were cached input, the lower bound would be $0.700, but this is only a theoretical cached-input floor.

Official evaluation summary:

- `django__django-14017` resolved.
- `django__django-11019` unresolved: 14 FAIL_TO_PASS failures, first failures include `test_combine_media` and `test_construction`.
- `django__django-15213` unresolved: 1 FAIL_TO_PASS failure, `test_full_expression_annotation_with_aggregation`.
- `django__django-15252` unresolved: 2 FAIL_TO_PASS failures, including `test_migrate_test_setting_false_ensure_schema`.

Loop and repetition observations:

No explicit loop-detector event was emitted in the event logs. `django__django-15213` did show a real repetition pattern in the tester phase: the same sentence beginning "I have a clear picture of the bug..." was emitted 5 times, accompanied by 4 repeated reads of `django/db/models/sql/where.py` around offset 116. The session then resumed productive work, so this was a short repeated-action episode rather than a terminal loop_blocked stop.

Useful artifact paths:

- Valid predictions: `predictions_team_temp00_budget1m_four.jsonl`
- Generation metrics: `metrics_team_temp00_budget1m_four.jsonl`
- Generation summary: `team_generation_summary_budget1m.tsv`
- Official eval input: `official_eval_budget1m_four_side/input/`
- Official eval reports: `official_eval_budget1m_four_side/official_eval/`
- Official summary Markdown: `official_eval_budget1m_four_side/summary_official_eval_budget1m_four.md`
- Official summary JSON: `official_eval_budget1m_four_side/summary_official_eval_budget1m_four.json`
