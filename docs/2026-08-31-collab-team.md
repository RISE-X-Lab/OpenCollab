# The collaborating team and its handoff experiment

`configs/team.collab.yaml` is a three-role team that actually hands work over.
This document explains the declared roles, the current launch routes, and the
workspace handoff measured in the original experiment.

The experiment results and smoke-run transcript below were recorded on
August 31, 2026. The configuration and launch instructions reflect the current
`configs/team.collab.yaml` and `scripts/run_collab_team.py`. The original runs
used `run_tests`, which was removed in 0.7.0. Current roles run native test
commands through Bash. See the [native test evidence guide](test-evidence.md).

## What it is

One self-contained YAML file. Three roles — Analyst (agent 0), Coder, Tester —
with their prompts inline, so the file can be copied anywhere and named on a
command line with no sibling files to fix up.

| | Analyst | Coder | Tester |
|---|---|---|---|
| seat | agent 0; the request arrives here and its answer is read back | teammate | teammate |
| edit tools | `apply_patch`, `file_write` | `apply_patch`, `file_write` | none |
| read/run | `file_read`, `grep`, `bash` | same | same, plus `git_diff` |
| collaboration | `message_agent`, `team_status`, `submit` | same | same |
| workspace | the delivered workspace itself | its own git worktree | its own git worktree |

Topology is all six directed edges. The return edges are load-bearing: with a
closed star (analyst → coder, analyst → tester, nothing back) a prebuilt
teammate has no way to deliver a result, because a prebuilt peer has no join
path.

The Analyst keeps every working tool on purpose. A seat that cannot edit would
hand work over because it has no choice, and the handoffs would then be a fact
about the config rather than about the model. `configs/team.handoff.starved.yaml`
is that other experiment; this file is not it.

## Why the Analyst is commanded and not persuaded

A declared topology is not a used one. Given these same three seats, these same
six edges, and a card that describes the channel and leaves the choice open,
this model sends **zero** messages and does the whole task in seat 0 — 84 runs,
seven phrasings, 80 of them with `message_agent` never called once. The model
states the reason in its own reasoning: *"the cost of messaging + coordination
exceeds just doing it."*

Delegation appears when the card commands it and blocks the alternative in one
sentence:

> Do not apply the change yourself.

Deleting only that sentence, holding everything else, drops handoff from 3/3
runs to 1/3. That is why the Analyst card here says it. If you want the version
that leaves the choice to the model, use `configs/team.handoff.experiment.yaml`;
if you want to know which sentence buys what, the four rungs in
`configs/team.handoff.cmd-*.yaml` isolate them one at a time.

Compliance is high, not total. Expect the Analyst to sometimes make an edit of
its own alongside the delegated one.

## How the work physically moves

All three seats share one git object store; the Coder and the Tester each get a
linked worktree, and the Analyst has the delivered workspace. So work moves in
two steps, and the whole payload is a sha:

1. the Coder `git commit`s in its worktree and sends the sha with `message_agent`;
2. the Analyst runs `git checkout <sha>` in the workspace.

No push, no fetch, no patch file. Until a commit exists, one seat's edits are
invisible to the others. **A run that ends without the Analyst's `git checkout`
delivers nothing** — the teammate worktrees are never read.

One trap, learned from a failed run and now written into the card: the sha the
Analyst is handed is on no branch of its own, so plain `git log` does not show
it and only `git log --all` reaches it. In smoke run #3 the Analyst searched for
the Coder's commit, found nothing, concluded nothing had been delivered, and
spent the rest of its seat on `sleep 20`, `sleep 45`, `sleep 110` until the
budget ran out — with the correct commit sitting in the object store the whole
time. The card now says to check the sha out directly and not to go looking.

## The three ways to run it

### Script (the everyday route)

```bash
uv run scripts/run_collab_team.py --workspace ./repo \
    --prompt "fix the failing test in tests/test_slugify.py" \
    --artifacts ./artifacts --allow-unisolated-shell
```

The script exists because the team file needs a prebuilt roster and
`uv run opencollab` has no flag for it. It fixes `prebuild_team=True`,
defaults to separate worktrees and serialized turns, and exposes
`--no-worktrees` and `--concurrent` for callers choosing different execution
conditions. It seeds `PYTEST_ADDOPTS=-p no:cacheprovider` (see below) and prints
the run status and recorded message attempts per role to stderr. A successful
delivery is recorded by scheduler events and must be read separately from
those tool-call counts.

`--budget` is the **shared pool, not a per-seat allowance**. Each seat may spend
at most `c * pool / N` with `c = 1.0` and `N = 3`, so `--budget 900000` gives
every seat a 300k ceiling. Nothing is reserved at seating. Tokens are metered against the shared pool
when roles run, and each role retains its own cumulative ceiling.
A team file that declares `budget: { tokens: N }` replaces the pool with an
independent allowance per role; the script then passes no pool unless
`--budget` is given, and the run refuses one if it is (see
[the configuration guide](../configs/README.md#team)).

### SDK

```python
result = await OpenCollab(workspace).team(
    prompt,
    config="configs/team.collab.yaml",
    prebuild_team=True,      # required — see below
    use_worktrees=True,
    serialize_turns=True,
    allow_unisolated_shell=True,  # authorize host commands for this workspace
)
```

### Evaluation

Benchmark launch commands and team budget handling belong to the companion
package. Use the
[OpenCollab-Eval README](https://github.com/RISE-X-Lab/OpenCollab-Eval#readme)
for its current team arm and configuration options. OpenCollab's team API
accepts `prebuild_team=True`, `use_worktrees=True`, `serialize_turns=True`, and
`record_delivery_tree=True` for integrations that need the declared roster and
workspace-delivery observations.

## The four things that must be true

The following checks connect each launch requirement to its observable state
and the current role instructions.

| Requirement | If it is missing | Symptom |
|---|---|---|
| **`prebuild_team=True`** | the Coder and the Tester are never seated, and no role holds `spawn_agent` | `team_status` shows only the Analyst. Its role card requires it to report the missing Coder and Tester and stop. |
| **A shell that can run `git`** | `bash` refuses when the environment provides no OS process sandbox | every role reports the same refusal, no commit can cross between seats, and the handoff cannot happen at all. Pass `--allow-unisolated-shell` (only for a workspace you trust — it lets agents execute code on the host). Inside the evaluation container this is already satisfied. |
| **No ignored files left in a teammate's worktree** | diff capture reports an error and retains the worktree when it cannot include ignored files | an otherwise completed task can report a lifecycle failure. Pytest's `.pytest_cache/` contains a `.gitignore` with `*`, so the directory ignores itself. The runner sets `PYTEST_ADDOPTS=-p no:cacheprovider`, and the Coder/Tester cards require cache cleanup. |
| **Artifacts kept, if you want to know whether it worked** | per-role transcript files and scheduler-event files are unavailable for inspection | Pass `--artifacts` to retain these records and print each role's `message_agent` attempt count. Otherwise the runner prints `message attempts=unknown (pass --artifacts to count them)`. Inspect delivery events to establish which attempts reached a teammate. Returned metrics retain the lead's step count, seat count, run identity and lifecycle observations. With `record_delivery_tree=True`, they also include workspace snapshots. |

## Recorded August 31 smoke run

Smoke run #4, `~/collab-smoke/artifacts-1788239916` on gpu3, `deepseek-v4-flash`,
a two-test slugify fixture:

```
-- status=completed seats=3 lead steps=10
   messages sent: analyst=3, coder=1, tester=2
```

Tool calls per seat, counted from the transcripts:

| seat | calls |
|---|---|
| analyst | `team_status` 1, `file_read` 2, `message_agent` 3, `bash` 2, `run_tests` 1, `submit` 4 |
| coder | `file_read` 2, **`apply_patch` 1**, `run_tests` 2, `bash` 7, `message_agent` 1, `submit` 1 |
| tester | `team_status` 1, `bash` 16, `file_read` 2, `run_tests` 4, `message_agent` 2, `submit` 2 |

The Analyst called no write tool at all. The single `apply_patch` is the
Coder's, it became commit `2c7b809`, and the delivered workspace's `HEAD` is
that commit — so 100% of the delivered change is the Coder's, and the checkout
step actually ran. This is one run; the ladder evidence above is what carries
the general claim.

## What is held by tests

`tests/workflows/test_collab_team_config.py` covers the roster
and six directed edges. The tests check role tool claims, the absence of
`spawn_agent`, loading after copying the configuration, runner call arguments,
and the distinction between message attempts and deliveries.

`scripts/run_collab_team.py` is registered in the framework-script whitelist in
`tests/packaging/test_repository_ownership.py`.

## Known rough edges

- **Partial compliance.** The Analyst sometimes edits alongside delegating. The
  command raises the delegation rate; it does not pin it to 1.
- **Waiting costs budget.** A seat with nothing to do should finish — an
  arriving message reopens a finished turn. The Analyst card says so, but an
  Analyst that decides to wait anyway can still exhaust its cap.
- **`team_status` does not report reachable roles**, only live ones, so a role
  the topology forbids is discovered by being refused.
- **The roster block generated above the card says the roles are ones you
  "may spawn or message."** Spawning is refused here. The card contradicts it
  explicitly; the generated text has not been changed.
