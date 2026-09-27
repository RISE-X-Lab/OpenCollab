# Base and Single2

Base is the default standalone-agent entry. In OpenCollab 0.8 it maps to
Single2, the profile extracted from the evaluated SWE-Mix-80 direct-agent
configuration. Named profiles share the same standalone API.

```python
from opencollab import OpenCollab

client = OpenCollab(workspace, model=model, provider=provider, config=config,
                    environment=environment)
# Follow the current Base mapping
result = await client.agent(task, artifacts=artifact_directory)
result = await client.agent(task, profile="base")

# Select a concrete implementation
result = await client.agent(task, profile="single2")

# Existing Single2 convenience entry
result = await client.agent2(task)
```

The omitted profile and the compatibility spellings `default` and `single`
resolve through Base. The old standalone Single implementation has been removed.
`agent2` selects Single2 explicitly and defaults its run name to `single2`.
The public resolver exposes the concrete name for configuration and run records.

```python
from opencollab.profiles import BASE_PROFILE, resolve_profile_name

assert BASE_PROFILE == "single2"
assert resolve_profile_name("base") == "single2"
```

A new implementation can be added to the named profile factories in
`opencollab/bootstrap/agent_profiles.py`. Each factory supplies its prompt,
tool resolver, shaper, safety wrapper, and default limits. `BASE_PROFILE` in
that composition module selects the default factory and is re-exported by
`opencollab.profiles`. Explicit implementation
names continue to select their own factory after the Base mapping changes.
Factories create fresh profile instances, and tool resolution creates fresh
built-in tools for each run. Completed runs record the concrete name in
`metrics["agent_profile"]`.

Single2 uses the evaluated static repair prompt, 200 steps by default,
and the ordered tool set `bash`, `file_read`, `file_write`, `apply_patch`,
`git_diff`, and `grep`. Its Bash tool requires process isolation and retains up
to 10,000 characters per stream. Oversized tool results keep both their head
and tail within the existing 16,000-character context cap. Container-root
recursive searches are rejected while workspace-scoped searches remain
available.

Bash execution uses an environment that advertises OS process isolation,
such as a Docker environment. A local environment continues to support the
filesystem tools. For deliberate local shell execution, pass explicit tools
built with `builtin_tools(..., headless=False, limits=profile_tool_limits("base"))`.
Caller-supplied tool instances retain their configuration.

The runtime supplies the remaining behavior used by the profile.
It keeps low-pressure history intact, accounts for provider state in context
pressure, uses the ASCII/CJK-aware request estimator, identifies discarded
shell streams correctly, reports omitted Grep matches, and directs tests
through each repository's native Bash commands.

Model, provider, sampling, token, and timeout settings remain caller-owned.
When `OPENCOLLAB_UNBOUNDED_LIMITS=true`, an explicit `budget` or `max_steps`
passed to Base or standalone Single2 remains effective. An omitted limit uses the unbounded setting. This lets an evaluation adapter pass an audited finite
allowance such as `1_000_000_000_000` without silently replacing it with the
profile default.

Workflows select the same profile for every role through the public facade.

```python
from opencollab.tools import builtin_tools

async def flow(ctx, inputs):
    tools = builtin_tools("bash", "file_read", "grep")
    return await ctx.agent(inputs["task"], tools=tools, label="coder")

result = await client.workflow(flow, {"task": task}, agent_profile="base")
```

Workflows that omit `agent_profile` retain their role-defined configuration.
An explicit `base` selects the current Base mapping, and `single2` selects
Single2 directly. With either selection today, each workflow session receives Single2's base prompt, shaper, and safety policy.
Workflow role duties and the actual tool permissions take precedence over the
base repair instructions. Structured roles finish through their capture tool,
including the single-tool corrective submission session. A judge created with
`tools=[]` receives only the workflow's injected structured capture tool.

Tools constructed with `builtin_tools` inside the workflow inherit Single2's
Bash output limit through a task-local configuration scope. Explicit `limits`
override those defaults. Explicit tool objects retain their configuration and
identity, so verification wrappers keep their recorded evidence. Concurrent
workflows have independent defaults, which expire when their workflow ends.
`profile_tool_limits("single2")` returns a copy of the tool defaults for
inspection. Workflow budget, step, and unbounded-limit behavior follows the
existing `workflow` API, including its 100-step default.

A team file selects the profile per seat, so a roster can be built out of the
same agent the single-agent arm runs.

```yaml
roles:
  adopter:
    profile: single2
    prompt_file: s2dual/adopter.s2dual-judge.md
    tools: [bash, file_read, file_write, apply_patch, git_diff, grep,
            message_agent, team_status, submit]
```

The seat receives Single2's base prompt, shaper, safety policy and tool output
caps; its `prompt`/`prompt_file` card is appended to the base prompt rather than
replacing it, and the team section follows the card. A seat still carries the
tools its team file declares, in the order the file declares them, so a roster
adds the coordination tools a team needs on top of Single2's six. A team file
that states its own `tool_limits` keeps them. `declared_role_profiles` reports
which profile each declared seat runs under, alongside the existing card
digests, so a recorded run can name both halves of its condition.

The source evaluation harness appends its bounded repository map, prepares the
anonymous solver workspace, isolates hidden tests, extracts the candidate, and
runs official grading. Those operations remain evaluation responsibilities and
are intentionally outside this profile.
