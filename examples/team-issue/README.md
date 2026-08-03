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

The launcher enables `--allow-local-child-tests` so the coder and tester can
execute this known fixture on the host. The flag is disabled by default. Use it
only for a trusted workspace because project tests run code outside an OS
process sandbox.

While the team runs, use `Tab` or `Shift+Tab` to follow any live agent. After
the run completes, the same keys inspect the final analyst, coder, and tester
transcripts. Press `q` to close the TUI. The launcher prints and retains the
temporary workspace path for inspection after the run.

Additional OpenCollab options can be appended as shown below.

```bash
./scripts/demo_team_issue.sh --model your-model
```
