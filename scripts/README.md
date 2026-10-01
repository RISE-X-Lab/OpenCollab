# Framework scripts

This directory contains OpenCollab framework launchers and provider diagnostics.
[OpenCollab-Eval](https://github.com/RISE-X-Lab/OpenCollab-Eval) contains the
benchmark generation, evaluation, reporting, and remote execution commands.

Use `uv run opencollab` as the standard development entry point.
`start_opencollab.sh` remains a compatibility launcher for callers that need
its physical-path handling.
`check_dashscope.py` performs an explicit provider connectivity check using the
caller's configuration.

`benchmark_tool_loop.py` compares framework tool-processing overhead between two
explicit source checkouts using mock providers and disposable local workspaces.
See `docs/development/tool-loop-progress.md` for scenarios and result fields.

`demo_team_issue.sh` copies a tiny failing Python fixture to a disposable
workspace and runs it through the explicit three-role `analyst`, `coder`, and
`tester` TUI demo. See `examples/team-issue/README.md` for the interaction flow.
