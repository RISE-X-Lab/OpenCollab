# Adherence

A team file says who should do what. A benchmark score reports only the final
outcome, so it cannot say whether the agents worked together. In OpenCollab's
traces they often did not: the lead agent briefed its teammates and then did
the work itself, or spent its whole token budget before handing anything off.
The declared team collapsed into a single agent. We call this the *illusion of
collaboration*.

<p align="center">
  <img src="../assets/collaboration-traces.png" alt="Two execution timelines on a shared time axis. In the read-only run, the Adopter briefs Coder A, Coder A commits, and the Adopter adopts that commit, which passes. In the reference run, the Adopter briefs both coders but keeps working until it runs out of budget; its own code is graded and fails." width="900">
</p>
<p align="center">
  <sub>Two runs reconstructed from OpenCollab's event stream. (a) With read-only tools, the Adopter
  (the lead agent) hands the task to the coders and adopts Coder A's commit. (b) In the reference team,
  the Adopter briefs both coders but keeps working until its budget runs out, so its own code is graded.</sub>
</p>

## What OpenCollab records

<p align="center">
  <img src="../assets/oc-architecture.png" alt="OpenCollab overview. A specification of task, model, role prompts, toolsets, topology, and token and context policies drives one of three controllers: Single, Team, or Workflow. All three run on a shared session runtime with isolated worktrees and one session loop. The event stream feeds an audit that yields Adherence, results, and costs." width="900">
</p>

- **One substrate for every organization.** Single agents, Teams, and
  Workflows run on the same session runtime, with the same model client,
  context handling, and tool implementations. Changing the organization does
  not change the rest of the system. Model access, context handling, tool
  execution, orchestration, and environments have separate extension points,
  so an experiment can change one component at a time.
- **Rules enforced at call time.** A message along an undeclared edge is
  refused, and the refusal is recorded. Each agent's token budget is checked
  before every model call, and an agent that reaches its ceiling stops with an
  explicit reason, so a run cut short is never confused with one that finished.
- **An event stream instead of prose logs.** `--trace` writes one JSONL record
  per step, stamped with the run, agent, role, event type, and token usage.
  Scripts can rebuild who did what without parsing chat transcripts.
- **Isolated workspaces.** Teammates can work in their own git worktrees and
  hand back their diffs.

## Measuring Adherence

A run counts as adherent only if its trace shows that every declared role took
part and that delegation, role boundaries, budget sharing, information flow,
and context policy all held. **Adherence** is the share of adherent runs. On
36 SWE-bench Pro tasks, one change to the same team moves it a long way:

| Change to the reference team | Adherence |
| --- | ---: |
| None (Qwen3.8-Flash, open prompt, all tools, star topology) | 47.2% |
| The prompt makes the handoff mandatory | 91.7% |
| The lead agent gets read-only tools | 94.4% |
| Every role runs GPT-5.6-Luna | 97.2% |

## Reading a Team-versus-Single gap

Adherence tells you how to read a Team-versus-Single gap. Dividing the Pass@1
difference by Adherence gives the complier average causal effect (CACE), and
because Adherence is recorded for every run, the assumption behind that
estimate can be examined with data rather than assumed. The paper defines the
six audit axes and reports the full ablation over model, tools, budget,
context, and topology.
