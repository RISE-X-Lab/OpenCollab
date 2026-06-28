# Temperature 0.0 Correctness And Anomaly Summary

Run directory: `eval_work/team_temp00_unresolved22_glm52_20260627`
Official evaluated: 0 / 22
Resolved: 0; unresolved: 0
Generation failures or empty patches: 1
Generation loop/budget signals: 8
Official-eval log anomaly instances: 0

| Instance | Gen status | Gen anomaly | Applied | Resolved | F2P fail | P2P fail | Eval anomalies | Evidence |
|---|---|---|---:|---:|---:|---:|---:|---|
| `astropy__astropy-14365` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `astropy__astropy-7746` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-11019` | ok | budget_exceeded | None | None | None | None | 0 | budget line 175: {"reason": "budget_exceeded", "aid": 1} |
| `django__django-11283` | ok | loop_or_repetition_marker | None | None | None | None | 0 | loop line 197: {"tool": "run_tests", "count": 3, "aid": 2} |
| `django__django-11564` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-11630` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-11742` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-11848` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-11905` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-12470` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-13321` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-13590` | ok | loop_or_repetition_marker | None | None | None | None | 0 | loop line 92: {"tool": "bash", "count": 3, "aid": 1} |
| `django__django-13660` | ok | loop_or_repetition_marker | None | None | None | None | 0 | loop line 162: {"tool": "run_tests", "count": 3, "aid": 2} |
| `django__django-13768` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-14017` | ok | budget_exceeded | None | None | None | None | 0 | budget line 305: {"reason": "budget_exceeded", "aid": 0} |
| `django__django-14155` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-14730` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-15202` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-15213` | ok | budget_exceeded | None | None | None | None | 0 | budget line 346: {"reason": "budget_exceeded", "aid": 0} |
| `django__django-15252` | ok | budget_exceeded | None | None | None | None | 0 | budget line 319: {"reason": "budget_exceeded", "aid": 2} |
| `django__django-15695` | ok | no_obvious_loop_signal | None | None | None | None | 0 | no obvious generation anomaly |
| `django__django-15738` | error_rc1 | loop_or_repetition_marker | None | None | None | None | 0 | loop line 93: {"tool": "file_read", "count": 8, "aid": 0} |
