# Team issue TUI demo

This demo runs a small failing issue through an explicit
`analyst -> coder -> tester` team. The analyst is agent 0 and the TUI entry.

Configure a provider, then run this command from the repository root.

```bash
./scripts/demo_team_issue.sh --allow-local-child-shell
```

The launcher copies `workspace/` to a new temporary directory, so each run
starts with one failing and two passing tests without modifying this fixture.
It then starts OpenCollab with the explicit `team.yaml`, the issue as a one-shot
prompt, shared filesystem mode, and the completed-run TUI hold.

The coder and tester are spawned dynamically and use `bash` for the fixture's
native test command. `--allow-local-child-shell` explicitly authorizes these
children to run commands on the host under your user account. The temporary
workspace is a copy of the fixture, not an OS process sandbox. Existing command
confirmation still applies. The launcher requires this opt-in before starting
the team. Without it, dynamic children require a process-isolated execution
environment even in interactive mode. Prebuilt teammates retain their separate
entry-agent permission rule.

While the team runs, use `Tab` or `Shift+Tab` to follow any live agent. After
the run completes, the same keys inspect the final analyst, coder, and tester
transcripts. Press `q` to close the TUI. The launcher prints and retains the
temporary workspace path for inspection after the run.

Append additional OpenCollab options to the script command.

```bash
./scripts/demo_team_issue.sh --allow-local-child-shell --model your-model
```
