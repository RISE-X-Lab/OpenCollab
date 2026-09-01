# The collaborating team: what it is, and how to run it

`configs/team.collab.yaml` is a three-role team that actually hands work over.
This document is the handoff for it: what the file is, why it is built the way
it is, the three ways to run it, and the four things that have to be true or the
run degrades into something that reads like a team and is not one.

Everything below was checked against runs, not only against the code. Where a
claim comes from a run, the run is named.

## What it is

One self-contained YAML file. Three roles — Analyst (agent 0), Coder, Tester —
with their prompts inline, so the file can be copied anywhere and named on a
command line with no sibling files to fix up.

| | Analyst | Coder | Tester |
|---|---|---|---|
| seat | agent 0; the request arrives here and its answer is read back | teammate | teammate |
| edit tools | `apply_patch`, `file_write` | `apply_patch`, `file_write` | none |
| read/run | `file_read`, `grep`, `bash`, `run_tests` | same | same, plus `git_diff` |
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
scripts/run_collab_team.py --workspace ./repo \
    --prompt "fix the failing test in tests/test_slugify.py" \
    --artifacts ./artifacts --allow-unisolated-shell
```

The script exists because the team file needs a prebuilt roster and
`uv run opencollab` has no flag for it. It fixes `prebuild_team=True`,
`use_worktrees=True` and `serialize_turns=True` at the call site rather than
exposing them, seeds `PYTEST_ADDOPTS=-p no:cacheprovider` (see below), and
prints one line to stderr saying whether the handoff happened.

`--budget` is the **shared pool, not a per-seat allowance**. Each seat may spend
at most `c * pool / N` with `c = 1.0` and `N = 3`, so `--budget 900000` gives
every seat a 300k ceiling. Nothing is reserved at seating, so a role the model
never uses holds no tokens and its allowance stays available to the others.

### SDK

```python
result = await OpenCollab(workspace).team(
    prompt,
    config="configs/team.collab.yaml",
    prebuild_team=True,      # required — see below
    use_worktrees=True,
    serialize_turns=True,
)
```

### Evaluation

```bash
gen_prediction_batch --arm team --team-config configs/team.collab.yaml ...
```

That path already passes `prebuild_team=True`, `use_worktrees=True`,
`serialize_turns=True` and `record_delivery_tree=True`
(`OpenCollab-Eval/src/opencollab_eval/engine/evaluator_sessions.py:373`). Note
that the batch driver's `--budget-per-seat` is multiplied by the number of roles
the team file declares (`gen_prediction_batch.pool_for`), so a three-role file
is started with three times the per-seat figure — the opposite convention from
the runner script's `--budget`.

## The four things that must be true

Each of these fails quietly rather than loudly. The symptom column is what you
would actually see.

| Requirement | If it is missing | Symptom |
|---|---|---|
| **`prebuild_team=True`** | the Coder and the Tester are never seated; no role holds `spawn_agent`, and on a prebuilt team spawning is refused anyway | the Analyst does the whole task alone and the run looks successful. The card defends against this: it calls `team_status` first and stops with an explicit report if the roster is not there. |
| **A shell that can run `git`** | `bash` refuses when the environment provides no OS process sandbox | every role reports the same refusal, no commit can cross between seats, and the handoff cannot happen at all. Pass `--allow-unisolated-shell` (only for a workspace you trust — it lets agents execute code on the host). Inside the evaluation container this is already satisfied. |
| **No ignored files left in a teammate's worktree** | a worktree holding an ignored file cannot have its changes read, and the failure is raised during cleanup | the team delivers a correct answer and the run still reports `failed`, after the fact. `.pytest_cache/` alone causes it: pytest writes a `.gitignore` containing `*` inside it, so the directory ignores itself. Both the runner (`PYTEST_ADDOPTS=-p no:cacheprovider`) and the Coder/Tester cards defend against it. |
| **Artifacts kept, if you want to know whether it worked** | `metrics` carries only agent 0's step count and the number of seats | "did anyone delegate" is not in the metrics, and a solo run is externally indistinguishable from a delegated one. Pass `--artifacts`; the runner then counts `message_agent` calls per role from the transcripts and prints them. Without it, it prints `handoffs=unknown` rather than an unsupported `0`. |

## What a working run looks like

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

`tests/test_collab_team_config.py`, 32 tests, all passing. They pin: the roster
and the six edges; that no role holds `spawn_agent`; that no card names a tool
its role does not hold (strict for the role's own capability paragraph, loose
against the union of the team's tools, because a card may legitimately describe
a teammate's bundle); that the file stays self-contained when copied elsewhere;
and the runner's call-site arguments, through a stub client.

`scripts/run_collab_team.py` is registered in the framework-script whitelist in
`tests/test_repository_ownership.py`.

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
