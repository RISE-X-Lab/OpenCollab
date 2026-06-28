# GLM-5.2 first100 错误题深度归因（本地重建）

非空未通过 31 题，空 patch 19 题。

非空未通过类别：{'target_tests_still_fail': 20, 'target_failed_and_regressed': 6, 'regression_only': 3, 'partial_target_fix': 2}。

空 patch 类别：{'changed_but_final_diff_empty': 5, 'changed_then_stalled_or_reverted': 6, 'only_noop_or_failed_write': 8}。

## 31 个非空未通过

### 2. astropy__astropy-14182

类别：target_tests_still_fail；patch_chars=552；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：astropy/io/ascii/rst.py。gold 改动：astropy/io/ascii/rst.py。

问题：Please support header rows in RestructuredText output ### Description It would be great if the following would work: ```Python >>> from astropy.table import QTable >>> import astropy.units as u >>> import sys >>> tbl = QTable({'wave': [350,950]*u.nm, 'response...

残留目标测试：astropy/io/ascii/tests/test_rst.py::test_rst_with_header_rows。

### 3. astropy__astropy-14365

类别：target_failed_and_regressed；patch_chars=474；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=2。

模型改动：astropy/io/ascii/qdp.py。gold 改动：astropy/io/ascii/qdp.py。

问题：ascii.qdp Table format assumes QDP commands are upper case ### Description ascii.qdp assumes that commands in a QDP file are upper case, for example, for errors they must be "READ SERR 1 2" whereas QDP itself is not case sensitive and case use "read serr 1 2"....

残留目标测试：astropy/io/ascii/tests/test_qdp.py::test_roundtrip[True]。

回归测试样例：astropy/io/ascii/tests/test_qdp.py::test_get_tables_from_qdp_file; astropy/io/ascii/tests/test_qdp.py::test_roundtrip_example_comma。

### 16. django__django-11283

类别：target_tests_still_fail；patch_chars=1560；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：django/contrib/auth/migrations/0011_update_proxy_permissions.py。gold 改动：django/contrib/auth/migrations/0011_update_proxy_permissions.py。

问题：Migration auth.0011_update_proxy_permissions fails for models recreated as a proxy. Description (last modified by Mariusz Felisiak) I am trying to update my project to Django 2.2. When I launch python manage.py migrate, I get this error message when migration ...

残留目标测试：test_migrate_with_existing_target_permission (auth_tests.test_migrations.ProxyModelWithSameAppLabelTests)。

### 18. django__django-11564

类别：target_tests_still_fail；patch_chars=872；FAIL_TO_PASS 成功/失败=0/2；PASS_TO_PASS 失败=0。

模型改动：django/core/checks/urls.py。gold 改动：django/conf/__init__.py。

问题：Add support for SCRIPT_NAME in STATIC_URL and MEDIA_URL Description (last modified by Rostyslav Bryzgunov) By default, {% static '...' %} tag just appends STATIC_URL in the path. When running on sub-path, using SCRIPT_NAME WSGI param, it results in incorrect s...

残留目标测试：test_add_script_name_prefix (settings_tests.tests.MediaURLStaticURLPrefixTest); test_not_prefixed (settings_tests.tests.MediaURLStaticURLPrefixTest)。

### 21. django__django-11630

类别：target_tests_still_fail；patch_chars=1782；FAIL_TO_PASS 成功/失败=0/2；PASS_TO_PASS 失败=0。

模型改动：django/core/checks/model_checks.py。gold 改动：django/core/checks/model_checks.py。

问题：Django throws error when different apps with different models have the same name table name. Description Error message: table_name: (models.E028) db_table 'table_name' is used by multiple models: base.ModelName, app2.ModelName. We have a Base app that points t...

残留目标测试：test_collision_across_apps_database_routers_installed (check_framework.test_model_checks.DuplicateDBTableTests); test_collision_in_same_app_database_routers_installed (check_framework.test_model_checks.DuplicateDBTableTests)。

### 22. django__django-11742

类别：target_failed_and_regressed；patch_chars=1505；FAIL_TO_PASS 成功/失败=1/1；PASS_TO_PASS 失败=3。

模型改动：django/db/models/fields/__init__.py。gold 改动：django/db/models/fields/__init__.py。

问题：Add check to ensure max_length fits longest choice. Description There is currently no check to ensure that Field.max_length is large enough to fit the longest value in Field.choices. This would be very helpful as often this mistake is not noticed until an atte...

残留目标测试：test_choices_in_max_length (invalid_models_tests.test_ordinary_fields.CharFieldTests)。

回归测试样例：test_choices_containing_non_pairs (invalid_models_tests.test_ordinary_fields.CharFieldTests); test_choices_named_group_non_pairs (invalid_models_tests.test_ordinary_fields.CharFieldTests); test_non_iterable_choices (invalid_models_tests.test_ordinary_fields.CharFieldTests)。

### 25. django__django-11848

类别：target_tests_still_fail；patch_chars=735；FAIL_TO_PASS 成功/失败=0/2；PASS_TO_PASS 失败=0。

模型改动：django/utils/http.py。gold 改动：django/utils/http.py。

问题：django.utils.http.parse_http_date two digit year check is incorrect Description (last modified by Ad Timmering) RFC 850 does not mention this, but in RFC 7231 (and there's something similar in RFC 2822), there's the following quote: Recipients of a timestamp v...

残留目标测试：test_parsing_rfc850 (utils_tests.test_http.HttpDateProcessingTests); test_parsing_year_less_than_70 (utils_tests.test_http.HttpDateProcessingTests)。

### 26. django__django-11905

类别：target_tests_still_fail；patch_chars=608；FAIL_TO_PASS 成功/失败=0/2；PASS_TO_PASS 失败=0。

模型改动：django/db/models/lookups.py。gold 改动：django/db/models/lookups.py。

问题：Prevent using __isnull lookup with non-boolean value. Description (last modified by Mariusz Felisiak) __isnull should not allow for non-boolean values. Using truthy/falsey doesn't promote INNER JOIN to an OUTER JOIN but works fine for a simple queries. Using n...

残留目标测试：test_isnull_non_boolean_value (lookup.tests.LookupTests); test_iterator (lookup.tests.LookupTests)。

### 27. django__django-11910

类别：regression_only；patch_chars=810；FAIL_TO_PASS 成功/失败=1/0；PASS_TO_PASS 失败=1。

模型改动：django/db/migrations/autodetector.py。gold 改动：django/db/migrations/autodetector.py。

问题：ForeignKey's to_field parameter gets the old field's name when renaming a PrimaryKey. Description Having these two models class ModelA(models.Model): field_wrong = models.CharField('field1', max_length=50, primary_key=True) # I'm a Primary key. class ModelB(mo...

回归测试样例：test_rename_field_foreign_key_to_field (migrations.test_autodetector.AutodetectorTests)。

### 37. django__django-12470

类别：target_tests_still_fail；patch_chars=760；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：django/db/models/sql/compiler.py。gold 改动：django/db/models/sql/compiler.py。

问题：Inherited model doesn't correctly order by "-pk" when specified on Parent.Meta.ordering Description Given the following model definition: from django.db import models class Parent(models.Model): class Meta: ordering = ["-pk"] class Child(Parent): pass Querying...

残留目标测试：test_inherited_ordering_pk_desc (model_inheritance.tests.ModelInheritanceTests)。

### 42. django__django-12747

类别：target_tests_still_fail；patch_chars=971；FAIL_TO_PASS 成功/失败=0/3；PASS_TO_PASS 失败=0。

模型改动：django/db/models/deletion.py。gold 改动：django/db/models/deletion.py。

问题：QuerySet.Delete - inconsistent result when zero objects deleted Description The result format of the QuerySet.Delete method is a tuple: (X, Y) X - is the total amount of deleted objects (including foreign key deleted objects) Y - is a dictionary specifying cou...

残留目标测试：test_fast_delete_empty_no_update_can_self_select (delete.tests.FastDeleteTests); test_model_delete_returns_num_rows (delete.tests.DeletionTests); test_queryset_delete_returns_num_rows (delete.tests.DeletionTests)。

### 43. django__django-12856

类别：target_failed_and_regressed；patch_chars=685；FAIL_TO_PASS 成功/失败=0/3；PASS_TO_PASS 失败=5。

模型改动：django/db/models/base.py。gold 改动：django/db/models/base.py。

问题：Add check for fields of UniqueConstraints. Description (last modified by Marnanel Thurman) When a model gains a UniqueConstraint, makemigrations doesn't check that the fields named therein actually exist. This is in contrast to the older unique_together syntax...

残留目标测试：test_unique_constraint_pointing_to_m2m_field (invalid_models_tests.test_models.ConstraintsTests); test_unique_constraint_pointing_to_missing_field (invalid_models_tests.test_models.ConstraintsTests); test_unique_constraint_pointing_to_non_local_field (invalid_models_tests.test_models.ConstraintsTests)。

回归测试样例：test_deferrable_unique_constraint (invalid_models_tests.test_models.ConstraintsTests); test_deferrable_unique_constraint_required_db_features (invalid_models_tests.test_models.ConstraintsTests); test_unique_constraint_pointing_to_fk (invalid_models_tests.test_models.ConstraintsTests); test_unique_constraint_with_condition (invalid_models_tests.test_models.ConstraintsTests); test_unique_constraint_with_condition_required_db_features (invalid_models_tests.test_models.ConstraintsTests)。

### 52. django__django-13265

类别：partial_target_fix；patch_chars=2174；FAIL_TO_PASS 成功/失败=2/2；PASS_TO_PASS 失败=0。

模型改动：django/db/migrations/autodetector.py。gold 改动：django/db/migrations/autodetector.py。

问题：AlterOrderWithRespectTo() with ForeignKey crash when _order is included in Index(). Description class Meta: db_table = 'look_image' order_with_respect_to = 'look' indexes = [ models.Index(fields=['look', '_order']), models.Index(fields=['created_at']), models....

残留目标测试：test_set_alter_order_with_respect_to_index_constraint_foo_together (migrations.test_autodetector.AutodetectorTests); test_supports_functools_partial (migrations.test_autodetector.AutodetectorTests)。

### 54. django__django-13321

类别：partial_target_fix；patch_chars=843；FAIL_TO_PASS 成功/失败=337/18；PASS_TO_PASS 失败=0。

模型改动：django/contrib/sessions/backends/base.py。gold 改动：django/contrib/sessions/backends/base.py。

问题：Decoding an invalid session data crashes. Description (last modified by Matt Hegarty) Hi I recently upgraded my staging server to 3.1. I think that there was an old session which was still active. On browsing to any URL, I get the crash below. It looks similar...

残留目标测试：test_decode_failure_logged_to_security (sessions_tests.tests.CookieSessionTests); test_decode_legacy (sessions_tests.tests.CookieSessionTests); test_decode_failure_logged_to_security (sessions_tests.tests.CacheSessionTests); test_decode_legacy (sessions_tests.tests.CacheSessionTests); test_decode_failure_logged_to_security (sessions_tests.tests.FileSessionPathLibTests); test_decode_legacy (sessions_tests.tests.FileSessionPathLibTests); test_decode_failure_logged_to_security (sessions_tests.tests.FileSessionTests); test_decode_legacy (sessions_tests.tests.FileSessionTests); test_decode_failure_logged_to_security (sessions_tests.tests.DatabaseSessionTests); test_decode_legacy (sessions_tests.tests.DatabaseSessionTests)。

### 55. django__django-13401

类别：regression_only；patch_chars=1219；FAIL_TO_PASS 成功/失败=1/0；PASS_TO_PASS 失败=1。

模型改动：django/db/models/fields/__init__.py。gold 改动：django/db/models/fields/__init__.py。

问题：Abstract model field should not be equal across models Description Consider the following models: class A(models.Model): class Meta: abstract = True myfield = IntegerField() class B(A): pass class C(A): pass If I pull the fields of B and C into a shared set, o...

回归测试样例：Fields are ordered based on their creation.。

### 57. django__django-13448

类别：target_tests_still_fail；patch_chars=759；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：django/db/backends/base/creation.py。gold 改动：django/db/backends/base/creation.py。

问题：Test runner setup_databases crashes with "TEST": {"MIGRATE": False}. Description I'm trying to upgrade a project from Django 3.0 to Django 3.1 and wanted to try out the new "TEST": {"MIGRATE": False} database setting. Sadly I'm running into an issue immediatel...

残留目标测试：test_migrate_test_setting_false (backends.base.test_creation.TestDbCreationTests)。

### 61. django__django-13660

类别：regression_only；patch_chars=1199；FAIL_TO_PASS 成功/失败=2/0；PASS_TO_PASS 失败=2。

模型改动：django/core/management/commands/shell.py。gold 改动：django/core/management/commands/shell.py。

问题：shell command crashes when passing (with -c) the python code with functions. Description The examples below use Python 3.7 and Django 2.2.16, but I checked that the code is the same on master and works the same in Python 3.8. Here's how ​python -c works: $ pyt...

回归测试样例：test_command_option_globals (shell.tests.ShellCommandTestCase); test_stdin_read_globals (shell.tests.ShellCommandTestCase)。

### 62. django__django-13710

类别：target_failed_and_regressed；patch_chars=575；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=11。

模型改动：django/contrib/admin/options.py。gold 改动：django/contrib/admin/options.py。

问题：Use Admin Inline verbose_name as default for Inline verbose_name_plural Description Django allows specification of a verbose_name and a verbose_name_plural for Inline classes in admin views. However, verbose_name_plural for an Inline is not currently based on ...

残留目标测试：test_verbose_name_inline (admin_inlines.tests.TestVerboseNameInlineForms)。

回归测试样例：test_inline_add_fk_add_perm (admin_inlines.tests.TestInlinePermissions); test_inline_add_m2m_view_only_perm (admin_inlines.tests.TestInlinePermissions); test_inline_change_fk_add_change_perm (admin_inlines.tests.TestInlinePermissions); test_inline_change_fk_add_perm (admin_inlines.tests.TestInlinePermissions); test_inline_change_fk_all_perms (admin_inlines.tests.TestInlinePermissions); test_inline_change_fk_change_del_perm (admin_inlines.tests.TestInlinePermissions); test_inline_change_fk_change_perm (admin_inlines.tests.TestInlinePermissions); test_inline_change_m2m_change_perm (admin_inlines.tests.TestInlinePermissions); test_inline_change_m2m_view_only_perm (admin_inlines.tests.TestInlinePermissions); test_inlines_plural_heading_foreign_key (admin_inlines.tests.TestInline)。

### 63. django__django-13757

类别：target_tests_still_fail；patch_chars=773；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：django/db/models/fields/json.py。gold 改动：django/db/models/fields/json.py。

问题：Using __isnull=True on a KeyTransform should not match JSON null on SQLite and Oracle Description The KeyTransformIsNull lookup borrows the logic from HasKey for isnull=False, which is correct. If isnull=True, the query should only match objects that do not ha...

残留目标测试：test_isnull_key (model_fields.test_jsonfield.TestQuerying)。

### 64. django__django-13768

类别：target_tests_still_fail；patch_chars=995；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：django/dispatch/dispatcher.py。gold 改动：django/dispatch/dispatcher.py。

问题：Log exceptions handled in Signal.send_robust() Description As pointed out by ​Haki Benita on Twitter, by default Signal.send_robust() doesn't have any log messages for exceptions raised in receivers. Since Django logs exceptions in other similar situations, su...

残留目标测试：test_send_robust_fail (dispatch.tests.DispatcherTests)。

### 65. django__django-13925

类别：target_tests_still_fail；patch_chars=550；FAIL_TO_PASS 成功/失败=0/2；PASS_TO_PASS 失败=0。

模型改动：django/db/models/base.py。gold 改动：django/db/models/base.py。

问题：models.W042 is raised on inherited manually specified primary key. Description I have models which inherit from other models, and they should inherit the primary key. This works fine with Django 3.1. However, if I install Django 3.2 alpha, when I run make_migr...

残留目标测试：test_auto_created_inherited_pk (check_framework.test_model_checks.ModelDefaultAutoFieldTests); test_explicit_inherited_pk (check_framework.test_model_checks.ModelDefaultAutoFieldTests)。

### 70. django__django-14155

类别：target_tests_still_fail；patch_chars=690；FAIL_TO_PASS 成功/失败=0/3；PASS_TO_PASS 失败=0。

模型改动：django/urls/resolvers.py。gold 改动：django/urls/resolvers.py。

问题：ResolverMatch.__repr__() doesn't handle functools.partial() nicely. Description (last modified by Nick Pope) When a partial function is passed as the view, the __repr__ shows the func argument as functools.partial which isn't very helpful, especially as it doe...

残留目标测试：test_repr (urlpatterns_reverse.tests.ResolverMatchTests); test_repr_functools_partial (urlpatterns_reverse.tests.ResolverMatchTests); test_resolver_match_on_request (urlpatterns_reverse.tests.ResolverMatchTests)。

### 79. django__django-14730

类别：target_tests_still_fail；patch_chars=739；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：django/db/models/fields/related.py。gold 改动：django/db/models/fields/related.py。

问题：Prevent developers from defining a related_name on symmetrical ManyToManyFields Description In ManyToManyField, if the symmetrical argument is passed, or if it's a self-referential ManyToMany relationship, the related field on the target model is not created. ...

残留目标测试：test_many_to_many_with_useless_related_name (invalid_models_tests.test_relative_fields.RelativeFieldTests)。

### 85. django__django-14999

类别：target_failed_and_regressed；patch_chars=2677；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=113。

模型改动：django/db/migrations/operations/models.py。gold 改动：django/db/migrations/operations/models.py。

问题：RenameModel with db_table should be a noop. Description A RenameModel operation that already has db_table defined must be a noop. In Postgres, it drops and recreates foreign key constraints. In sqlite it recreates the table (as expected for a table renaming).

残留目标测试：test_rename_model_with_db_table_noop (migrations.test_operations.OperationTests)。

回归测试样例：test_references_model_mixin (migrations.test_operations.TestCreateModel); test_reference_field_by_through_fields (migrations.test_operations.FieldOperationTests); test_references_field_by_from_fields (migrations.test_operations.FieldOperationTests); test_references_field_by_name (migrations.test_operations.FieldOperationTests); test_references_field_by_remote_field_model (migrations.test_operations.FieldOperationTests); test_references_field_by_through (migrations.test_operations.FieldOperationTests); test_references_field_by_to_fields (migrations.test_operations.FieldOperationTests); test_references_model (migrations.test_operations.FieldOperationTests); Tests the AddField operation.; The CreateTable operation ignores swapped models.。

### 86. django__django-15061

类别：target_tests_still_fail；patch_chars=531；FAIL_TO_PASS 成功/失败=0/3；PASS_TO_PASS 失败=0。

模型改动：django/forms/widgets.py。gold 改动：django/forms/widgets.py。

问题：Remove "for = ..." from MultiWidget's <label>. Description The instance from Raw MultiWidget class generate id_for_label like f'{id_}0' It has not sense. For example ChoiceWidget has self.add_id_index and I can decide it myself, how I will see label_id - with ...

残留目标测试：test_form_as_table (forms_tests.field_tests.test_multivaluefield.MultiValueFieldTest); test_form_as_table_data (forms_tests.field_tests.test_multivaluefield.MultiValueFieldTest); test_form_as_table (forms_tests.field_tests.test_splitdatetimefield.SplitDateTimeFieldTest)。

### 87. django__django-15202

类别：target_tests_still_fail；patch_chars=1394；FAIL_TO_PASS 成功/失败=0/2；PASS_TO_PASS 失败=0。

模型改动：django/core/validators.py。gold 改动：django/core/validators.py。

问题：URLField throws ValueError instead of ValidationError on clean Description forms.URLField( ).clean('////]@N.AN') results in: ValueError: Invalid IPv6 URL Traceback (most recent call last): File "basic_fuzzer.py", line 22, in TestOneInput File "fuzzers.py", lin...

残留目标测试：test_urlfield_clean_invalid (forms_tests.field_tests.test_urlfield.URLFieldTest); test_urlfield_clean_not_required (forms_tests.field_tests.test_urlfield.URLFieldTest)。

### 89. django__django-15252

类别：target_tests_still_fail；patch_chars=806；FAIL_TO_PASS 成功/失败=0/2；PASS_TO_PASS 失败=0。

模型改动：django/db/migrations/recorder.py。gold 改动：django/db/migrations/executor.py。

问题：MigrationRecorder does not obey db_router allow_migrate rules Description Hi, We have a multi-db setup. We have one connection that is for the django project, and several connections that talk to other dbs for information (ie models with managed = False). Djan...

残留目标测试：test_migrate_test_setting_false_ensure_schema (backends.base.test_creation.TestDbCreationTests); The django_migrations table is not created if there are no migrations。

### 90. django__django-15320

类别：target_tests_still_fail；patch_chars=775；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：django/db/models/expressions.py。gold 改动：django/db/models/expressions.py。

问题：Subquery.as_sql() generates invalid SQL. Description (last modified by M1ha Shvn) Since ​this commit Subquery.as_sql(...) method returns incorrect SQL removing first and last symbols instead of absent breakets. Adding Subquery().query.subquery = True attribute...

残留目标测试：test_subquery_sql (expressions.tests.BasicExpressionsTests)。

### 92. django__django-15388

类别：target_tests_still_fail；patch_chars=370；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：django/template/autoreload.py。gold 改动：django/template/autoreload.py。

问题：Dev Server fails to restart after adding BASE_DIR to TEMPLATES[0]['DIRS'] in settings Description Repro steps: $ pip install -U django $ django-admin startproject <name> Open settings.py, copy the BASE_DIR variable from line 16 and paste it into the empty DIRS...

残留目标测试：test_non_template_changed_in_template_directory (template_tests.test_autoreloader.TemplateReloadTests)。

### 95. django__django-15695

类别：target_tests_still_fail；patch_chars=800；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=0。

模型改动：django/db/migrations/operations/models.py。gold 改动：django/db/migrations/operations/models.py。

问题：RenameIndex() crashes when unnamed index is moving backward and forward. Description RenameIndex() should restore the old auto-generated name when an unnamed index for unique_together is moving backward. Now re-applying RenameIndex() crashes. For example: test...

残留目标测试：test_rename_index_unnamed_index (migrations.test_operations.OperationTests)。

### 97. django__django-15781

类别：target_failed_and_regressed；patch_chars=637；FAIL_TO_PASS 成功/失败=0/1；PASS_TO_PASS 失败=43。

模型改动：django/core/management/base.py。gold 改动：django/core/management/base.py。

问题：Customizable management command formatters. Description With code like: class Command(BaseCommand): help = ''' Import a contract from tzkt. Example usage: ./manage.py tzkt_import 'Tezos Mainnet' KT1HTDtMBRCKoNHjfWEEvXneGQpCfPAt6BRe ''' Help output is: $ ./mana...

残留目标测试：BaseCommand.create_parser() passes kwargs to CommandParser.。

回归测试样例：test_get_random_secret_key (user_commands.tests.UtilsTests); test_is_ignored_path_false (user_commands.tests.UtilsTests); test_is_ignored_path_true (user_commands.tests.UtilsTests); test_no_existent_external_program (user_commands.tests.UtilsTests); test_normalize_path_patterns_truncates_wildcard_base (user_commands.tests.UtilsTests); By default, call_command should not trigger the check framework, unless; When passing the long option name to call_command, the available option; It should be possible to pass non-string arguments to call_command.; test_call_command_unrecognized_option (user_commands.tests.CommandTests); test_call_command_with_required_parameters_in_mixed_options (user_commands.tests.CommandTests)。

## 19 个空 patch

### 6. astropy__astropy-7746

类别：changed_but_final_diff_empty；session=2026-06-26T10-51-57；write_count=2；changed=2；noop=0；old_str_not_found=0；loop=False。

问题：Issue when passing empty lists/arrays to WCS transformations The following should not fail but instead should return empty lists/arrays: ``` In [1]: from astropy.wcs import WCS In [2]: wcs = WCS('2MASS_h.fits') In [3]: wcs.wcs_pix2world([], [], 0) ------------...

最后模型自述：The test doesn't exist yet (it's the one that must pass after the fix). Let me verify the behavior with a quick snippet using a WCS with naxis=2. | The empty list `[]` becomes a float array of shape `(0,)`, so `xy` after hstack has shape `(0, 2)` but with `xy.shape[0] == 0`. Wait, the error is still happening. Let me check — the issue is `np.asarray([])` produces shape `(0,)` and `x.reshape((x.size, 1))` ... | My edit didn't apply — the file still shows the old content. Let me re-apply the str_replace. | The traceback shows line 1225 still calls `func(xy, origin)` directly — my edit isn't taking. There may be a compiled `.pyc` or the file content differs. Let me check the actual current content. | The str_replace reported success but the file content didn't change — likely whitespace mismatch. Let me check for trailing whitespace and use a more targeted unique match.。

写入结果：Replaced in /testbed/astropy/wcs/wcs.py: 1181 chars → 1348 chars (content changed) | Replaced in /testbed/astropy/wcs/wcs.py: 853 chars → 1020 chars (content changed)。

### 8. django__django-10924

类别：changed_then_stalled_or_reverted；session=2026-06-26T10-58-37；write_count=4；changed=2；noop=2；old_str_not_found=0；loop=False。

问题：Allow FilePathField path to accept a callable. Description I have a special case where I want to create a model containing the path to some local files on the server/dev machine. Seeing as the place where these files are stored is different on different machin...

最后模型自述：Wait, that change removed the defaults. Let me reconsider — the formfield needs to pass the resolved path. Let me revert and do this properly. | Now let me make the form field resolve a callable path, and the model field's formfield pass the path through (the form field will resolve it). Let me update the forms FilePathField to call the path if callable. | Let me look at the test to understand exactly what's expected. | The test file doesn't exist yet — it'll be added by the grader. Let me check the test_promises test and the models to understand the expected behavior, then apply the fix. | Now let me apply the fix. The model field's `formfield` should resolve a callable path, and the form field should resolve a callable path in its `__init__`.。

写入结果：Replaced in /testbed/django/db/models/fields/__init__.py: 350 chars → 135 chars (content changed) | Replaced in /testbed/django/db/models/fields/__init__.py: 135 chars → 350 chars (content changed) | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in /testbed/django/forms/fields.py. | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in /testbed/django/db/models/fields/__init__.py.。

### 10. django__django-11019

类别：only_noop_or_failed_write；session=2026-06-26T11-03-12；write_count=2；changed=0；noop=2；old_str_not_found=0；loop=False。

问题：Merging 3 or more media objects can throw unnecessary MediaOrderConflictWarnings Description Consider the following form definition, where text-editor-extras.js depends on text-editor.js but all other JS files are independent: from django import forms class Co...

最后模型自述：The issue is in the `merge` method. When a path already exists in `combined_list` and `index > last_insert_index`, it warns. But the warning should only fire when there's an actual conflict. The problem: when merging multiple lists, the `last_insert_index` tra... | The key insight: the warning should only fire when there's a *real* conflict — i.e., when the duplicate path appears *before* `last_insert_index` in a way that contradicts list_2's order. The current logic warns whenever `index > last_insert_index`, but that's... | The fix: only warn when there's a genuine conflict. The current code warns whenever `index > last_insert_index`, but `last_insert_index` moves backward as we process list_2 in reverse, so a duplicate found later (with a smaller index) is fine. The real conflic... | Let me trace the actual issue carefully. The problem is the warning fires for `text-editor-extras.js` / `text-editor.js`. Let me reproduce first. | Now I understand. The `_js` property merges lists pairwise via `merge`. The issue is that `merge` is called on already-merged results, and the warning logic is too aggressive. The real fix: the warning should only fire when there's a genuine conflict between t...。

写入结果：Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in /testbed/django/forms/widgets.py. | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in /testbed/django/forms/widgets.py.。

### 14. django__django-11133

类别：changed_then_stalled_or_reverted；session=2026-06-26T11-09-17；write_count=5；changed=1；noop=4；old_str_not_found=0；loop=False。

问题：HttpResponse doesn't handle memoryview objects Description I am trying to write a BinaryField retrieved from the database into a HttpResponse. When the database is Sqlite this works correctly, but Postgresql returns the contents of the field as a memoryview ob...

最后模型自述：That created a duplicate. Let me view and fix properly. | Let me remove the duplicate and add memoryview handling. | I'll replace the duplicated block with a single correct version that handles memoryview. | I'll rewrite the whole duplicated region in one create-style replacement by reading the file and writing it back. Let me just use sed to remove the duplicate block and add memoryview handling. | Now the duplicate is removed. Let me add memoryview handling.。

写入结果：Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in /testbed/django/http/response.py. | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in /testbed/django/http/response.py. | Replaced in /testbed/django/http/response.py: 238 chars → 1005 chars (content changed) | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in /testbed/django/http/response.py. | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in /testbed/django/http/response.py.。

### 23. django__django-11797

类别：only_noop_or_failed_write；session=2026-06-26T11-42-41；write_count=3；changed=0；noop=0；old_str_not_found=0；loop=False。

问题：Filtering on query result overrides GROUP BY of internal query Description from django.contrib.auth import models a = models.User.objects.filter(email__isnull=True).values('email').annotate(m=Max('id')).values('m') print(a.query) # good # SELECT MAX("auth_user...

最后模型自述：This is the `get_aggregation` path. But the issue is about filtering on a query result (subquery used as RHS of a filter). Let me look at how the subquery is built when used as a lookup RHS. The issue shows `GROUP BY U0."id"` instead of `U0."email"`. Let me ch... | Let me use the test runner setup instead. | Let me check the test that must pass to understand the expected behavior. | The test doesn't exist yet — it'll be added during grading. Let me look at the test_sqlite settings and reproduce properly. | The test_sqlite doesn't define INSTALLED_APPS. Let me reproduce via the runtests infrastructure instead, but first let me just understand the bug from the code. The issue: when a sliced query `a[:1]` is used as RHS of a filter, the subquery's GROUP BY becomes ...。

写入结果：Error: No module named 'tests.test_settings' | Error: Model class django.contrib.contenttypes.models.ContentType doesn't declare an explicit app_label and isn't in an application in INSTALLED_APPS. | Error: Model class django.contrib.contenttypes.models.ContentType doesn't declare an explicit app_label and isn't in an application in INSTALLED_APPS.。

### 30. django__django-12113

类别：only_noop_or_failed_write；session=2026-06-26T11-53-14；write_count=3；changed=0；noop=0；old_str_not_found=0；loop=False。

问题：admin_views.test_multidb fails with persistent test SQLite database. Description (last modified by Mariusz Felisiak) I've tried using persistent SQLite databases for the tests (to make use of --keepdb), but at least some test fails with: sqlite3.OperationalErr...

最后模型自述：The issue is in `_get_test_db_name`. When `TEST['NAME']` is not set, it returns `:memory:` which then becomes a shared in-memory DB. But the real problem is the `database is locked` error with persistent SQLite files. Let me look at the base creation class to ... | The issue is the `database is locked` error. With persistent SQLite files, the connection isn't being closed before the file is accessed/copied, causing lock contention. Let me look at the `_create_test_db` flow more carefully — the key issue is that when `kee... | The `test_custom_test_name` test uses `BaseDatabaseCreation.test_db_signature()` and expects `signature[3]` to be the test name. Let me check the base `test_db_signature`: | Now I understand the test. The base `test_db_signature` returns a 4-tuple with `_get_test_db_name()` as element [3]. The SQLite override returns a different signature. The test `test_custom_test_name` expects `signature[3] == 'hodor'`. Now let me focus on the ... | The root cause: with persistent SQLite files, the `_create_test_db` returns early on `keepdb` but the connection that was opened (e.g. during `ensure_connection` or by the test runner's setup) still holds a lock on the file. The base `create_test_db` calls `se...。

写入结果：Error: database is locked | Error: database is locked | Error: database is locked。

### 39. django__django-12589

类别：only_noop_or_failed_write；session=2026-06-26T12-09-45；write_count=1；changed=0；noop=0；old_str_not_found=0；loop=False。

问题：Django 3.0: "GROUP BY" clauses error with tricky field annotation Description Let's pretend that we have next model structure with next model's relations: class A(models.Model): bs = models.ManyToManyField('B', related_name="a", through="AB") class B(models.Mo...

最后模型自述：I'll explore the source to find where GROUP BY clauses are generated for annotations. | Let me look at the GROUP BY generation logic in the compiler. | The key is in `get_group_by_cols`. The issue is that when an annotation is referenced in `.values()` and then used in GROUP BY, it should use the alias reference (Ref) instead of expanding the full subquery. Let me look at `get_group_by_cols` and how `resolve_... | The issue: when `.values("status")` is called, `query.group_by` contains the string `"status"`. In `get_group_by`, `resolve_ref("status")` returns the annotation (the Subquery) directly because `summarize=False`. Then in the select loop, the annotation's `get_... | Let me look at the `Ref` class and the `get_group_by_cols` for the base Expression.。

写入结果：Error: column reference "status" is ambiguous。

### 47. django__django-13028

类别：changed_then_stalled_or_reverted；session=2026-06-26T13-28-11；write_count=7；changed=3；noop=2；old_str_not_found=2；loop=False。

问题：Queryset raises NotSupportedError when RHS has filterable=False attribute. Description (last modified by Nicolas Baccelli) I'm migrating my app to django 3.0.7 and I hit a strange behavior using a model class with a field labeled filterable class ProductMetaDa...

最后模型自述：The `filterable` attribute is meant for expressions to opt out of filtering. A plain model instance with a field named `filterable` shouldn't trigger this. The fix: only check `filterable` on actual expressions (those with `as_sql`), not arbitrary values. | That edit was wrong — I accidentally duplicated a method signature. Let me fix it properly. | Good, the file is back to original (my edit was a no-op since the `_add_q` line already existed). Now the correct fix: only enforce the `filterable` opt-out on actual expressions (those with `as_sql`), not plain model instances used as RHS values. | The file is already back to original. Now apply the correct fix: only enforce the `filterable` opt-out on actual expressions (those with `as_sql`), not plain model instances used as RHS values. | That was wrong — I duplicated the method. Let me revert to a clean state and apply the correct minimal fix.。

写入结果：Error: old_str not found in /testbed/django/db/models/sql/query.py. Make sure the text matches exactly (including whitespace). If the edit keeps failing to match, use the apply_patch tool instead. | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in /testbed/django/db/models/sql/query.py. | Replaced in /testbed/django/db/models/sql/query.py: 496 chars → 578 chars (content changed) | Good, the file is back to original (my edit was a no-op since the `_add_q` line already existed). Now the correct fix: only enforce the `filterable` opt-out on actual expressions (those with `as_sql`), not plain model instances used as RHS values. | Error: old_str not found in /testbed/django/db/models/sql/query.py. Make sure the text matches exactly (including whitespace). If the edit keeps failing to match, use the apply_patch tool instead. | Replaced in /testbed/django/db/models/sql/query.py: 496 chars → 996 chars (content changed)。

### 49. django__django-13158

类别：changed_but_final_diff_empty；session=2026-06-26T12-33-53；write_count=1；changed=1；noop=0；old_str_not_found=0；loop=False。

问题：QuerySet.none() on combined queries returns all results. Description I came across this issue on Stack Overflow. I'm not 100% sure it's a bug, but it does seem strange. With this code (excuse the bizarre example filtering): class Publication(models.Model): pas...

最后模型自述：Now let me look at how `union`/combinators are built and how the SQL is generated, to find where `is_empty` should short-circuit. | I see the issue. When `none()` is called on a combined query, `set_empty()` adds a `NothingNode` to `self.where`, but for a combinator query the `where` isn't used — the combined subqueries are. So `is_empty()` returns False and the empty result isn't honored.... | The fix: `is_empty()` should return True when the query itself is a combinator that's been marked empty (via `none()`), since `set_empty()` adds a `NothingNode` to `self.where` but combinator queries don't use `where`. Let me update `is_empty` to also check th... | The issue: when `none()` is called on a combined query, `set_empty()` adds a `NothingNode` to `self.where`, but combinator queries don't use `where` — they use `combined_queries`. So `is_empty()` returns False and the empty result isn't honored. The fix is to ... | Now let me verify the fix works. The key scenario: `none()` on a union query should return no results.。

写入结果：Replaced in /testbed/django/db/models/sql/query.py: 99 chars → 207 chars (content changed)。

### 59. django__django-13590

类别：changed_but_final_diff_empty；session=2026-06-26T12-51-21；write_count=1；changed=1；noop=0；old_str_not_found=0；loop=False。

问题：Upgrading 2.2>3.0 causes named tuples used as arguments to __range to error. Description I noticed this while upgrading a project from 2.2 to 3.0. This project passes named 2-tuples as arguments to range queryset filters. This works fine on 2.2. On 3.0 it caus...

最后模型自述：Now let me verify the fix works: | The fix works. Let me run the actual test that must pass: | The test doesn't exist yet (it's the test that must pass after the fix). Let me check the existing test class to understand the pattern, then verify the fix works against the actual queryset behavior: | The fix is already applied. Let me verify it works against the actual queryset behavior with a named tuple range lookup:。

写入结果：Replaced in /testbed/django/db/models/sql/query.py: 330 chars → 375 chars (content changed)。

### 67. django__django-13964

类别：changed_then_stalled_or_reverted；session=2026-06-26T13-39-25；write_count=2；changed=2；noop=0；old_str_not_found=0；loop=False。

问题：Saving parent object after setting on child leads to data loss for parents with non-numeric primary key. Description (last modified by Charlie DeTar) Given a model with a foreign key relation to another model that has a non-auto CharField as its primary key: c...

最后模型自述：The default `pre_save` reads `getattr(model_instance, self.attname)` — i.e. the cached `product_id` value set at assignment time. So if we don't overwrite it with empty string when the related pk is unset, the save will read the stale empty value. The fix: in ... | Wait — my fix only handles `None`, but the issue is about an empty string (CharField pk defaults to `''`). Let me reconsider. The real problem: when the related pk is unset (empty string for CharField), we shouldn't store that stale value. But `pre_save` reads... | The test doesn't exist yet (it's the grader's). Let me reconsider my fix. The real issue: `pre_save` reads `product_id` from the instance dict (stale empty string). My fix sets it to None when pk is None — but for CharField pk, the unset value is `''` not None... | Let me revert my over-complicated fix and look at how the FK value is read at save time. The key insight: `pre_save` reads `product_id` from instance dict. The proper fix is to make `pre_save` re-read from the cached related object when its pk has changed. | Now let me look at the ForeignKey `pre_save` to make it re-read from the cached related object.。

写入结果：Replaced in /testbed/django/db/models/fields/related_descriptors.py: 211 chars → 879 chars (content changed) | Replaced in /testbed/django/db/models/fields/related_descriptors.py: 879 chars → 211 chars (content changed)。

### 69. django__django-14017

类别：changed_then_stalled_or_reverted；session=2026-06-26T13-47-27；write_count=4；changed=2；noop=1；old_str_not_found=1；loop=False。

问题：Q(...) & Exists(...) raises a TypeError Description Exists(...) & Q(...) works, but Q(...) & Exists(...) raise a TypeError Here's a minimal example: In [3]: Exists(Product.objects.all()) & Q() Out[3]: <Q: (AND: <django.db.models.expressions.Exists object at 0x...

最后模型自述：Wait — `__rand__`/`__ror__` only get called when the left operand doesn't define `__and__`/`__or__`. But `Exists` is an `Expression` which may define `__and__`/`__or__` via the base `Expression` class. Let me check whether `Exists` defines these operators, sin... | Now I understand the full picture. `Exists` is `conditional=True`, so `Exists & Q` calls `Expression.__and__` → `Q(self) & Q(other)` which works. But `Q & Exists` calls `Q.__and__` → `_combine` which raises `TypeError` because `Exists` isn't a `Q`. The real fi... | The `__rand__`/`__ror__` won't be reached because `Expression` defines them. The real fix is in `Q._combine`: wrap non-Q conditional expressions in a `Q` instead of raising. Let me fix that. | Now the real fix: `Expression.__and__`/`__or__` already handle `Exists & Q` by calling `Q(self) & Q(other)`. The problem is `Q & Exists` calls `Q.__and__` → `_combine` raises `TypeError`. The cleanest minimal fix is to make `Q.__and__`/`__or__` wrap non-Q cond... | The file is already in the reverted state (no `__rand__`/`__ror__`). Now I'll apply the real fix: make `Q.__and__`/`__or__` wrap non-Q conditional expressions in a `Q`.。

写入结果：Replaced in django/db/models/query_utils.py: 766 chars → 920 chars (content changed) | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in django/db/models/query_utils.py. | Replaced in django/db/models/query_utils.py: 920 chars → 766 chars (content changed) | Error: old_str not found in django/db/models/query_utils.py. Make sure the text matches exactly (including whitespace). If the edit keeps failing to match, use the apply_patch tool instead.。

### 71. django__django-14238

类别：only_noop_or_failed_write；session=2026-06-26T13-54-30；write_count=1；changed=0；noop=0；old_str_not_found=0；loop=False。

问题：DEFAULT_AUTO_FIELD subclass check fails for subclasses of BigAutoField and SmallAutoField. Description Set DEFAULT_AUTO_FIELD = "example.core.models.MyBigAutoField" , with contents of example.core.models: from django.db import models class MyBigAutoField(model...

最后模型自述：This is an older Django without the AutoFieldMeta feature. Let me find where AutoField is defined and where the subclass check happens. | No matches — this version doesn't have the feature yet. Let me check the options.py and the test files to understand what's expected. | The test files exist, so the feature must be partially there. Let me look at the test files and the actual source. | Empty output is odd. Let me check file sizes. | The files are empty (0 lines). Let me check the actual state of the repo more carefully.。

写入结果：Error: Primary key 'example.core.models.MyBigAutoField' referred by DEFAULT_AUTO_FIELD must subclass AutoField.。

### 77. django__django-14667

类别：changed_but_final_diff_empty；session=2026-06-26T14-22-45；write_count=1；changed=1；noop=0；old_str_not_found=0；loop=False。

问题：QuerySet.defer() doesn't clear deferred field when chaining with only(). Description Considering a simple Company model with four fields: id, name, trade_number and country. If we evaluate a queryset containing a .defer() following a .only(), the generated sql...

最后模型自述：The issue: when `defer` is called after `only`, `deferred_loading` is `(existing_immediate, False)`. In `add_deferred_loading`, the `else` branch does `existing.difference(field_names)` — removing the deferred field from the immediate set. But if the deferred ... | The fix: in `add_deferred_loading`, when in immediate-loading mode and the deferred field removes the last immediate field, we should switch to defer mode deferring all fields (so only pk loads). Let me apply the fix. | Wait — deferring all fields with an empty set means "defer nothing" (load all). Let me check `deferred_to_data` to understand empty-set semantics before verifying. | With `defer=True` and empty `field_names`, `deferred_to_data` returns early (line 681-682) → loads all fields. So my fix switching to `(frozenset(), True)` is wrong — that means "load all". The correct behavior: when all immediate fields are deferred away, we ...。

写入结果：Replaced in /testbed/django/db/models/sql/query.py: 351 chars → 631 chars (content changed)。

### 84. django__django-14997

类别：only_noop_or_failed_write；session=2026-06-26T14-48-17；write_count=2；changed=0；noop=0；old_str_not_found=0；loop=False。

问题：Remaking table with unique constraint crashes on SQLite. Description In Django 4.0a1, this model: class Tag(models.Model): name = models.SlugField(help_text="The tag key.") value = models.CharField(max_length=150, help_text="The tag value.") class Meta: orderi...

最后模型自述：The issue is that when remaking the table, the deferred SQL for the unique constraint/index contains expressions with the old table name (e.g. `"table"."column"`), and SQLite prohibits the `.` operator in index expressions. Let me look at how the deferred SQL ... | The `Expressions.__str__` compiles the expression which produces `"table"."column"` (with the alias). When this is used in an index expression on SQLite, the `.` operator is prohibited. The fix: when compiling expressions for index usage, the table alias shoul...。

写入结果：Error: the "." operator prohibited in index expressions | Error: the "." operator prohibited in index expressions。

### 88. django__django-15213

类别：only_noop_or_failed_write；session=2026-06-26T14-59-36；write_count=0；changed=0；noop=0；old_str_not_found=0；loop=False。

问题：ExpressionWrapper for ~Q(pk__in=[]) crashes. Description (last modified by Stefan Brand) Problem Description I'm reducing some Q objects (similar to what is described in ticket:32554. Everything is fine for the case where the result is ExpressionWrapper(Q(pk__...

最后模型自述：The issue is `~Q(pk__in=[])` produces an empty WHERE clause. Let me look at how `pk__in=[]` resolves to `0` and how negation handles empty cases. | The bug: when `pk__in=[]` raises `EmptyResultSet`, the negated case returns `('', [])` — empty SQL. For a negated empty set, it should match everything (return `1`/true), not empty string. Let me check how the non-negated `Q(pk__in=[])` produces `0`. The non-n... | I now understand the bug. When `~Q(pk__in=[])` is compiled as a select annotation, the inner `pk__in=[]` raises `EmptyResultSet`. The negated `WhereNode.as_sql` catches it and returns `('', [])` (line 96-97), which renders as empty SQL `SELECT AS "foo"`. The c... | I have enough understanding. The bug: in `WhereNode.as_sql`, when a negated node's child raises `EmptyResultSet` (making `empty_needed == 0`), it returns `('', [])` — empty SQL — instead of raising `EmptyResultSet`. The compiler's select handler catches `Empty...。

### 94. django__django-15498

类别：changed_then_stalled_or_reverted；session=2026-06-26T16-34-42；write_count=5；changed=3；noop=2；old_str_not_found=0；loop=False。

问题：Fix handling empty string for If-Modified-Since header Description Empty string used to be ignored for If-Modified-Since header, but now raises exception since d6aff369ad3. Fix handling empty string for If-Modified-Since header Description Empty string used to...

最后模型自述：`re.match` returns `None` for empty string (no match for `[^;]+`), so `matches[1]` raises `TypeError`, which isn't caught. Let me fix by treating empty string like None. | That edit didn't actually change anything meaningful. Let me make the real fix - treat empty string as None. | That just duplicated content. Let me view the current state and fix properly. | I duplicated content. Let me rewrite the whole function cleanly. | Now the file is back to original. Let me apply the actual fix - treat empty string like None.。

写入结果：Replaced in django/views/static.py: 148 chars → 199 chars (content changed) | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in django/views/static.py. | Replaced in django/views/static.py: 148 chars → 471 chars (content changed) | Replaced in django/views/static.py: 845 chars → 471 chars (content changed) | Error: str_replace was a no-op — old_str and new_str are identical; nothing changed in django/views/static.py.。

### 96. django__django-15738

类别：only_noop_or_failed_write；session=2026-06-26T16-41-17；write_count=1；changed=0；noop=0；old_str_not_found=0；loop=False。

问题：Models migration with change field foreign to many and deleting unique together. Description (last modified by Simon Charette) I have models like class Authors(models.Model): project_data_set = models.ForeignKey( ProjectDataSet, on_delete=models.PROTECT ) stat...

最后模型自述：I'll explore the migration autodetector to find the root cause. | Let me look at the `_generate_removed_altered_foo_together` and the field-alter generation to understand ordering. | The issue: when a field changes from FK to M2M and `unique_together` is removed, the autodetector generates the `AlterUniqueTogether` removal but the field is being altered. The problem is the dependency — the `AlterUniqueTogether` removal depends on the new f... | Now I understand the structure. When a field changes from FK to M2M, `generate_altered_fields` does `_generate_removed_field` + `_generate_added_field` (RemoveField + AddField). The `unique_together` removal is generated separately. The problem is ordering: th... | The `RemoveField` already depends on `foo_together_change`. The issue is the reverse: the `AlterUniqueTogether` removal needs to happen before the field is removed/altered. Let me check the `add_operation` and how dependencies resolve, plus the `foo_together_c...。

写入结果：Error: Found wrong number (0) of constraints for app_authors(project_data_set, state, start_date)。

### 100. django__django-15814

类别：changed_but_final_diff_empty；session=2026-06-26T16-51-36；write_count=1；changed=1；noop=0；old_str_not_found=0；loop=False。

问题：QuerySet.only() after select_related() crash on proxy models. Description When I optimize a query using select_related() and only() methods from the proxy model I encounter an error: Windows 10; Python 3.10; Django 4.0.5 Traceback (most recent call last): File...

最后模型自述：Now let me verify the fix works with the scenario described in the issue. | The test doesn't exist yet (it's the one that must pass after my fix). Let me verify the fix works by reproducing the issue scenario directly using the runtests infrastructure. | The models need to be in INSTALLED_APPS. Let me run the existing proxy_models test suite to confirm my fix doesn't break anything, and write a quick standalone reproduction using the test app config. | All 29 existing proxy_models tests pass. Now let me verify the actual reported scenario works by adding a temporary test to confirm the fix resolves the crash. | The test_sqlite settings don't include proxy_models in INSTALLED_APPS. Let me check the test models and run the actual reproduction through a temporary test file.。

写入结果：Replaced in django/db/models/sql/query.py: 118 chars → 139 chars (content changed)。
