# SWE-bench Official Evaluation Summary

Evaluation directory: `eval_work/team_temp02_unresolved5_glm52_20260627/official_eval`

Total reports: 4
Resolved: 1
Unresolved: 3
Patch apply failures: 0

| instance | applied | resolved | F2P fail | P2P fail | first failures |
|---|---:|---:|---:|---:|---|
| astropy__astropy-7746 | True | False | 1 | 0 | astropy/wcs/tests/test_wcs.py::test_zero_size_input |
| django__django-11019 | True | False | 14 | 0 | test_combine_media (forms_tests.tests.test_media.FormsMediaTestCase); test_construction (forms_tests.tests.test_media.FormsMediaTestCase) |
| django__django-13321 | True | False | 18 | 0 | test_decode_failure_logged_to_security (sessions_tests.tests.CookieSessionTests); test_decode_legacy (sessions_tests.tests.CookieSessionTests) |
| django__django-13590 | True | True | 0 | 0 |  |
