# SWE-bench Official Evaluation Summary

Evaluation directory: `eval_work/team_temp00_unresolved22_glm52_20260627/official_eval_21_side/official_eval`

Total reports: 21
Resolved: 5
Unresolved: 16
Patch apply failures: 0

| instance | applied | resolved | F2P fail | P2P fail | first failures |
|---|---:|---:|---:|---:|---|
| astropy__astropy-14365 | True | False | 1 | 0 | astropy/io/ascii/tests/test_qdp.py::test_roundtrip[True] |
| astropy__astropy-7746 | True | False | 1 | 0 | astropy/wcs/tests/test_wcs.py::test_zero_size_input |
| django__django-11019 | True | False | 16 | 4 | test_combine_media (forms_tests.tests.test_media.FormsMediaTestCase); test_construction (forms_tests.tests.test_media.FormsMediaTestCase); test_media_dsl (forms_tests.tests.test_media.FormsMediaTestCase) |
| django__django-11283 | True | False | 1 | 0 | test_migrate_with_existing_target_permission (auth_tests.test_migrations.ProxyModelWithSameAppLabelTests) |
| django__django-11564 | True | False | 2 | 0 | test_add_script_name_prefix (settings_tests.tests.MediaURLStaticURLPrefixTest); test_not_prefixed (settings_tests.tests.MediaURLStaticURLPrefixTest) |
| django__django-11630 | True | False | 2 | 0 | test_collision_across_apps_database_routers_installed (check_framework.test_model_checks.DuplicateDBTableTests); test_collision_in_same_app_database_routers_installed (check_framework.test_model_checks.DuplicateDBTableTests) |
| django__django-11742 | True | False | 2 | 0 | test_choices_in_max_length (invalid_models_tests.test_ordinary_fields.CharFieldTests); test_choices_named_group (invalid_models_tests.test_ordinary_fields.CharFieldTests) |
| django__django-11848 | True | True | 0 | 0 |  |
| django__django-11905 | True | False | 2 | 0 | test_isnull_non_boolean_value (lookup.tests.LookupTests); test_iterator (lookup.tests.LookupTests) |
| django__django-12470 | True | False | 1 | 0 | test_inherited_ordering_pk_desc (model_inheritance.tests.ModelInheritanceTests) |
| django__django-13321 | True | False | 18 | 0 | test_decode_failure_logged_to_security (sessions_tests.tests.CookieSessionTests); test_decode_legacy (sessions_tests.tests.CookieSessionTests) |
| django__django-13590 | True | True | 0 | 0 |  |
| django__django-13660 | True | True | 0 | 0 |  |
| django__django-13768 | True | False | 1 | 0 | test_send_robust_fail (dispatch.tests.DispatcherTests) |
| django__django-14017 | True | True | 0 | 0 |  |
| django__django-14155 | True | False | 3 | 0 | test_repr (urlpatterns_reverse.tests.ResolverMatchTests); test_repr_functools_partial (urlpatterns_reverse.tests.ResolverMatchTests) |
| django__django-14730 | True | False | 1 | 0 | test_many_to_many_with_useless_related_name (invalid_models_tests.test_relative_fields.RelativeFieldTests) |
| django__django-15202 | True | False | 2 | 0 | test_urlfield_clean_invalid (forms_tests.field_tests.test_urlfield.URLFieldTest); test_urlfield_clean_not_required (forms_tests.field_tests.test_urlfield.URLFieldTest) |
| django__django-15213 | True | True | 0 | 0 |  |
| django__django-15252 | True | False | 2 | 0 | test_migrate_test_setting_false_ensure_schema (backends.base.test_creation.TestDbCreationTests); The django_migrations table is not created if there are no migrations |
| django__django-15695 | True | False | 1 | 0 | test_rename_index_unnamed_index (migrations.test_operations.OperationTests) |
