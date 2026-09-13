# Team issue TUI demo

This demo runs a small failing issue through an explicit
`analyst -> coder -> tester` team. The analyst is agent 0 and the TUI entry.

Configure a provider, then run this command from the repository root.

```bash
./scripts/demo_team_issue.sh
```

The launcher copies `workspace/` to a new temporary directory, so each run
starts with one failing and two passing tests without modifying this fixture.
It then starts OpenCollab with the explicit `team.yaml`, the issue as a one-shot
prompt, shared filesystem mode, and the completed-run TUI hold.

The coder and tester use `bash` for the fixture's native test command. They
inherit the interactive entry agent's shell permission and existing command
confirmation policy. Headless integrations must provide a process-isolated
environment for shell execution.

While the team runs, use `Tab` or `Shift+Tab` to follow any live agent. After
the run completes, the same keys inspect the final analyst, coder, and tester
transcripts. Press `q` to close the TUI. The launcher prints and retains the
temporary workspace path for inspection after the run.

Append additional OpenCollab options to the script command.

```bash
./scripts/demo_team_issue.sh --model your-model
```
