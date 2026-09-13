# Execution Server

The Execution Server runs OpenCollab's existing tools in an isolated sandbox
reached over HTTP. OpenCollab retains model calls, the agent loop, scheduling,
and tool selection. The service owns sandbox lifecycle, command execution,
file operations, cancellation, and filesystem checkpoints.

```text
OpenCollab agent -> built-in tools -> RemoteEnvironment -> HTTP /v1
                 -> Execution Server -> DockerBackend or OSBackend -> sandbox
```

`RemoteEnvironment` implements the same environment contract as the local and
Docker adapters. The `bash`, `file_read`, `file_write`, `apply_patch`,
`run_tests`, and `git_diff` tools need no server-specific implementation.

## Installation and Docker startup

```bash
uv sync --extra execution
uv run opencollab-execution-server \
  --backend docker --host 127.0.0.1 --port 8080
```

The Docker backend creates one persistent, network-disabled container for each
environment. Commands and file operations share its filesystem until deletion
or restore. An init process reaps orphaned children during cancellation.

## OpenCollab client

```python
import os

from opencollab import OpenCollab
from opencollab.environments import remote_environment

environment = remote_environment(
    "http://127.0.0.1:8080",
    image="python:3.11-slim",
)
client = OpenCollab(
    ".",
    model="MODEL_NAME",
    provider="openai",
    api_key=os.environ["OPENCOLLAB_API_KEY"],  # pragma: allowlist secret
    base_url="PROVIDER_BASE_URL",
    environment=environment,
)
result = await client.agent(
    "Create result.txt containing 17 * 23 and verify the file.",
    tools="coding",
    cleanup_timeout=15,
)
```

Remote cleanup includes HTTP and process-tree cleanup latency. Callers should
set `cleanup_timeout` for their deployment; 15 seconds is a practical local
starting point.

## HTTP contract

All routes use the `/v1` prefix.

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/environments` | Create a sandbox from an image or base |
| `GET` | `/environments/{id}` | Inspect a sandbox |
| `POST` | `/environments/{id}/heartbeat` | Renew its lease |
| `POST` | `/environments/{id}/abort` | Revoke active work |
| `DELETE` | `/environments/{id}` | Destroy it and its checkpoints |
| `POST` | `/environments/{id}/executions` | Start a command |
| `GET` | `/executions/{id}` | Wait for its result |
| `DELETE` | `/executions/{id}` | Cancel and prove quiescence |
| `POST` | `/environments/{id}/files/read` | Read a UTF-8 file |
| `POST` | `/environments/{id}/files/read_range` | Read a bounded range |
| `POST` | `/environments/{id}/files/write` | Atomically write a file |
| `POST` | `/environments/{id}/files/write_temp` | Create an owned temp file |
| `POST` | `/environments/{id}/files/remove` | Remove an owned temp file |
| `POST` | `/environments/{id}/checkpoints` | Capture the filesystem |
| `GET` | `/environments/{id}/checkpoints` | List owned checkpoints |
| `POST` | `/environments/{id}/checkpoints/{checkpoint}/restore` | Restore in place |
| `POST` | `/environments/{id}/checkpoints/{checkpoint}/fork` | Create a branch |
| `DELETE` | `/environments/{id}/checkpoints/{checkpoint}` | Discard storage |

Execution results preserve all seven fields: `returncode`, `stdout`, `stderr`,
`stdout_truncated`, `stderr_truncated`, `stdout_dropped_bytes`, and
`stderr_dropped_bytes`.

`X-Sandbox-Generation` fences requests across restore. Restore increments the
generation and stale requests receive HTTP 409. `Idempotency-Key` on execution
creation prevents a transport retry from running the command twice.

Cancellation returns only after the backend confirms that the process tree is
quiet. If cleanup cannot be proven, the sandbox is revoked and later operations
receive `sandbox_torn_down`.

## Checkpoints and backends

Docker checkpoints use `docker commit --pause`. Restore replaces the running
container; fork creates an independent container from the same checkpoint.

The rootful Linux OS backend uses mount, PID, and network namespaces, a verified
read-only rootfs, an OverlayFS writable layer, and one bounded cgroup v2 per
sandbox. Its checkpoints archive the writable layer with xattrs and whiteouts.
They include filesystem state outside `/workspace` but not process memory.

The OS backend is Linux-only and fail-closed. It requires
`EXECSERVER_CGROUP_ROOT` to point to an administrator-delegated empty cgroup v2
parent with the `cpu`, `memory`, and `pids` controllers enabled.

```bash
sudo mkdir -p /var/lib/execserver/{bases,os,snap}
sudo uv run python -m opencollab.adapters.execution.linux build-base python311

export EXECSERVER_OS_BASES=/var/lib/execserver/bases
export EXECSERVER_OS_ROOT=/var/lib/execserver/os
export EXECSERVER_OS_SNAPSHOTS=/var/lib/execserver/snap
export EXECSERVER_CGROUP_ROOT=/sys/fs/cgroup/execserver-delegated
sudo --preserve-env uv run opencollab-execution-server \
  --backend os --host 127.0.0.1 --port 8080
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `EXECSERVER_BACKEND` | `docker` | Selected backend |
| `EXECSERVER_LEASE_TTL` | `1800` | Idle lifetime in seconds |
| `EXECSERVER_OS_BASES` | `/var/lib/execserver/bases` | Rootfs store |
| `EXECSERVER_OS_ROOT` | `/var/lib/execserver/os` | Live sandbox store |
| `EXECSERVER_OS_SNAPSHOTS` | `/var/lib/execserver/snap` | Checkpoint store |
| `EXECSERVER_CGROUP_ROOT` | required for OS | Delegated cgroup parent |
| `EXECSERVER_CPU_QUOTA_US` | `100000` | CPU quota per 100000 μs |
| `EXECSERVER_MEMORY_MAX_BYTES` | `536870912` | Memory per sandbox |
| `EXECSERVER_PIDS_MAX` | `128` | Processes per sandbox |

## Validation

The default suite covers the public factory, HTTP serialization, cancellation,
generation fencing, idempotency, lifecycle races, and failure recovery without
requiring a running service:

```bash
uv run pytest -q
```

Real Docker tests use an explicitly started server:

```bash
OPENCOLLAB_TEST_EXECUTION_SERVER=1 uv run pytest -q \
  tests/execution/test_remote_tools_e2e.py \
  tests/execution/test_execution_identity.py
```

The privileged Linux tests in `tests/execution/test_osbackend.py`,
`test_os_cgroup.py`, and `test_rootfs_identity.py` directly exercise the OS
backend and skip when the required kernel capabilities are unavailable.

## Deployment limits

The API has no built-in authentication, authorization, TLS, audit log, or
durable registry recovery. Bind it to a trusted interface for development. A
shared or Internet-facing deployment needs an authenticated encrypted gateway,
persistent recovery, and observability.

Commands within one OS sandbox are serialized. Separate sandboxes may execute
concurrently.
