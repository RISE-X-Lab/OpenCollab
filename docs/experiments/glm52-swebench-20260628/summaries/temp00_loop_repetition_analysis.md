# Temperature 0.0 Team Run Loop/Repetition Analysis

Run directory: `eval_work/team_temp00_unresolved22_glm52_20260627`
Temperature: `0.0`; thinking: `false`.
Predictions: 22; metrics: 22; success tokens: 6,535,787; known failed-attempt tokens: 504,236.
Cost range so far, including known failed attempts: $9.8560 to $30.9761.

| Instance | Patch bytes | Tokens | Lead steps | Classification | Main evidence |
|---|---:|---:|---:|---|---|
| `astropy__astropy-14365` | 617 | 61237 | 5 | no_obvious_loop_signal | no obvious marker |
| `astropy__astropy-7746` | 1106 | 136083 | 7 | no_obvious_loop_signal | no obvious marker |
| `django__django-13321` | 720 | 176933 | 8 | no_obvious_loop_signal | no obvious marker |
| `django__django-11019` | 5347 | 514364 | 12 | budget_exceeded | budget marker line 175: {"reason": "budget_exceeded", "aid": 1} |
| `django__django-11283` | 921 | 270749 | 13 | loop_or_repetition_marker | loop marker line 197 (loop_detected): {"tool": "run_tests", "count": 3, "aid": 2} |
| `django__django-11564` | 727 | 272169 | 17 | no_obvious_loop_signal | no obvious marker |
| `django__django-11630` | 1885 | 503970 | 19 | no_obvious_loop_signal | no obvious marker |
| `django__django-11742` | 1578 | 305510 | 19 | no_obvious_loop_signal | no obvious marker |
| `django__django-11848` | 920 | 104304 | 8 | no_obvious_loop_signal | no obvious marker |
| `django__django-11905` | 938 | 197149 | 8 | no_obvious_loop_signal | no obvious marker |
| `django__django-12470` | 816 | 194617 | 22 | no_obvious_loop_signal | no obvious marker |
| `django__django-13590` | 941 | 372419 | 6 | loop_or_repetition_marker | loop marker line 92 (loop_detected): {"tool": "bash", "count": 3, "aid": 1} |
| `django__django-13660` | 1600 | 237894 | 9 | loop_or_repetition_marker | loop marker line 162 (loop_detected): {"tool": "run_tests", "count": 3, "aid": 2} |
| `django__django-13768` | 995 | 93376 | 11 | no_obvious_loop_signal | no obvious marker |
| `django__django-14017` | 1267 | 508882 | 8 | budget_exceeded | budget marker line 305: {"reason": "budget_exceeded", "aid": 0} |
| `django__django-14155` | 783 | 228686 | 12 | no_obvious_loop_signal | no obvious marker |
| `django__django-14730` | 1036 | 260514 | 13 | no_obvious_loop_signal | no obvious marker |
| `django__django-15202` | 1460 | 187143 | 5 | no_obvious_loop_signal | no obvious marker |
| `django__django-15213` | 2042 | 501525 | 16 | budget_exceeded | budget marker line 346: {"reason": "budget_exceeded", "aid": 0} |
| `django__django-15252` | 2064 | 508354 | 20 | budget_exceeded | budget marker line 319: {"reason": "budget_exceeded", "aid": 2} |
| `django__django-15695` | 1287 | 395795 | 13 | no_obvious_loop_signal | no obvious marker |
| `django__django-15738` | 2367 | 504114 | 19 | loop_or_repetition_marker | loop marker line 93 (loop_detected): {"tool": "file_read", "count": 8, "aid": 0} |
