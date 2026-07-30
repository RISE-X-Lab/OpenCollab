# Framework scripts

This directory contains OpenCollab framework launchers and provider diagnostics.
Benchmark generation, evaluation, reporting, and remote execution commands are
owned by an external evaluation harness rather than this framework package.

`start_opencollab.sh` prepares the local environment and starts the framework.
`check_dashscope.py` performs an explicit provider connectivity check using the
caller's configuration.

`demo_team_issue.sh` copies a tiny failing Python fixture to a disposable
workspace and runs it through the explicit three-role `analyst`, `coder`, and
`tester` TUI demo. See `examples/team-issue/README.md` for the interaction flow.
