# OpenCollab tests

Run the complete suite from the repository root with `uv run pytest -q`.
Pytest also collects the Mini Edict example through the repository configuration.

Tests are grouped by the behavior they exercise. Topic filenames and test
function names identify individual regressions within each directory.

| Directory | Behavior |
| --- | --- |
| `agents/` | Base and Single2 profiles, single-agent sessions, turn execution and completion |
| `workflows/` | Teams, topology, messaging, scheduling, built-in and authored workflows |
| `tools/` | Tool execution, files, shell commands, Git operations and executable evidence |
| `runtime/` | Providers, environments, cancellation, context, storage and tracing |
| `interfaces/` | CLI, TUI and public SDK entry points |
| `packaging/` | Architecture boundaries, repository ownership, public metadata and distribution support |
| `support/` | Reusable doubles, preparation functions and test path resolution |

Shared preparation lives in `tests.support` and is imported by its owning
module. A helper used by one topic stays beside that topic's tests. Repository
paths come from `tests.support.paths.REPO_ROOT`, which anchors to the test tree
in a checkout or unpacked source distribution. Its `PACKAGE_ROOT` locates the
actual imported package when a test starts a Python subprocess.

Session call envelopes and recording doubles share
`tests.support.session_runtime_test_support`. Tool execution uses the common
builders in `tests.support.tool_execution_test_support`. A scenario constructs
its own objects with explicit inputs, which also supports several independent
sessions or tools in one test. Fixtures own resources whose setup and cleanup
follow the test lifetime. Preparation shared within one directory stays in a
module next to that directory's topic tests.

The [testing guide](../docs/testing.md) covers focused runs and the existing
repository checks. Example-specific tests remain next to their examples.
