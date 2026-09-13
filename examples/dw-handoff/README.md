# The handoff experiment, sequenced by a script

This example is the code-sequenced twin of `configs/team.handoff.experiment.yaml`.
Both run an analyst, a coder, and a tester over one repository. **Exactly one
thing differs between them, and it is the thing under study.**

| | this example | `team.handoff.experiment.yaml` |
|---|---|---|
| roles | analyst / coder / tester | same |
| tools per role | same bundles, minus the coordination tools | analyst and coder can edit; tester has `git_diff`, not `apply_patch` |
| workspace | one git worktree per agent | one git worktree per agent |
| handoff payload | a commit sha | a commit sha |
| **who sequences the work** | **this script** | **the model** |

The team config states its own purpose as measuring "whether models hand work to
each other when they are able to and nothing tells them to", and says that
"scripting the exchange would mean measuring the script". This example scripts
it deliberately, so that the two arms can be compared without a second
difference confounding the first.

## Why every agent gets a worktree, including the ones that mostly read

Linked worktrees share `.git/objects`, so a sha committed in one is reachable
from every other the moment it exists. Isolation does not restrict what an agent
can receive — it is what makes receiving **observable**.

A tester that shares the coder's directory holds the coder's edits whether or
not it was ever told about them, so "the tester worked from the coder's output"
is not a question the run can answer. With separate trees, the tester holds that
work only if it checked out the sha.

## What the script cannot make happen

The script hands the coder's sha to the tester. It cannot make the tester use
it. So each round records both:

```
"offered_commit": "a1b2c3…",     # what the script handed over
"adopted_commit": "a1b2c3…",     # what the tester says it checked out
"handoff_taken": true            # whether those are the same
```

This is the point of the mirror. In the team arm, a handoff requires the model
to choose to send a message; here it requires the model to choose to check out
what it was given. **Both arms are scored by whether the declared edge was
actually walked, by the same rule**, which is what makes the two numbers
comparable.

The tester's claim is not taken on trust. Its worktree separately records the
revision its diff was measured against, so `adopted_commit` and the tree either
agree or they do not.

## Running it

```bash
opencollab workflow run dw-handoff \
  --workspace /path/to/a/git/repo \
  --args '{"goal": "the task the agents are given"}'
```

The workspace must be a git repository with at least one commit: worktrees need
something to branch from. Each agent's tree is created detached at the
workspace's base commit and removed when the run ends.

**A worktree is a fresh checkout, so uncommitted files in the workspace are not
in it.** If your task depends on files that were written into the workspace
without being committed — a harness that applies test files and leaves them
dirty, for example — those files will be missing from every agent's tree. Commit
them first, or copy them in at creation.
