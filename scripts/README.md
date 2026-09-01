# Framework scripts

This directory contains OpenCollab framework launchers and provider diagnostics.
[OpenCollab-Eval](https://github.com/RISE-X-Lab/OpenCollab-Eval) contains the
benchmark generation, evaluation, reporting, and remote execution commands.

Use `uv run opencollab` as the standard development entry point.
`start_opencollab.sh` remains a compatibility launcher for callers that need
its physical-path handling.
`check_dashscope.py` performs an explicit provider connectivity check using the
caller's configuration.

`demo_team_issue.sh` copies a tiny failing Python fixture to a disposable
workspace and runs it through the explicit three-role `analyst`, `coder`, and
`tester` TUI demo. See `examples/team-issue/README.md` for the interaction flow.

`run_collab_team.py` runs the reusable three-role team in
`configs/team.collab.yaml` against a workspace. It exists because that team file
needs its roster seated before the first model call — no role in it holds
`spawn_agent`, and `uv run opencollab` has no flag for prebuilding — so a run
started any other way would seat the Analyst alone and produce a solo run that
reads like a team's.

```bash
scripts/run_collab_team.py --workspace ./repo --prompt "fix the failing test" \
    --allow-unisolated-shell
```

The handoff payload between roles is a commit sha, so the roles need a shell
that can run `git`. Outside a sandboxed environment that requires
`--allow-unisolated-shell`, which lets the roles execute commands on the host:
pass it only for a workspace you trust.
