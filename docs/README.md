# OpenCollab documentation

Start with the repository [README](../README.md) for setup and the two runtime
modes, then use the [package guide](../opencollab/README.md) for the CLI, Python
API, and architecture boundary.

## Current documentation

The [configuration guide](../configs/README.md) covers providers,
models, and team files. The [skills guide](../skills/README.md) explains
on-demand agent skills. Contribution checks and vulnerability reporting are in
[CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).
The [testing guide](testing.md) covers development checks and focused test runs.
The [Duo guide](duo.md) covers built-in dual-coder execution, public evidence,
candidate selection, and patch adoption through the CLI and SDK.
The [Chinese guide](duo/README.zh-CN.md) provides the same instructions in Chinese.
The [Single2 guide](single2.md) documents the default Base agent profile and its
execution model.
The [native test evidence guide](test-evidence.md) explains Bash observation
and the public Git patch parser.

Migration guides are available for major releases:
- The [0.6.0 migration guide](migrations/0.6.0.md) lists the public API,
  team/workflow, evidence, lifecycle, and budget-contract changes.
- The [0.5.0 migration guide](migrations/0.5.0.md) remains available for
  upgrades from the previous release.
- The [remove-run-tests guide](migrations/remove-run-tests.md) explains the
  removal of the built-in `run_tests` tool.

## Design records

Dated Markdown files in this directory and `archive/` record earlier design
work. Their branch names, line-number anchors, test counts, and implementation
status reflect the repository at the time of writing. The package guide,
current source, and tests define current behavior.
