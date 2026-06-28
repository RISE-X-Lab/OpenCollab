# Empty Patch Harness Research

Total empty patches: 19.
Rows with at least one successful file_write receipt: 11.
Rows with direct harness-suspect readback signal: 5.

Research class counts:
- harness_suspect_write_ack_without_persisted_change: 2
- model_reverted_or_cleaned_change: 5
- model_no_successful_file_write: 8
- harness_or_late_cleanup_suspect: 1
- ambiguous_successful_write_but_empty_final_diff: 3

| # | instance | prior class | research class | successful writes | harness signal | key evidence |
|---:|---|---|---|---:|---|---|
| 6 | `astropy__astropy-7746` | changed_but_final_diff_empty | harness_suspect_write_ack_without_persisted_change | 2 | file_write_returned_success_but_later_file_read_lacked_added_signature | The traceback shows line 1225 still calls `func(xy, origin)` directly — my edit isn't taking. There may be a compiled `.pyc` or the file content differs. Let me check the actual current content. \| The str_replace reported success but the file content didn't change — likely whitespace mismatch. Let me check for trailing whitespace and use a more targeted unique match. |
| 8 | `django__django-10924` | changed_then_stalled_or_reverted | model_reverted_or_cleaned_change | 2 | none | The test file doesn't exist yet — it'll be added by the grader. Let me check the test_promises test and the models to understand the expected behavior, then apply the fix. \| Now let me apply the fix. The model field's `formfield` should resolve a callable path, and the form field should resolve a callable path in its `__init__`. |
| 10 | `django__django-11019` | only_noop_or_failed_write | model_no_successful_file_write | 0 | none | Let me trace the actual issue carefully. The problem is the warning fires for `text-editor-extras.js` / `text-editor.js`. Let me reproduce first. \| Now I understand. The `_js` property merges lists pairwise via `merge`. The issue is that `merge` is called on already-merged results, and the warning logic is too aggressive. The real fix: the warning should only fire when there's a genuine conflict betwee... |
| 14 | `django__django-11133` | changed_then_stalled_or_reverted | harness_or_late_cleanup_suspect | 1 | none | I'll rewrite the whole duplicated region in one create-style replacement by reading the file and writing it back. Let me just use sed to remove the duplicate block and add memoryview handling. \| Now the duplicate is removed. Let me add memoryview handling. |
| 23 | `django__django-11797` | only_noop_or_failed_write | model_no_successful_file_write | 0 | none | The test doesn't exist yet — it'll be added during grading. Let me look at the test_sqlite settings and reproduce properly. \| The test_sqlite doesn't define INSTALLED_APPS. Let me reproduce via the runtests infrastructure instead, but first let me just understand the bug from the code. The issue: when a sliced query `a[:1]` is used as RHS of a filter, the subquery's GROUP BY becom... |
| 30 | `django__django-12113` | only_noop_or_failed_write | model_no_successful_file_write | 0 | none | Now I understand the test. The base `test_db_signature` returns a 4-tuple with `_get_test_db_name()` as element [3]. The SQLite override returns a different signature. The test `test_custom_test_name` expects `signature[3] == 'hodor'`. Now let me focus on t... \| The root cause: with persistent SQLite files, the `_create_test_db` returns early on `keepdb` but the connection that was opened (e.g. during `ensure_connection` or by the test runner's setup) still holds a lock on the file. The base `create_test_db` calls ... |
| 39 | `django__django-12589` | only_noop_or_failed_write | model_no_successful_file_write | 0 | none | The issue: when `.values("status")` is called, `query.group_by` contains the string `"status"`. In `get_group_by`, `resolve_ref("status")` returns the annotation (the Subquery) directly because `summarize=False`. Then in the select loop, the annotation's `g... \| Let me look at the `Ref` class and the `get_group_by_cols` for the base Expression. |
| 47 | `django__django-13028` | changed_then_stalled_or_reverted | model_reverted_or_cleaned_change | 3 | file_write_returned_success_but_later_file_read_lacked_added_signature | The file is already back to original. Now apply the correct fix: only enforce the `filterable` opt-out on actual expressions (those with `as_sql`), not plain model instances used as RHS values. \| That was wrong — I duplicated the method. Let me revert to a clean state and apply the correct minimal fix. |
| 49 | `django__django-13158` | changed_but_final_diff_empty | ambiguous_successful_write_but_empty_final_diff | 1 | none | The issue: when `none()` is called on a combined query, `set_empty()` adds a `NothingNode` to `self.where`, but combinator queries don't use `where` — they use `combined_queries`. So `is_empty()` returns False and the empty result isn't honored. The fix is ... \| Now let me verify the fix works. The key scenario: `none()` on a union query should return no results. |
| 59 | `django__django-13590` | changed_but_final_diff_empty | ambiguous_successful_write_but_empty_final_diff | 1 | none | The test doesn't exist yet (it's the test that must pass after the fix). Let me check the existing test class to understand the pattern, then verify the fix works against the actual queryset behavior: \| The fix is already applied. Let me verify it works against the actual queryset behavior with a named tuple range lookup: |
| 67 | `django__django-13964` | changed_then_stalled_or_reverted | model_reverted_or_cleaned_change | 2 | none | Let me revert my over-complicated fix and look at how the FK value is read at save time. The key insight: `pre_save` reads `product_id` from instance dict. The proper fix is to make `pre_save` re-read from the cached related object when its pk has changed. \| Now let me look at the ForeignKey `pre_save` to make it re-read from the cached related object. |
| 69 | `django__django-14017` | changed_then_stalled_or_reverted | model_reverted_or_cleaned_change | 2 | file_write_returned_success_but_later_file_read_lacked_added_signature | Now the real fix: `Expression.__and__`/`__or__` already handle `Exists & Q` by calling `Q(self) & Q(other)`. The problem is `Q & Exists` calls `Q.__and__` → `_combine` raises `TypeError`. The cleanest minimal fix is to make `Q.__and__`/`__or__` wrap non-Q c... \| The file is already in the reverted state (no `__rand__`/`__ror__`). Now I'll apply the real fix: make `Q.__and__`/`__or__` wrap non-Q conditional expressions in a `Q`. |
| 71 | `django__django-14238` | only_noop_or_failed_write | model_no_successful_file_write | 0 | none | Empty output is odd. Let me check file sizes. \| The files are empty (0 lines). Let me check the actual state of the repo more carefully. |
| 77 | `django__django-14667` | changed_but_final_diff_empty | harness_suspect_write_ack_without_persisted_change | 1 | file_write_returned_success_but_later_file_read_lacked_added_signature | Wait — deferring all fields with an empty set means "defer nothing" (load all). Let me check `deferred_to_data` to understand empty-set semantics before verifying. \| With `defer=True` and empty `field_names`, `deferred_to_data` returns early (line 681-682) → loads all fields. So my fix switching to `(frozenset(), True)` is wrong — that means "load all". The correct behavior: when all immediate fields are deferred away, ... |
| 84 | `django__django-14997` | only_noop_or_failed_write | model_no_successful_file_write | 0 | none | The issue is that when remaking the table, the deferred SQL for the unique constraint/index contains expressions with the old table name (e.g. `"table"."column"`), and SQLite prohibits the `.` operator in index expressions. Let me look at how the deferred S... \| The `Expressions.__str__` compiles the expression which produces `"table"."column"` (with the alias). When this is used in an index expression on SQLite, the `.` operator is prohibited. The fix: when compiling expressions for index usage, the table alias sh... |
| 88 | `django__django-15213` | only_noop_or_failed_write | model_no_successful_file_write | 0 | none | I now understand the bug. When `~Q(pk__in=[])` is compiled as a select annotation, the inner `pk__in=[]` raises `EmptyResultSet`. The negated `WhereNode.as_sql` catches it and returns `('', [])` (line 96-97), which renders as empty SQL `SELECT AS "foo"`. Th... \| I have enough understanding. The bug: in `WhereNode.as_sql`, when a negated node's child raises `EmptyResultSet` (making `empty_needed == 0`), it returns `('', [])` — empty SQL — instead of raising `EmptyResultSet`. The compiler's select handler catches `Em... |
| 94 | `django__django-15498` | changed_then_stalled_or_reverted | model_reverted_or_cleaned_change | 3 | file_write_returned_success_but_later_file_read_lacked_added_signature | I duplicated content. Let me rewrite the whole function cleanly. \| Now the file is back to original. Let me apply the actual fix - treat empty string like None. |
| 96 | `django__django-15738` | only_noop_or_failed_write | model_no_successful_file_write | 0 | none | Now I understand the structure. When a field changes from FK to M2M, `generate_altered_fields` does `_generate_removed_field` + `_generate_added_field` (RemoveField + AddField). The `unique_together` removal is generated separately. The problem is ordering:... \| The `RemoveField` already depends on `foo_together_change`. The issue is the reverse: the `AlterUniqueTogether` removal needs to happen before the field is removed/altered. Let me check the `add_operation` and how dependencies resolve, plus the `foo_togethe... |
| 100 | `django__django-15814` | changed_but_final_diff_empty | ambiguous_successful_write_but_empty_final_diff | 1 | none | All 29 existing proxy_models tests pass. Now let me verify the actual reported scenario works by adding a temporary test to confirm the fix resolves the crash. \| The test_sqlite settings don't include proxy_models in INSTALLED_APPS. Let me check the test models and run the actual reproduction through a temporary test file. |

## Successful file_write readback details

### 6. astropy__astropy-7746

Research class: `harness_suspect_write_ack_without_persisted_change`.

- `file_write` at message 7 path `/testbed/astropy/wcs/wcs.py`: Replaced in /testbed/astropy/wcs/wcs.py: 1181 chars → 1348 chars (content changed)
- added signatures: `if xy.shape[0] == 0:`; `return [np.array([]) for i in range(self.naxis)]`; `return np.array([])`
- later bash message 11: contains_added=False preview=Exit code: 1 stderr: Traceback (most recent call last): File "<string>", line 19, in <module> File "/testbed/astropy/wcs/wcs.py", line 1354, in wcs_pix2world 'output', *args, **kwargs) File "/testbed/astropy/wcs/wcs.p...
- later file_read message 13: contains_added=False preview=File: /testbed/astropy/wcs/wcs.py (3299 lines total, showing 1214-1238) 1214 def _return_list_of_arrays(axes, origin): 1215 try: 1216 axes = np.broadcast_arrays(*axes) 1217 except ValueError: 1218 raise ValueError( 12...
- later bash message 17: contains_added=False preview=Exit code: 1 stderr: Traceback (most recent call last): File "<string>", line 18, in <module> File "/testbed/astropy/wcs/wcs.py", line 1354, in wcs_pix2world 'output', *args, **kwargs) File "/testbed/astropy/wcs/wcs.p...
- later file_read message 19: contains_added=False preview=File: /testbed/astropy/wcs/wcs.py (3299 lines total, showing 1221-1242) 1221 xy = np.hstack([x.reshape((x.size, 1)) for x in axes]) 1222 1223 if ra_dec_order and sky == 'input': 1224 xy = self._denormalize_sky(xy) 122...

- `file_write` at message 15 path `/testbed/astropy/wcs/wcs.py`: Replaced in /testbed/astropy/wcs/wcs.py: 853 chars → 1020 chars (content changed)
- added signatures: `if xy.shape[0] == 0:`; `return [np.array([]) for i in range(self.naxis)]`; `return np.array([])`
- later bash message 17: contains_added=False preview=Exit code: 1 stderr: Traceback (most recent call last): File "<string>", line 18, in <module> File "/testbed/astropy/wcs/wcs.py", line 1354, in wcs_pix2world 'output', *args, **kwargs) File "/testbed/astropy/wcs/wcs.p...
- later file_read message 19: contains_added=False preview=File: /testbed/astropy/wcs/wcs.py (3299 lines total, showing 1221-1242) 1221 xy = np.hstack([x.reshape((x.size, 1)) for x in axes]) 1222 1223 if ra_dec_order and sky == 'input': 1224 xy = self._denormalize_sky(xy) 122...

### 8. django__django-10924

Research class: `model_reverted_or_cleaned_change`.

- `file_write` at message 12 path `/testbed/django/db/models/fields/__init__.py`: Replaced in /testbed/django/db/models/fields/__init__.py: 350 chars → 135 chars (content changed)

- `file_write` at message 14 path `/testbed/django/db/models/fields/__init__.py`: Replaced in /testbed/django/db/models/fields/__init__.py: 135 chars → 350 chars (content changed)
- added signatures: `'match': self.match,`; `'recursive': self.recursive,`; `'form_class': forms.FilePathField,`; `'allow_files': self.allow_files,`

### 14. django__django-11133

Research class: `harness_or_late_cleanup_suspect`.

- `file_write` at message 14 path `/testbed/django/http/response.py`: Replaced in /testbed/django/http/response.py: 238 chars → 1005 chars (content changed)
- added signatures: `def make_bytes(self, value):`; `"""Turn a value into a bytestring encoded in the output charset."""`; `# Per PEP 3333, this response body must be bytes. To avoid returning`; `# an instance of a subclass, this function returns `bytes(value)`.`
- later file_read message 16: contains_added=True preview=File: /testbed/django/http/response.py (574 lines total, showing 220-259) 220 221 # Common methods used by subclasses 222 223 def make_bytes(self, value): 224 """Turn a value into a bytestring encoded in the output ch...
- later file_read message 24: contains_added=True preview=File: /testbed/django/http/response.py (558 lines total, showing 223-240) 223 def make_bytes(self, value): 224 """Turn a value into a bytestring encoded in the output charset.""" 225 # Per PEP 3333, this response body...

### 47. django__django-13028

Research class: `model_reverted_or_cleaned_change`.

- `file_write` at message 7 path `/testbed/django/db/models/sql/query.py`: Replaced in /testbed/django/db/models/sql/query.py: 496 chars → 542 chars (content changed)
- added signatures: `if hasattr(expression, 'resolve_expression') and hasattr(expression, 'get_source_expressions'):`
- later file_read message 13: contains_added=False preview=File: /testbed/django/db/models/sql/query.py (2414 lines total, showing 1125-1136) 1125 def check_filterable(self, expression): 1126 """Raise an error if expression cannot be used in a WHERE clause.""" 1127 if not get...
- later file_read message 19: contains_added=False preview=File: /testbed/django/db/models/sql/query.py (2414 lines total, showing 1125-1149) 1125 def check_filterable(self, expression): 1126 """Raise an error if expression cannot be used in a WHERE clause.""" 1127 if not get...

- `file_write` at message 17 path `/testbed/django/db/models/sql/query.py`: Replaced in /testbed/django/db/models/sql/query.py: 496 chars → 578 chars (content changed)
- added signatures: `def _add_q(self, q_object, used_aliases, allow_joins=True, split_subq=True):`
- later file_read message 19: contains_added=False preview=File: /testbed/django/db/models/sql/query.py (2414 lines total, showing 1125-1149) 1125 def check_filterable(self, expression): 1126 """Raise an error if expression cannot be used in a WHERE clause.""" 1127 if not get...
- later file_read message 25: contains_added=False preview=File: /testbed/django/db/models/sql/query.py (2414 lines total, showing 1125-1149) 1125 def check_filterable(self, expression): 1126 """Raise an error if expression cannot be used in a WHERE clause.""" 1127 if not get...

- `file_write` at message 23 path `/testbed/django/db/models/sql/query.py`: Replaced in /testbed/django/db/models/sql/query.py: 496 chars → 996 chars (content changed)
- added signatures: `def _check_filterable(self, expression):`; `"""Raise an error if expression cannot be used in a WHERE clause."""`; `if not getattr(expression, 'filterable', True):`; `raise NotSupportedError(`
- later file_read message 25: contains_added=True preview=File: /testbed/django/db/models/sql/query.py (2414 lines total, showing 1125-1149) 1125 def check_filterable(self, expression): 1126 """Raise an error if expression cannot be used in a WHERE clause.""" 1127 if not get...

### 49. django__django-13158

Research class: `ambiguous_successful_write_but_empty_final_diff`.

- `file_write` at message 23 path `/testbed/django/db/models/sql/query.py`: Replaced in /testbed/django/db/models/sql/query.py: 99 chars → 207 chars (content changed)
- added signatures: `return (`; `any(isinstance(c, NothingNode) for c in self.where.children)`; `or self.combinator and all(q.is_empty() for q in self.combined_queries)`
- later bash message 25: contains_added=False preview=Exit code: 1 stderr: Traceback (most recent call last): File "<string>", line 4, in <module> File "/testbed/django/__init__.py", line 19, in setup configure_logging(settings.LOGGING_CONFIG, settings.LOGGING) File "/te...

### 59. django__django-13590

Research class: `ambiguous_successful_write_but_empty_final_diff`.

- `file_write` at message 7 path `/testbed/django/db/models/sql/query.py`: Replaced in /testbed/django/db/models/sql/query.py: 330 chars → 375 chars (content changed)
- added signatures: `self.resolve_lookup_value(sub_value, can_reuse, allow_joins)`; `for sub_value in value`
- later bash message 17: contains_added=False preview=Exit code: 0 stdout: E ====================================================================== ERROR: test_range_lookup_namedtuple (unittest.loader._FailedTest) ---------------------------------------------------------...

### 67. django__django-13964

Research class: `model_reverted_or_cleaned_change`.

- `file_write` at message 12 path `/testbed/django/db/models/fields/related_descriptors.py`: Replaced in /testbed/django/db/models/fields/related_descriptors.py: 211 chars → 879 chars (content changed)
- added signatures: `# Use the related instance's primary key value when it has been`; `# set, otherwise leave the foreign key value unset so that it's`; `# populated from the related instance when the parent is saved.`; `# This avoids storing a stale value (e.g. an empty string) when`
- later file_read message 16: contains_added=True preview=File: /testbed/django/db/models/fields/related_descriptors.py (1215 lines total, showing 253-277) 253 254 # Set the values of the related field. 255 else: 256 for lh_field, rh_field in self.field.related_fields: 257 s...

- `file_write` at message 18 path `/testbed/django/db/models/fields/related_descriptors.py`: Replaced in /testbed/django/db/models/fields/related_descriptors.py: 879 chars → 211 chars (content changed)

### 69. django__django-14017

Research class: `model_reverted_or_cleaned_change`.

- `file_write` at message 11 path `django/db/models/query_utils.py`: Replaced in django/db/models/query_utils.py: 766 chars → 920 chars (content changed)
- added signatures: `def __rand__(self, other):`; `return self._combine(other, self.AND)`; `def __ror__(self, other):`; `return self._combine(other, self.OR)`
- later file_read message 23: contains_added=True preview=File: django/db/models/query_utils.py (324 lines total, showing 60-71) 60 61 def __or__(self, other): 62 return self._combine(other, self.OR) 63 64 def __and__(self, other): 65 return self._combine(other, self.AND) 66...

- `file_write` at message 19 path `django/db/models/query_utils.py`: Replaced in django/db/models/query_utils.py: 920 chars → 766 chars (content changed)
- later file_read message 23: contains_added=False preview=File: django/db/models/query_utils.py (324 lines total, showing 60-71) 60 61 def __or__(self, other): 62 return self._combine(other, self.OR) 63 64 def __and__(self, other): 65 return self._combine(other, self.AND) 66...

### 77. django__django-14667

Research class: `harness_suspect_write_ack_without_persisted_change`.

- `file_write` at message 17 path `/testbed/django/db/models/sql/query.py`: Replaced in /testbed/django/db/models/sql/query.py: 351 chars → 631 chars (content changed)
- added signatures: `# A set of immediate load names that is empty means that all`; `# fields are deferred. To avoid loading all fields, defer all`; `# fields instead.`; `if not self.deferred_loading[0]:`
- later file_read message 21: contains_added=False preview=File: /testbed/django/db/models/sql/query.py (2452 lines total, showing 666-710) 666 def deferred_to_data(self, target, callback): 667 """ 668 Convert the self.deferred_loading data structure to an alternate data 669 ...
- later file_read message 23: contains_added=False preview=File: /testbed/django/db/models/sql/query.py (2452 lines total, showing 710-734) 710 if model == opts.model: 711 model = cur_model 712 if not is_reverse_o2o(field): 713 add_to_dict(seen, model, field) 714 715 if defer...
- later file_read message 25: contains_added=False preview=File: /testbed/django/db/models/sql/query.py (2452 lines total, showing 2083-2094) 2083 existing, defer = self.deferred_loading 2084 if defer: 2085 # Add to existing deferred names. 2086 self.deferred_loading = existi...

### 94. django__django-15498

Research class: `model_reverted_or_cleaned_change`.

- `file_write` at message 13 path `django/views/static.py`: Replaced in django/views/static.py: 148 chars → 199 chars (content changed)
- added signatures: `header_mtime = parse_http_date(matches[1])`
- later file_read message 19: contains_added=True preview=File: django/views/static.py (150 lines total, showing 114-148) 114 def was_modified_since(header=None, mtime=0, size=0): 115 """ 116 Was something modified since the user last downloaded it? 117 118 header 119 This i...
- later file_read message 21: contains_added=False preview=File: django/views/static.py (150 lines total, showing 148-150) 148 except (AttributeError, ValueError, OverflowError): 149 return True 150 return False

- `file_write` at message 17 path `django/views/static.py`: Replaced in django/views/static.py: 148 chars → 471 chars (content changed)
- added signatures: `header_mtime = parse_http_date(matches[1])`; `header_len = matches[3]`; `if header_len and int(header_len) != size:`; `raise ValueError`
- later file_read message 19: contains_added=True preview=File: django/views/static.py (150 lines total, showing 114-148) 114 def was_modified_since(header=None, mtime=0, size=0): 115 """ 116 Was something modified since the user last downloaded it? 117 118 header 119 This i...
- later file_read message 21: contains_added=True preview=File: django/views/static.py (150 lines total, showing 148-150) 148 except (AttributeError, ValueError, OverflowError): 149 return True 150 return False

- `file_write` at message 23 path `django/views/static.py`: Replaced in django/views/static.py: 845 chars → 471 chars (content changed)

### 100. django__django-15814

Research class: `ambiguous_successful_write_but_empty_final_diff`.

- `file_write` at message 5 path `django/db/models/sql/query.py`: Replaced in django/db/models/sql/query.py: 118 chars → 139 chars (content changed)
- added signatures: `opts = cur_model._meta.concrete_model._meta`
- later bash message 7: contains_added=False preview=Exit code: 1 stderr: Traceback (most recent call last): File "<string>", line 4, in <module> File "/testbed/django/__init__.py", line 19, in setup configure_logging(settings.LOGGING_CONFIG, settings.LOGGING) File "/te...
- later bash message 11: contains_added=False preview=Exit code: 1 stderr: Traceback (most recent call last): File "<string>", line 4, in <module> File "/testbed/django/__init__.py", line 19, in setup configure_logging(settings.LOGGING_CONFIG, settings.LOGGING) File "/te...
- later bash message 15: contains_added=False preview=Exit code: 0 stdout: E ====================================================================== ERROR: test_select_related_only (unittest.loader._FailedTest) -------------------------------------------------------------...
- later bash message 17: contains_added=False preview=Exit code: 1 stderr: Traceback (most recent call last): File "<string>", line 11, in <module> File "/testbed/tests/proxy_models/models.py", line 22, in <module> class Person(models.Model): File "/testbed/django/db/mod...

