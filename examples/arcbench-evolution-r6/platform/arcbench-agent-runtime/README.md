# arcbench-agent-runtime

`arcbench-agent-runtime` is a small Python package for agents running inside ARC-Bench workspaces.

It provides:

- runner event emission to `runner-events.jsonl`
- traceability database creation and CRUD helpers
- git init / commit / reset helpers with deterministic identity handling

The package is designed to stay compatible with the current ARC-Bench website backend:

- `.arc/runner-events.jsonl`
- `.arc/traceability/*.json`

## Quick start

```python
from arcbench_agent_runtime import AgentRuntime

runtime = AgentRuntime.from_env()

runtime.events.mark_design_done("REQ-1", "Design completed")
runtime.traceability.upsert_requirement(
    req_id="REQ-1",
    name="Login",
    description="User can log in",
)
runtime.git.ensure_repo()
runtime.git.commit("REQ-1 (design): Login")
```
