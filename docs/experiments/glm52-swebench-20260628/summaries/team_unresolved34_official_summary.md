# SWE-bench Official Evaluation Summary

Evaluation directory: `eval_work/team_mode_unresolved34_glm52_20260627/official_eval_parallel2b`

Total reports: 34
Resolved: 15
Unresolved: 19
Patch apply failures: 0

| instance | applied | resolved | F2P fail | P2P fail | first failures |
|---|---:|---:|---:|---:|---|
| astropy__astropy-14182 | True | True | 0 | 0 |  |
| django__django-11019 | True | False | 13 | 0 | test_combine_media (forms_tests.tests.test_media.FormsMediaTestCase); test_construction (forms_tests.tests.test_media.FormsMediaTestCase) |
| django__django-11283 | True | False | 1 | 0 | test_migrate_with_existing_target_permission (auth_tests.test_migrations.ProxyModelWithSameAppLabelTests) |
| django__django-11564 | True | False | 2 | 0 | test_add_script_name_prefix (settings_tests.tests.MediaURLStaticURLPrefixTest); test_not_prefixed (settings_tests.tests.MediaURLStaticURLPrefixTest) |
| django__django-11630 | True | False | 2 | 0 | test_collision_across_apps_database_routers_installed (check_framework.test_model_checks.DuplicateDBTableTests); test_collision_in_same_app_database_routers_installed (check_framework.test_model_checks.DuplicateDBTableTests) |
| django__django-11742 | True | False | 2 | 0 | test_choices_in_max_length (invalid_models_tests.test_ordinary_fields.CharFieldTests); test_choices_named_group (invalid_models_tests.test_ordinary_fields.CharFieldTests) |
| django__django-11848 | True | False | 2 | 0 | test_parsing_rfc850 (utils_tests.test_http.HttpDateProcessingTests); test_parsing_year_less_than_70 (utils_tests.test_http.HttpDateProcessingTests) |
| django__django-11905 | True | False | 2 | 0 | test_isnull_non_boolean_value (lookup.tests.LookupTests); test_iterator (lookup.tests.LookupTests) |
| django__django-12470 | True | False | 1 | 0 | test_inherited_ordering_pk_desc (model_inheritance.tests.ModelInheritanceTests) |
| django__django-12856 | True | True | 0 | 0 |  |
| django__django-13158 | True | True | 0 | 0 |  |
| django__django-13265 | True | True | 0 | 0 |  |
| django__django-13401 | True | True | 0 | 0 |  |
| django__django-13448 | True | True | 0 | 0 |  |
| django__django-13590 | True | False | 0 | 5 | test_complex_expressions_do_not_introduce_sql_injection_via_untrusted_string_inclusion (expressions.tests.IterableLookupInnerExpressionsTests); test_expressions_in_lookups_join_choice (expressions.tests.IterableLookupInnerExpressionsTests) |
| django__django-13660 | True | False | 0 | 2 | test_command_option_globals (shell.tests.ShellCommandTestCase); test_stdin_read_globals (shell.tests.ShellCommandTestCase) |
| django__django-13710 | True | True | 0 | 0 |  |
| django__django-13757 | True | True | 0 | 0 |  |
| django__django-13768 | True | False | 1 | 0 | test_send_robust_fail (dispatch.tests.DispatcherTests) |
| django__django-13925 | True | True | 0 | 0 |  |
| django__django-13964 | True | True | 0 | 0 |  |
| django__django-14017 | True | False | 1 | 0 | test_boolean_expression_combined_with_empty_Q (expressions.tests.BasicExpressionsTests) |
| django__django-14155 | True | False | 3 | 0 | test_repr (urlpatterns_reverse.tests.ResolverMatchTests); test_repr_functools_partial (urlpatterns_reverse.tests.ResolverMatchTests) |
| django__django-14667 | True | True | 0 | 0 |  |
| django__django-14730 | True | False | 1 | 0 | test_many_to_many_with_useless_related_name (invalid_models_tests.test_relative_fields.RelativeFieldTests) |
| django__django-14997 | True | True | 0 | 0 |  |
| django__django-15061 | True | True | 0 | 0 |  |
| django__django-15202 | True | False | 2 | 0 | test_urlfield_clean_invalid (forms_tests.field_tests.test_urlfield.URLFieldTest); test_urlfield_clean_not_required (forms_tests.field_tests.test_urlfield.URLFieldTest) |
| django__django-15213 | True | False | 1 | 0 | test_full_expression_annotation_with_aggregation (annotations.tests.NonAggregateAnnotationTestCase) |
| django__django-15252 | True | False | 2 | 0 | test_migrate_test_setting_false_ensure_schema (backends.base.test_creation.TestDbCreationTests); The django_migrations table is not created if there are no migrations |
| django__django-15320 | True | True | 0 | 0 |  |
| django__django-15388 | True | True | 0 | 0 |  |
| django__django-15695 | True | False | 1 | 0 | test_rename_index_unnamed_index (migrations.test_operations.OperationTests) |
| django__django-15738 | True | False | 1 | 0 | #23938 - Changing a ManyToManyField into a concrete field |
