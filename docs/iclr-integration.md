# ICLR integration series

The target branch is `integrate/iclr-2027`. It starts at main `7e83256f`.
The source boundary is ICLR `53506fdb`. Every pull request in this series targets
the integration branch and builds on the preceding head.

The final stage also merges main `fe36bed5`, including release 0.8.2,
range-aware file-read loop detection, Duo V5 workspace delivery, Duo V6
evidence-based selection rules, and worktree cleanup recovery.

| Order | Head branch | Scope |
| --- | --- | --- |
| 1 | `iclr/01-provider` | Streaming provider responses and timing records |
| 2 | `iclr/02-session` | Session configuration, budgeting, observation, and steering |
| 3 | `iclr/03-team-runtime` | Team profiles, delivery snapshots, adoption, and inspection |
| 4 | `iclr/04-handoff-config` | Handoff variants, card assembly, and the team launcher |
| 5 | `iclr/05-dual-g22-config` | Dual-candidate and mirror team configurations |
| 6 | `iclr/06-s2-config` | Single2 team and ablation configurations |
| 7 | `iclr/07-docs` | Guides and configuration documentation |
| 8 | `iclr/08-complete-history` | Coverage record and the verified ICLR merge ancestry |

Use **Create a merge commit** for these pull requests in this order. Keeping the
cumulative ancestors makes earlier changes disappear from later PR comparisons
as they are accepted. The final PR merges the independently verified complete
integration, which carries the ICLR source as a parent. This records the source
history only after every remaining source change has a destination.

After accepting the complete series, the following check succeeds.

```sh
git merge-base --is-ancestor 53506fdb integrate/iclr-2027
```

[Source coverage](iclr-source-coverage.json) records each path changed by ICLR
relative to the shared Git ancestor. Entries identify unchanged source content,
adapted code, retained current-main behavior, and relocated tests. The complete
runtime and configuration tree was independently compared with the staged
series before the final history merge.

Current main supplies Base's Single2 mapping, the model capability defaults,
strict editing defaults, lifecycle safety, and cancellation-safe stop notices.
The additional ICLR editing behavior is available through explicit
`normalize_hunks` and `relocate_expected` options. Provider timing retains its
compatibility fields with an explicit first-protocol-event meaning; request
tool observation identifies its application-layer sampling point.

The corrected main handoff entry remains `team.handoff.experiment.yaml`.
The original ICLR primary treatment is available as
`team.handoff.primary-legacy.yaml`, and the integrated evaluation registry uses
that name for the historical primary condition. Legacy cards retain their
recorded treatment text. The card generator checks all 40 assembled cards.

The final complete source and series each passed 3702 tests, whole-repository
Ruff, and the existing four architecture checks. After merging main `fe36bed5`,
the complete integration passed 3788 tests, whole-repository Ruff, dependency
hygiene, and the same four architecture checks. Earlier stage results are
recorded in their pull request descriptions. The OCE series uses the public
team, environment, model-inspection, and profile-tool interfaces from this
complete OC series.
