# OC Single2

OC Single2 is an opt-in standalone-agent profile extracted from the evaluated
SWE-Mix-80 direct-agent configuration. It runs on the OpenCollab 0.7 runtime
alongside the existing agent, team, and workflow APIs.

```python
from opencollab import OpenCollab

client = OpenCollab(workspace, model=model, provider=provider, config=config,
                    environment=environment)
result = await client.agent2(task, artifacts=artifact_directory)

# Equivalent explicit selection
result = await client.agent(task, profile="single2",
                            artifacts=another_artifact_directory)

# The existing standalone agent remains the default
result = await client.agent(task)
```

`agent2` accepts the existing `agent` options and defaults its name to
`single2`. It uses the evaluated static repair prompt, 200 steps by default,
and the ordered tool set `bash`, `file_read`, `file_write`, `apply_patch`,
`git_diff`, and `grep`. Its Bash tool requires process isolation and retains up
to 10,000 characters per stream. Oversized tool results keep both their head
and tail within the existing 16,000-character context cap. Container-root
recursive searches are rejected while workspace-scoped searches remain
available.

OpenCollab 0.7 already supplies the remaining behavior used by the profile.
It keeps low-pressure history intact, accounts for provider state in context
pressure, uses the ASCII/CJK-aware request estimator, identifies discarded
shell streams correctly, reports omitted Grep matches, and directs tests
through each repository's native Bash commands.

Model, provider, sampling, token, and timeout settings remain caller-owned.
When `OPENCOLLAB_UNBOUNDED_LIMITS=true`, an explicit `budget` or `max_steps`
passed to Single2 remains effective. An omitted limit keeps OpenCollab 0.7's
unbounded behavior. This lets an evaluation adapter pass an audited finite
allowance such as `1_000_000_000_000` without silently replacing it with the
profile default.

The source evaluation harness appends its bounded repository map, prepares the
anonymous solver workspace, isolates hidden tests, extracts the candidate, and
runs official grading. Those operations remain evaluation responsibilities and
are intentionally outside this profile.

