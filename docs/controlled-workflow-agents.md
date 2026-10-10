# Controlled workflow agent calls

`WorkflowContext.agent_run` runs a fresh session and returns its execution
result, including its stop reason, tokens, steps, session identity and cleanup
state. Existing `ctx.agent` calls keep their text or structured-output result.

```python
from opencollab import OpenCollab, RunControl, workflow
from opencollab.tools import builtin_tools

@workflow(name="incremental-edit")
async def incremental_edit(ctx, inputs):
    tools = builtin_tools("bash", "file_read", "apply_patch", "git_diff", headless=False)
    first = await ctx.agent_run(
        inputs["first_task"], label="first", budget=100_000,
        tools=tools,
        max_steps=40, system_prompt="Implement the assigned change and verify it.",
        run_control=RunControl(initial_soft_budget_tokens=80_000),
    )
    if not first.workspace_ready:
        return {"status": "cleanup_failed", "first": first.status}
    second = await ctx.agent_run(inputs["second_task"], label="second", budget=100_000, tools=tools)
    return {"first": first.status, "second": second.status}

result = await OpenCollab("/path/to/application").workflow(
    incremental_edit, {"first_task": "...", "second_task": "..."},
    agent_profile="single2", budget=200_000, concurrency=1, limit_mode="explicit",
)
```

Each call has a new conversation. Calls using `isolation=False` continue over
the same application files. Pass explicit tools to each role as with
`ctx.agent`. A per-call system prompt replaces the default profile/role prompt
exactly; the selected profile still supplies tool configuration, shaping and
safety. Omitting the prompt retains the existing profile composition.

The workflow reserves a hard budget before the session starts. The actual
grant can be lower than the requested budget because other calls hold shares
of the pool. `RunControl` receives this actual grant and can expand the soft
allowance within it. Unused reservations become available after call cleanup.
Budget refusal and waiting for a concurrency slot remain distinct operations.

`limit_mode="environment"` is the default and keeps the existing
`OPENCOLLAB_UNBOUNDED_LIMITS` behavior. `limit_mode="explicit"` uses the supplied
workflow and per-call limits, including inside candidate child workflows.
Different modes can run concurrently without changing process environment.

`OpenCollab.workflow(llm=client)` borrows an existing model client. Every
session and its summaries use that client. The caller closes it after the
workflow finishes. Clients constructed by OC retain OC-owned cleanup.

A timed-out call may have provider or tool work still settling. `agent_run`
waits up to `cleanup_timeout` for the call's owned tasks. With incomplete
cleanup it returns a failed result and prevents further work in the shared
context. The workflow still owns the pending tasks and their budget until
they settle. `cleanup_complete` distinguishes a stable result from a usage
snapshot. External task cancellation propagates to the caller.

Execution completion is separate from application correctness. Workflows
should return their executable verification result alongside these session
records. `ctx.run_id` identifies the current workflow run; child contexts
retain the parent run identity while sessions have separate identities.
