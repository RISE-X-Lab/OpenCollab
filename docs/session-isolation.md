# Session isolation

[Chinese](session-isolation.zh-CN.md)

Team and Workflow calls can select an execution environment strategy. Session
messages remain private to each agent. The strategy controls the workspace and
tool execution environment.

`shared` reuses the parent environment and directory. `worktree` creates an
independent checkout inside the parent execution environment. Worktrees share
the operating system and Git object storage. A container-backed parent keeps
its children inside that container. Existing non-Git copy behavior is preserved.

## Choosing a strategy

```python
from opencollab import OpenCollab

client = OpenCollab(".")

async def solve(ctx, inputs):
    answer = await ctx.agent(inputs["task"], tools="coding")
    summary = await ctx.agent("Summarize the task", tools=[], isolation="shared")
    return {"answer": answer, "summary": summary}

result = await client.workflow(solve, {"task": "Inspect the code"}, isolation="worktree")
```

Workflow agents inherit the run strategy when `isolation` is omitted.
`isolation=True` selects `worktree`; `False` selects `shared`. Omitting the run
strategy preserves the historical shared Workflow default, including existing
worktree requirements for a clean source checkout. `ctx.agent_run` and candidate
child workflows use the same selection and cleanup path.

A Team file can select a default and role overrides.

```yaml
entry: manager
isolation: worktree
roles:
  manager:
    prompt: Coordinate the task.
    isolation: shared
    tools: [spawn_agent]
  coder:
    prompt: Implement the requested change.
    tools: [file_read, file_write]
topology:
  manager: [coder]
```

`client.team(..., isolation="shared")` overrides the Team-file default. Role
settings take precedence over the run default. With every new setting omitted,
`use_worktrees` keeps its existing behavior and the entry agent retains its
original workspace. The CLI reads these same Team-file settings.

An explicitly isolated entry agent keeps its full patch in the run artifacts
before the workspace is reclaimed. The SDK returns its path in
`result.metrics["entry_patch"]`. Trace-free runs also save this patch; without
an artifact directory, OC creates a recovery directory. Applying the patch to
the caller's source remains an explicit operation.

The Workflow CLI accepts `--isolation shared` or `--isolation worktree`.

## Session snapshots

`opencollab.bootstrap.snapshot_session(session)` creates independent session
state and a forked environment under the [snapshot collaborator requirements](migrations/0.5.0.md).
The source and its successful snapshots share the model transport. When OC
created that transport, each session holds an ownership reference and
`await session.aclose()` releases its reference. The final owner closes the
transport once. A snapshot can continue model requests after its source or
sibling closes. A model injected through `build_session(llm=...)` remains owned
by its caller, who closes it after all sessions finish.

The snapshot acquires its model reference after its state and runtime
collaborators have copied successfully. Failed construction cleans up its
forked environment. Shared model ownership retains a reference count, allowing
a closed source session to be reclaimed while its snapshots continue. Creating
a new snapshot from a session that has released its model ownership raises
`SnapshotSessionError`. Each session's environment retains its own cleanup
operation.

## Custom environment providers

Pass `environment_providers={"custom": factory}` to `OpenCollab`, then select
`isolation="custom"`. Each factory receives `workspace`, `base_environment`,
`owned_save_dir`, `enable_adoption`, and `owned_output_paths` as keyword arguments.
It creates a fresh pool for that run and strategy, implementing the public
`opencollab.environments.EnvironmentPool` protocol. The existing operations are
`acquire(role, source=...)`, `release_env(environment)`, `release()`, and
`adopt(patch)`.

The source passed to `acquire` is the actual parent or review workspace. An
environment returned by the provider is ready for tool execution. The provider
owns allocations it creates; the supplied source remains borrowed. Cleanup
waits for pending acquisitions and preserves recovery material when it fails.
OC retries retained cleanup through the same pool. Tools continue to use their
existing permission and workspace checks.

Unknown strategies report an error instead of selecting a different execution
environment. Model endpoints and request concurrency are configured separately
from workspace isolation.

## Docker strategies

`docker-group` allocates one container for each run using that strategy. Every
session receives a private worktree in that container, including the Team entry
agent. `docker-session` allocates a container and private workspace for each
session. Python orchestration stays in the caller process while tools execute
inside the selected container. The default image is `python:3.11` and requires
a working Docker daemon. Custom images need Bash, Git, and an entrypoint that
accepts the container's `sleep infinity` command.

```python
from opencollab import OpenCollab
from opencollab.environments import DockerMount, DockerOptions, docker_isolation_provider

options = DockerOptions(
    image="python:3.11",
    cpus=2,
    memory="2g",
    pids_limit=256,
    mounts=(DockerMount("/path/to/read-only-assets", "/assets"),),
)
client = OpenCollab(".", environment_providers={
    "docker-session": docker_isolation_provider(options, scope="session"),
})
result = await client.workflow(solve, {"task": "Inspect the code"}, isolation="docker-session")
```

Container networking defaults to `none`. The `network` option accepts `bridge`
or a named Docker network when the task needs network access. Additional mounts
are read-only and reject host sockets. Workspace data is copied into private
container storage. Existing worktree and submodule capture preserves physical
file contents while portable Git metadata lets the container operate
independently from the source checkout. The source may be local or an existing
Docker environment and remains owned by its caller.

Full patches are captured before owned containers are removed. A failed or
truncated export retains its workspace and container for recovery and cleanup
retry. Recovery locations include the full container ID. The runtime's existing
process cancellation and environment revocation behavior continues to apply.
The Docker adapter tests can execute these paths against a local image through
`OPENCOLLAB_DOCKER_TEST_IMAGE`.

## VM strategies

`vm-group` leases one existing Linux VM for a Team or Workflow and gives each
Session a private worktree inside that guest. `vm-session` leases one VM for
each Session. `shared` and `worktree` remain relative to the parent environment,
so a member running in a VM can keep its authored Workflow agents on that guest.
Prepare guest software and SSH access using the [VM guide](virtual-machines.md).

```python
from opencollab import OpenCollab
from opencollab.environments import VMConnection, VMResource, vm_isolation_provider

resources = [VMResource(
    resource_id="worker-01",
    connection=VMConnection(host="vm.example.net", user="worker"),
    workspace_root="/srv/opencollab",
    state_directory="/var/tmp/opencollab-operations",
    reset_command="/usr/local/sbin/reset-opencollab-guest",
)]
client = OpenCollab(".", environment_providers={
    "vm-group": vm_isolation_provider(resources, lease_database="/var/tmp/oc-leases.sqlite3"),
    "vm-session": vm_isolation_provider(resources, lease_database="/var/tmp/oc-leases.sqlite3", scope="session"),
})
result = await client.workflow(solve, {"task": "Inspect the code"}, isolation="vm-group", cleanup_timeout=60)
```

The stable `resource_id` identifies the actual machine across connection aliases.
Controller processes sharing a pool use the same absolute `lease_database` path.
SQLite transactions and resource uniqueness keep each leased VM exclusive.
`wait_timeout=0` reports unavailable capacity immediately, a positive number
waits for that many seconds, and `None` waits until capacity or cancellation.
A child requesting `vm-session` needs an available VM in addition to its parent.
An external allocation service can use the existing custom-provider entry point
when several controller machines share resources.

The workspace root must already exist on the guest. Each resource requires an
explicit `reset_command` that restores every guest change the workload may make.
Deleting a working directory restores files inside it. Dependency installations,
system configuration and other guest changes require corresponding restoration,
such as an administrator's reset operation. OC exports candidate files and logs,
stops owned operations, cleans its workspaces, and runs reset while retaining the
lease. Successful reset returns the VM to the pool. An export, cleanup, transport
or reset failure preserves the lease and recovery material for retry.

For a serializable SDK configuration, put the same values in `config["vm"]`.
The `vm` object contains `resources`, `lease_database`, optional `wait_timeout`,
and `cleanup_timeout` with a default of 60 seconds.
Each resource object contains the `VMResource` fields, with a `connection` object
containing `VMConnection` fields. `OpenCollab` loads both VM strategies from this
profile. Explicit `environment_providers` entries override matching configured
names.

The profile's cleanup grace applies to SDK Team and Workflow calls, the CLI,
and member-authored child Workflows. An explicit `cleanup_timeout` on an SDK
call or child `launch` takes precedence. The Workflow CLI accepts
`--cleanup-timeout`. Without a VM profile, existing runtime cleanup defaults
continue to apply. Custom VM providers can set cleanup grace through those
same call options, as in the provider example above.

```python
import json
from dataclasses import asdict
from pathlib import Path

profile = {"resources": [asdict(resource) for resource in resources],
           "lease_database": "/var/tmp/oc-leases.sqlite3"}
Path("vm-profile.json").write_text(json.dumps(profile))
profile = json.loads(Path("vm-profile.json").read_text())
client = OpenCollab(".", config={"vm": profile})
result = await client.team("Inspect the source", config="team.yaml", isolation="vm-group")
```

The CLI reads the JSON profile from `OPENCOLLAB_VM`, including the existing
`configs/.env` source. Team YAML uses the same `isolation` defaults and role
overrides shown above. Workflow runs use the existing `--isolation` option.

```bash
OPENCOLLAB_VM="$(cat vm-profile.json)" opencollab workflow run solve --isolation vm-group
```

Model routes, credentials and shared request slots stay with the controller.
VM selection controls tool execution and workspace ownership. Team communication
continues to follow its declared topology.

Leases survive controller process exit. An administrator can recover a retained
resource after confirming that its owned remote processes have stopped and its
guest reset succeeded, then removing that resource's exact lease record. Active
pool objects support cleanup retries. Recreating a pool leaves retained leases
in place until that recovery is complete.

`manage_workflow` accepts an optional `isolation` on `launch` to select the child
Workflow's default. Omission inherits its member's strategy. A member can launch
with `{"action":"launch","source":"generated.py","isolation":"worktree"}`
to run its child agents in independent worktrees inside its own VM. Individual
child calls can select `isolation="vm-session"` to request another guest. The
[executable VM example](../examples/vm-authored-workflow/README.md) starts with a
member writing source and finishes by collecting the native child result.

The `budget` on `launch` is the child's total token allowance, shared across its
sessions. The host checks the remaining allowance before each model request,
including calls made with `over_budget_ok=True` inside the child.
