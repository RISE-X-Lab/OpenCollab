# ICLR integration series

The cumulative series was merged into `main` through PR #154 at `a10d99ce` on
October 1, 2026. Both the original ICLR source boundary `45fdf22a` and the final
`integrate/iclr-2027` head are ancestors of current `main`. This page records the
original integration sequence and its validation results. Use the
[package guide](../opencollab/README.md) and
[contributor guide](../CONTRIBUTING.md) for current development.

The target branch was `integrate/iclr-2027`, starting at main `7e83256f`.
The source boundary was ICLR `45fdf22a`. Each pull request targeted the
integration branch and built on the preceding head.

The final stage also merged main `fe36bed5`, including release 0.8.2,
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

The series used **Create a merge commit** in this order. Keeping the cumulative
ancestors removed accepted changes from later PR comparisons. The final PR
merged the independently verified complete integration with the ICLR source as
a parent after every remaining source change had a destination.

The completed source-history relationship can be inspected in the current
checkout.

```sh
git merge-base --is-ancestor 45fdf22a HEAD
git merge-base --is-ancestor a10d99ce HEAD
```

[Source coverage](iclr-source-coverage.json) records each path changed by ICLR
relative to the shared Git ancestor. Entries identify unchanged source content,
adapted code, retained current-main behavior, and relocated tests. The complete
runtime and configuration tree was independently compared with the staged
series before the final history merge.

The integrated main revision supplied Base's Single2 mapping, model capability
defaults,
strict editing defaults, lifecycle safety, and cancellation-safe stop notices.
The additional ICLR editing behavior is available through explicit
`normalize_hunks` and `relocate_expected` options. Provider timing retains its
compatibility fields with an explicit first-protocol-event meaning; request
tool observation identifies its application-layer sampling point.

The corrected main handoff entry was `team.handoff.experiment.yaml`.
The original ICLR primary treatment is available as
`team.handoff.primary-legacy.yaml`, and the integrated evaluation registry uses
that name for the historical primary condition. Legacy cards retain their
recorded treatment text. The card generator checked all 40 assembled cards at
that stage.

The final complete source and series each passed 3702 tests, whole-repository
Ruff, and the existing four architecture checks. After merging main `fe36bed5`,
the complete integration passed 3788 tests, whole-repository Ruff, dependency
hygiene, and the same four architecture checks. Earlier stage results are
recorded in their pull request descriptions. The OCE series uses the public
team, environment, model-inspection, and profile-tool interfaces from this
complete OC series.

The October 1, 2026 synchronization merges main through `86cfe810` for release
0.8.4. It brings execution-fact loop handling, bounded validation recovery,
and actual budget inheritance for candidate workflows into the integration
branch. The merged implementation retains ICLR's optional patch relocation
notes, ordered pending-tool results, and cancellation recovery. Merge commits
preserve both the current main and original ICLR source ancestry. The runtime
merge passed 4065 tests, whole-repository Ruff, dependency hygiene, and the four
existing architecture checks. The final 0.8.4 metadata and SDK validation
passed 19 targeted tests.
