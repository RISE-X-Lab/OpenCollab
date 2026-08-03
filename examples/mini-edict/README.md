# Mini Edict

[English](README.md) | [Chinese](README.zh-CN.md)

Mini Edict implements the Three Departments and Six Ministries as an executable
governance protocol. Everything specific to the example lives in this
directory. OpenCollab provides the agent runtime.

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
complete execution record.

## Talk to Zhongshu

Start OpenCollab with the example's team file.

```bash
uv run opencollab \
  --team-config examples/mini-edict/team.yaml \
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

The same institution can run as a one-shot workflow with fixed approval gates.

```bash
OPENCOLLAB_WORKFLOWS_DIR=examples/mini-edict/workflows \
  uv run opencollab workflow run three-departments-six-ministries \
  --concurrency 6 \
  --args '{"task":"Design a six-month open-source community growth plan."}'
```

This command prints each institutional phase and returns the complete execution
record with its token use.

## Design references

The workflow adapts Edict's mandatory
[Menxia review](https://github.com/cft0808/edict/blob/main/agents/menxia/SOUL.md)
and dynamic [Shangshu routing](https://github.com/cft0808/edict/blob/main/agents/shangshu/SOUL.md).
It also uses [Agent-Team](https://github.com/EthanHuangEbor/Agent-Team) as a
reference for packaging skills and controlling revision and dispatch. The
implementation uses OpenCollab's workflow API.

## Files to edit

The team file defines the conversational roles and the workflow file defines
the gated execution path. Edit role mandates in `team.yaml` and stage order in
the workflow. Together, these files implement the protocol on OpenCollab's
shared runtime.
