# OpenCollab documentation

Start with the repository [README](../README.md) for setup and the two runtime
modes, then use the [package guide](../opencollab/README.md) for the CLI, Python
API, and architecture boundary.

## Current documentation

The [Base and Single2 guide](single2.md) explains the default agent profile and its implementation. The [configuration guide](../configs/README.md) covers providers,
models, and team files. The [skills guide](../skills/README.md) explains
on-demand agent skills, and the [scripts guide](../scripts/README.md) documents
launchers and provider diagnostics. Contribution checks and vulnerability reporting are in
[CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).
The [testing guide](testing.md) covers development checks and focused test runs.
The [Duo guide](duo.md) covers built-in dual-coder execution, public evidence,
candidate selection, and patch adoption through the CLI and SDK.
The [Chinese guide](duo/README.zh-CN.md) provides the same instructions in Chinese.
The [benchmark results](results.md) page reports the cross-harness comparison
and the paired Duo–Base test. The [Adherence page](adherence.md) explains what
the runtime records, the seven conditions for a controlled comparison and how
ten agent artifacts meet them, and how we measure whether a declared team
collaborated.
The [native test evidence guide](test-evidence.md) explains Bash observation
and the public Git patch parser.

## Migration guides

- The [0.8.0 migration guide](migrations/0.8.0.md) covers Base profile introduction and Single2 as default
- The [0.7.0 migration guide](migrations/0.7.0.md) covers Duo workflow, evaluation runtime, and native Bash testing
- The [remove-run-tests migration guide](migrations/remove-run-tests.md) explains the transition to native Bash testing
- The [0.6.0 migration guide](migrations/0.6.0.md) covers public API, team/workflow, evidence, lifecycle, and budget-contract changes (historical)
- The [0.5.0 migration guide](migrations/0.5.0.md) remains available for historical reference

[The collaborating team](2026-08-31-collab-team.md) documents
`configs/team.collab.yaml`: what the three-role team is, the three ways to run
it, and the four conditions that make a run a team's rather than one seat's.

## Design records

Dated Markdown files in this directory and `archive/` record earlier design
work. Their branch names, line-number anchors, test counts, and implementation
status reflect the repository at the time of writing. The package guide,
current source, and tests define current behavior.

[Offline model and profile inspection](model-inspection.md) describes the public queries used by evaluation and research tooling.
