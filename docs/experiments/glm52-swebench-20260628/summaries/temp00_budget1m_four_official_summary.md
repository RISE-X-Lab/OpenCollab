# SWE-bench Official Evaluation Summary

Evaluation directory: `eval_work/team_temp00_budget1m_four_20260628/official_eval_budget1m_four_side/official_eval`

Total reports: 4
Resolved: 1
Unresolved: 3
Patch apply failures: 0

| instance | applied | resolved | F2P fail | P2P fail | first failures |
|---|---:|---:|---:|---:|---|
| django__django-11019 | True | False | 14 | 0 | test_combine_media (forms_tests.tests.test_media.FormsMediaTestCase); test_construction (forms_tests.tests.test_media.FormsMediaTestCase) |
| django__django-14017 | True | True | 0 | 0 |  |
| django__django-15213 | True | False | 1 | 0 | test_full_expression_annotation_with_aggregation (annotations.tests.NonAggregateAnnotationTestCase) |
| django__django-15252 | True | False | 2 | 0 | test_migrate_test_setting_false_ensure_schema (backends.base.test_creation.TestDbCreationTests); The django_migrations table is not created if there are no migrations |
