# OpenCollab documentation

Start with the repository [README](../README.md) for installation and runtime
modes, then use the [package guide](../opencollab/README.md) for the CLI, Python
API, and architecture boundary.

## Current documentation

The [Base and Single2 guide](single2.md) explains the default agent profile and
its implementation. The [Duo guide](duo.md) covers dual-coder execution,
candidate selection, evidence, and patch adoption through the CLI and SDK.
The [Chinese Duo guide](duo/README.zh-CN.md) provides localized instructions.
The [Weave guide](weave.md) covers ordered shared-workspace sessions,
executed checks, bounded repair and continuation through the CLI and SDK.
The [configuration guide](../configs/README.md) covers providers, models, and
team files. The [skills guide](../skills/README.md) explains on-demand agent
skills. The [collaborating team guide](2026-08-31-collab-team.md) describes the
three-role configuration, its current launcher and SDK calls, and the original
handoff experiment.

[Offline model and profile inspection](model-inspection.md) documents public
queries used by evaluation and research tooling. The
[evaluation runtime guide](evaluation-runtime.md) describes the framework
surfaces used by external harnesses. Benchmark generation, scoring, reporting,
and remote execution are documented in the
[OpenCollab-Eval README](https://github.com/RISE-X-Lab/OpenCollab-Eval#readme).
The [native test evidence guide](test-evidence.md) explains Bash observation and
the public Git patch parser.

The [benchmark results](results.md) page reports the recorded cross-harness
comparison and paired Duo–Base test. The [Adherence page](adherence.md)
explains runtime records, the conditions for a controlled comparison, and the
measurement of collaboration in declared teams.

[CONTRIBUTING.md](../CONTRIBUTING.md) covers contributor setup and checks,
[the testing guide](testing.md) gives Python and DOM test commands, and
[the test directory guide](../tests/README.md) maps behavior to test topics.
[The scripts guide](../scripts/README.md) covers framework launchers, diagnostics,
and local tool-loop measurement. [RELEASING.md](../RELEASING.md) describes the
current GitHub release process. Report vulnerabilities through
[SECURITY.md](../SECURITY.md).

## Migration guides

Migration documents describe changes in the named release. Use the package
and configuration guides above for the current signatures and defaults.

| Guide | Release changes |
| --- | --- |
| [0.8.0](migrations/0.8.0.md) | Base profile introduction, Single2 default, and 0.8.x follow-up changes |
| [0.7.0](migrations/0.7.0.md) | Duo workflow, evaluation runtime, and native Bash testing |
| [Retiring run_tests](migrations/remove-run-tests.md) | Migration from the removed test runner to native Bash |
| [0.6.0](migrations/0.6.0.md) | Compact public API, team/workflow, lifecycle, evidence, and budget changes |
| [0.5.0](migrations/0.5.0.md) | Audited runtime failures, isolation, configuration, and budgeting |

## Design and integration history

[The ICLR integration record](iclr-integration.md) describes the cumulative
series merged into `main` on October 1, 2026, its source ancestry, and validation
at each recorded stage. [Source coverage](iclr-source-coverage.json) maps the
original ICLR changes to that integration.

Dated design files in this directory and `archive/` preserve earlier design
work. Their branch names, line-number references, test counts, and
implementation status refer to the recorded revision. Current package guides,
source, and executable tests describe the maintained behavior.
