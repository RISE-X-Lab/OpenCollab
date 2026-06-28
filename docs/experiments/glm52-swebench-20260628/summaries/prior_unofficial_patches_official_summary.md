# Prior Unofficial Patches Official Evaluation Summary

Run directory: `eval_work/prior_unofficial_patches_official_eval_20260627`

Input files:
`eval_work/prior_unofficial_patches_official_eval_20260627/input/dataset_prior_unofficial17.json`
`eval_work/prior_unofficial_patches_official_eval_20260627/input/predictions_prior_unofficial17.jsonl`

Official run id: `prior_unofficial_patches_official_20260627`

Total reports: 17
Resolved: 6
Unresolved: 11
Patch apply failures: 0

| source | total | resolved | unresolved |
|---|---:|---:|---:|
| empty_patch_rerun_nonempty15 | 15 | 5 | 10 |
| loopcheck_glm2 | 2 | 1 | 1 |

| instance | source | applied | resolved | F2P fail | P2P fail | first failures |
|---|---|---:|---:|---:|---:|---|
| astropy__astropy-7746 | empty_patch_rerun_nonempty15 | True | False | 1 | 0 | astropy/wcs/tests/test_wcs.py::test_zero_size_input |
| django__django-10924 | empty_patch_rerun_nonempty15 | True | True | 0 | 0 |  |
| django__django-11019 | empty_patch_rerun_nonempty15 | True | False | 16 | 0 | test_combine_media (forms_tests.tests.test_media.FormsMediaTestCase); test_construction (forms_tests.tests.test_media.FormsMediaTestCase) |
| django__django-11133 | empty_patch_rerun_nonempty15 | True | True | 0 | 0 |  |
| django__django-12113 | empty_patch_rerun_nonempty15 | True | True | 0 | 0 |  |
| django__django-13028 | empty_patch_rerun_nonempty15 | True | False | 0 | 5 | test_ticket_22429 (queries.tests.Ticket22429Tests); test_exclude_reverse_fk_field_ref (queries.tests.ExcludeTests) |
| django__django-13158 | empty_patch_rerun_nonempty15 | True | False | 1 | 0 | test_union_none (queries.test_qs_combinators.QuerySetSetOperationTests) |
| django__django-13590 | empty_patch_rerun_nonempty15 | True | False | 0 | 5 | test_complex_expressions_do_not_introduce_sql_injection_via_untrusted_string_inclusion (expressions.tests.IterableLookupInnerExpressionsTests); test_expressions_in_lookups_join_choice (expressions.tests.IterableLookupInnerExpressionsTests) |
| django__django-13964 | empty_patch_rerun_nonempty15 | True | False | 1 | 0 | test_save_fk_after_parent_with_non_numeric_pk_set_on_child (many_to_one.tests.ManyToOneTests) |
| django__django-14017 | empty_patch_rerun_nonempty15 | True | False | 2 | 0 | test_boolean_expression_combined (expressions.tests.BasicExpressionsTests); test_boolean_expression_combined_with_empty_Q (expressions.tests.BasicExpressionsTests) |
| django__django-14667 | empty_patch_rerun_nonempty15 | True | False | 1 | 1 | test_defer_only_chaining (defer.tests.DeferTests); test_defer_only_clear (defer.tests.DeferTests) |
| django__django-14997 | empty_patch_rerun_nonempty15 | True | False | 3 | 0 | test_rename_table_references_without_alias (backends.test_ddl_references.ExpressionsTests); test_alter_field_with_func_index (migrations.test_operations.OperationTests) |
| django__django-15213 | empty_patch_rerun_nonempty15 | True | False | 3 | 0 | test_aggregate_over_full_expression_annotation (annotations.tests.NonAggregateAnnotationTestCase); test_full_expression_annotation (annotations.tests.NonAggregateAnnotationTestCase) |
| django__django-15498 | empty_patch_rerun_nonempty15 | True | True | 0 | 0 |  |
| django__django-15814 | empty_patch_rerun_nonempty15 | True | True | 0 | 0 |  |
| psf__requests-2148 | loopcheck_glm2 | True | False | 1 | 0 | test_requests.py::RequestsTestCase::test_iter_content_handles_socket_error |
| sympy__sympy-11400 | loopcheck_glm2 | True | True | 0 | 0 |  |

Report root:
`eval_work/prior_unofficial_patches_official_eval_20260627/official_eval/logs/run_evaluation/prior_unofficial_patches_official_20260627`
