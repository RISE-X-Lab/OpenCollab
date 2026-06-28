# SWE-bench Official Evaluation Summary

Evaluation directory: `eval_work/team_mode_unresolved10_glm52_20260627/official_eval`

Total reports: 10
Resolved: 7
Unresolved: 3
Patch apply failures: 0

| instance | applied | resolved | F2P fail | P2P fail | first failures |
|---|---:|---:|---:|---:|---|
| astropy__astropy-14365 | True | False | 1 | 0 | astropy/io/ascii/tests/test_qdp.py::test_roundtrip[True] |
| astropy__astropy-7746 | True | False | 1 | 0 | astropy/wcs/tests/test_wcs.py::test_zero_size_input |
| django__django-11797 | True | True | 0 | 0 |  |
| django__django-11910 | True | True | 0 | 0 |  |
| django__django-12589 | True | True | 0 | 0 |  |
| django__django-12747 | True | True | 0 | 0 |  |
| django__django-13028 | True | True | 0 | 0 |  |
| django__django-13321 | True | False | 18 | 0 | test_decode_failure_logged_to_security (sessions_tests.tests.CookieSessionTests); test_decode_legacy (sessions_tests.tests.CookieSessionTests) |
| django__django-14999 | True | True | 0 | 0 |  |
| django__django-15781 | True | True | 0 | 0 |  |
