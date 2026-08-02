# Three Departments and Six Ministries

This example turns the Three Departments and Six Ministries into a compact,
executable governance protocol. It adds one team configuration and one workflow
file to OpenCollab. The framework continues to provide agent sessions, model
access, concurrency, budgets, tracing, and persistence.

```text
task
  -> Zhongshu Secretariat drafts
  -> Menxia Chancellery approves or vetoes
       -> veto: Zhongshu revises, at most two times
       -> approve: Shangshu selects relevant ministries
            -> selected ministries execute in parallel
  -> Shangshu produces an evidence-bearing memorial
  -> Menxia performs the final audit
       -> approved result or blocked result
```

The one-shot workflow enforces this separation in Python control flow. Execution
cannot start before Menxia approval. A veto returns concrete findings to
Zhongshu. Shangshu assigns distinct work and acceptance criteria only to
ministries relevant to the task. The final audit checks the memorial against the
approved proposal, assignments, ministry reports, and their evidence.

## Talk to Zhongshu

Start OpenCollab with the example's team file.

```bash
uv run opencollab \
  --team-config examples/three-departments-six-ministries/team.yaml \
  --workspace .
```

The persistent root session is Zhongshu. It answers ordinary questions itself.
Ask for a concrete deliverable, or say `convene the court`, and Zhongshu drafts
a proposal, obtains Menxia approval, sends the approved work to Shangshu, and
returns the audited memorial. Shangshu selects and spawns the relevant
ministries through OpenCollab's normal team scheduler. The team topology
prevents Zhongshu from dispatching directly to a ministry and gives Menxia no
dispatch path. Role instructions guide the review order in this conversational
mode.

## Run one decree

The same institution is available as a hard-gated one-shot workflow.

```bash
OPENCOLLAB_WORKFLOWS_DIR=examples/three-departments-six-ministries/workflows \
  uv run opencollab workflow run three-departments-six-ministries \
  --concurrency 6 \
  --args '{"task":"Design a six-month open-source community growth plan."}'
```

This command prints each institutional phase and returns the proposal, review
history, dispatch decision, selected ministry reports, final memorial, audit,
and total token use.

## Design references

The workflow adapts Edict's mandatory
[Menxia review](https://github.com/cft0808/edict/blob/main/agents/menxia/SOUL.md)
and dynamic [Shangshu routing](https://github.com/cft0808/edict/blob/main/agents/shangshu/SOUL.md).
It also follows the skill-first packaging, bounded revision loop, selective
dispatch, and evidence-oriented output described by
[Agent-Team](https://github.com/EthanHuangEbor/Agent-Team). The implementation
uses OpenCollab's workflow API instead of copying either project's runtime or
operations stack.

## Why the example is small

The example contains one team file for conversation and one workflow file for
hard-gated execution. Changing the organization means editing role mandates or
stage order. Generic agent infrastructure remains in OpenCollab.

This is the point of the demonstration. A collaboration idea that often becomes
a standalone multi-agent repository can be expressed as a small, inspectable
protocol on top of a shared framework.
