"""HTTP service for remote OpenCollab execution environments."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from opencollab.adapters._env_docker import DockerEnvironment


@contextlib.asynccontextmanager
async def _lifespan(application: FastAPI):
    application.state.reaper = asyncio.create_task(_reaper())
    try:
        yield
    finally:
        application.state.reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await application.state.reaper


app = FastAPI(
    title="OpenCollab Execution Server",
    version="0.2.0",
    lifespan=_lifespan,
)

# How long a sandbox may sit idle before the lease reaper reclaims it. A client
# that dies must not leak a container forever. Refreshed by any call on the env
# and by an explicit heartbeat.
_LEASE_TTL_SECONDS = float(os.environ.get("EXECSERVER_LEASE_TTL", "1800"))
_REAPER_INTERVAL_SECONDS = 30.0


# ==========================================================================
# Backend seam
# --------------------------------------------------------------------------
# The seam sits at container lifecycle + snapshot, NOT at exec/files: those
# already route through OpenCollab's EnvironmentPort and are backend-agnostic.
# What differs between Docker, an OS-level sandbox, and a microVM is exactly
# "make a sandbox", "snapshot it", "materialize a sandbox from a snapshot",
# and "free a snapshot". OSBackend implements this same Protocol.
# ==========================================================================

from .backend import Sandbox, SandboxBackend  # noqa: E402


async def _run(*args: str, timeout: float = 300.0) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:2])} failed: {err!r}")


class DockerBackend:
    """The Docker implementation of the backend seam.

    A snapshot is a committed container image. This works because OpenCollab
    starts the container with NO `-v` bind mount unless setup(mount_dir=...) is
    given, and we call setup() bare — so /workspace, /tmp, /opt, /etc all live
    in one writable layer, and `docker commit` captures the whole environment.
    """

    def _image_tag(self, ref: str) -> str:
        return f"execserver-ckpt:{ref}"

    async def create(self, image: str) -> Sandbox:
        env = DockerEnvironment(image=image, init_process=True)
        container_id = await env.setup()   # returns the container id
        return Sandbox(env, container_id)

    async def destroy(self, sb: Sandbox) -> None:
        await sb.env.cleanup()

    async def snapshot(self, sb: Sandbox, ref: str) -> None:
        # --pause so the layer is not captured mid-write.
        await _run("docker", "commit", "--pause", sb.handle, self._image_tag(ref))

    async def materialize(self, ref: str) -> Sandbox:
        env = DockerEnvironment(image=self._image_tag(ref), init_process=True)
        container_id = await env.setup()
        return Sandbox(env, container_id)

    async def discard(self, ref: str) -> None:
        # -f: the image may still be referenced by a stopped --rm container that
        # has already exited; force removal frees the disk.
        with contextlib.suppress(RuntimeError):
            await _run("docker", "rmi", "-f", self._image_tag(ref))

    def alive(self, sb: Sandbox) -> bool:
        # DockerEnvironment drops its container id and/or revokes when a hard
        # cancel had to tear the container down to guarantee quiescence.
        return getattr(sb.env, "_container_id", "x") is not None and not sb.env.revoked


def _select_backend() -> SandboxBackend:
    """Pick the backend by EXECSERVER_BACKEND (default: docker).

    The server, its routes, and RemoteEnvironment are identical whichever wins —
    that is the whole point of the SandboxBackend seam. `os` selects the
    self-built Linux namespace/overlay backend (Linux + root only).
    """
    choice = os.environ.get("EXECSERVER_BACKEND", "docker").lower()
    if choice == "os":
        from .linux import OSBackend  # imported only when selected (Linux-only)
        return OSBackend()
    return DockerBackend()


BACKEND: SandboxBackend = _select_backend()


# ==========================================================================
# state
# ==========================================================================

class _EnvRecord:
    def __init__(self, sandbox: Sandbox) -> None:
        self.sb = sandbox
        self.alive = True
        self.restoring = False
        # Materialized sandboxes that could not be cleaned are retained here;
        # DELETE/reaper retries them instead of losing the only cleanup handle.
        self.pending_cleanup: list[Sandbox] = []
        self.primary_cleanup_done = False
        # Bumped on every restore. A client that holds a handle from before a
        # restore is working against a sandbox that no longer exists; its late
        # request must be rejected (409) rather than silently hit the new
        # container. RemoteEnvironment carries its known generation on each call.
        self.generation = 0
        self.last_used = time.monotonic()
        # checkpoint / restore / fork must not run while a command is writing.
        # A snapshot taken mid-write is not a state anything was ever in.
        self.snapshot_lock = asyncio.Lock()
        self.executions: set[str] = set()
        # idempotency cache: key -> finished exec result payload
        self.idem: dict[str, dict] = {}

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def env(self) -> Any:
        return self.sb.env


class _ExecRecord:
    def __init__(self, env_id: str, task: asyncio.Task, fingerprint: str) -> None:
        self.env_id = env_id
        self.task = task
        self.fingerprint = fingerprint
        self.cancelled = False


class _CheckpointRecord:
    def __init__(self, ref: str, env_id: str, workspace: str,
                 owned_temp: list[str], label: str | None) -> None:
        self.ref = ref
        self.env_id = env_id      # the environment that owns this checkpoint
        self.workspace = workspace
        self.owned_temp = owned_temp
        self.label = label


_ENVS: dict[str, _EnvRecord] = {}
_EXECS: dict[str, _ExecRecord] = {}
_CHECKPOINTS: dict[str, _CheckpointRecord] = {}
_TORN_DOWN: dict[str, float] = {}


def _env(env_id: str) -> _EnvRecord:
    rec = _ENVS.get(env_id)
    if rec is None:
        torn_down_at = _TORN_DOWN.get(env_id)
        if torn_down_at is not None and time.monotonic() - torn_down_at <= _LEASE_TTL_SECONDS:
            raise HTTPException(409, {"code": "sandbox_torn_down",
                                      "message": "sandbox was torn down and reclaimed"})
        _TORN_DOWN.pop(env_id, None)
        raise HTTPException(404, {"code": "environment_not_found", "message": env_id})
    if getattr(rec.env, 'revoked', False):
        rec.alive = False
    if not rec.alive:
        raise HTTPException(409, {"code": "sandbox_torn_down",
                                  "message": "sandbox was torn down to guarantee quiescence"})
    if rec.restoring:
        raise HTTPException(409, {"code": "sandbox_restoring", "message": "restore is in progress"})
    rec.touch()
    return rec


def _exec(exec_id: str) -> _ExecRecord:
    rec = _EXECS.get(exec_id)
    if rec is None:
        raise HTTPException(404, {"code": "execution_not_found", "message": exec_id})
    return rec


def _guard_generation(rec: _EnvRecord, gen: str | None) -> None:
    """Reject a request that carries a generation older than the sandbox's.

    Only checked when the caller supplies one — a plain client that never
    restores can omit the header and is unaffected.
    """
    if gen is None:
        return
    try:
        want = int(gen)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, {"code": "invalid_generation", "message": gen}) from exc
    if want != rec.generation:
        raise HTTPException(409, {
            "code": "stale_generation",
            "message": f"sandbox is at generation {rec.generation}, request carried {want}",
            "current_generation": rec.generation,
        })


@contextlib.asynccontextmanager
async def _operation(env_id: str, generation: str | None):
    """Fence queued operations against restore and destruction, including files."""
    rec = _env(env_id)
    _guard_generation(rec, generation)
    admitted_generation = rec.generation
    async with rec.snapshot_lock:
        _env(env_id)
        _guard_generation(rec, str(admitted_generation))
        try:
            yield rec
        except Exception as exc:
            if getattr(rec.env, 'revoked', False):
                rec.alive = False
                raise HTTPException(409, {
                    'code': 'sandbox_torn_down',
                    'message': f'sandbox is no longer usable; operation failed: {exc}',
                }) from exc
            raise
        finally:
            if getattr(rec.env, 'revoked', False):
                rec.alive = False


def _dump(value: Any) -> Any:
    """ExecResult / TextFileRange are dataclasses; keep every field."""
    return asdict(value) if is_dataclass(value) else value


def _fail(exc: Exception) -> HTTPException:
    """Map an environment exception to a code the client re-raises as the same type.

    The client must reproduce the exception TYPE, not just the message:
    OpenCollab's execute_tool catches everything and renders it into the
    agent's transcript, so a wrong type becomes a wrong sentence in context.
    """
    if isinstance(exc, HTTPException):
        return exc
    table = [
        (PermissionError, 403, "path_escapes_workspace"),
        (FileNotFoundError, 404, "not_found"),
        (ValueError, 400, "invalid_argument"),
        (OSError, 400, "os_error"),
    ]
    for kind, status, code in table:
        if isinstance(exc, kind):
            return HTTPException(status, {"code": code, "message": str(exc)})
    return HTTPException(500, {"code": "internal", "message": str(exc)})


# ==========================================================================
# lease reaper
# ==========================================================================

async def _reclaim_env(env_id: str, rec: _EnvRecord) -> None:
    """Fully release an environment: its executions, its container, AND the
    checkpoint images it owns. Reaping only the container would leak images."""
    if rec.restoring:
        raise HTTPException(409, {"code": "sandbox_restoring", "message": "retry cleanup after restore"})
    was_torn_down = not rec.alive
    rec.alive = False
    rec.env.revoke()
    # Keep records until cleanup succeeds, so a failed kernel kill/unmount is
    # visible to the caller and the same DELETE/reaper can retry it.
    if not rec.primary_cleanup_done:
        await BACKEND.destroy(rec.sb)
        rec.primary_cleanup_done = True
    for sandbox in list(rec.pending_cleanup):
        await BACKEND.destroy(sandbox)
        rec.pending_cleanup.remove(sandbox)
    for ref, ck in list(_CHECKPOINTS.items()):
        if ck.env_id == env_id:
            await BACKEND.discard(ref)
            _CHECKPOINTS.pop(ref, None)
    for execution_id in list(rec.executions):
        _EXECS.pop(execution_id, None)
    _ENVS.pop(env_id, None)
    if was_torn_down:
        _TORN_DOWN[env_id] = time.monotonic()


async def _reaper() -> None:
    """Reclaim sandboxes idle past the lease TTL so a dead client cannot leak one."""
    while True:
        await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
        now = time.monotonic()
        for eid, torn_down_at in list(_TORN_DOWN.items()):
            if now - torn_down_at > _LEASE_TTL_SECONDS:
                _TORN_DOWN.pop(eid, None)
        stale = [eid for eid, rec in _ENVS.items()
                 if not rec.alive or now - rec.last_used > _LEASE_TTL_SECONDS]
        for eid in stale:
            rec = _ENVS.get(eid)
            if rec is not None:
                try:
                    await _reclaim_env(eid, rec)
                except Exception:
                    logging.getLogger(__name__).exception("Environment cleanup failed; retained for retry")


# ==========================================================================
# environment lifecycle
# ==========================================================================

class _CreateEnvBody(BaseModel):
    image: str = "python:3.11-slim"


def _capabilities(env: Any) -> dict:
    return {
        # BashTool refuses to run when this is false.
        "process_isolated": getattr(env, "process_isolated", False),
        # WorktreePool branches on this; a remote env must report false.
        "local_filesystem": getattr(env, "local_filesystem", False),
        "checkpointable": True,
    }


@app.post("/v1/environments")
async def create_environment(body: _CreateEnvBody) -> dict:
    sb = await BACKEND.create(body.image)
    env_id = "env_" + uuid.uuid4().hex[:12]
    rec = _EnvRecord(sb)
    _ENVS[env_id] = rec
    return {"id": env_id, "workspace": sb.env.workspace,
            "capabilities": _capabilities(sb.env),
            "generation": rec.generation,
            "lease_ttl_seconds": _LEASE_TTL_SECONDS}


@app.get("/v1/environments/{env_id}")
async def get_environment(env_id: str) -> dict:
    rec = _env(env_id)
    return {
        "id": env_id,
        "workspace": rec.env.workspace,
        "revoked": rec.env.revoked,
        "executions": sorted(rec.executions),
        "idle_seconds": round(time.monotonic() - rec.last_used, 1),
    }


@app.post("/v1/environments/{env_id}/heartbeat")
async def heartbeat(env_id: str) -> dict:
    _env(env_id)   # _env() already refreshes the lease
    return {"state": "alive", "lease_ttl_seconds": _LEASE_TTL_SECONDS}


@app.post("/v1/environments/{env_id}/abort")
async def abort_environment(env_id: str) -> dict:
    rec = _env(env_id)
    await rec.env.abort()
    return {"state": "aborted"}


@app.delete("/v1/environments/{env_id}")
async def destroy_environment(env_id: str) -> dict:
    rec = _ENVS.get(env_id)
    if rec is None:
        if env_id in _TORN_DOWN:
            _TORN_DOWN.pop(env_id, None)
            return {"state": "destroyed"}
        raise HTTPException(404, {"code": "environment_not_found", "message": env_id})
    try:
        await _reclaim_env(env_id, rec)
        _TORN_DOWN.pop(env_id, None)  # explicit DELETE completes the lifecycle
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, {"code": "cleanup_failed", "message": str(exc)}) from exc
    return {"state": "destroyed"}


# ==========================================================================
# execution
# ==========================================================================

class _ExecBody(BaseModel):
    # EnvironmentPort.exec_cmd takes a shell string, not argv. Do not "improve"
    # this into argv — it would force a change to OpenCollab's own contract.
    command: str
    timeout: float = 120.0
    # A client MAY supply the execution id. This makes cancellation robust: if
    # the POST response is lost on the wire, the client already knows the id and
    # can still DELETE /executions/{id}. Omit it and the server generates one.
    execution_id: str | None = None


def _fingerprint(body: _ExecBody) -> str:
    return f"{body.timeout}:{body.command}"


@app.post("/v1/environments/{env_id}/executions")
async def start_execution(
    env_id: str,
    body: _ExecBody,
    idempotency_key: str | None = Header(default=None),
    x_sandbox_generation: str | None = Header(default=None),
) -> dict:
    rec = _env(env_id)
    _guard_generation(rec, x_sandbox_generation)

    # Idempotency: a retried POST after a network blip must not run the command
    # twice (imagine `git commit`). Because this handler yields no control before
    # it records the key, two concurrent requests with the same key are already
    # serialized by the event loop — the second sees the first's entry. A repeat
    # with the SAME key but DIFFERENT parameters is a caller bug, not a retry, so
    # it is refused rather than silently replaying the first result.
    if idempotency_key and idempotency_key in rec.idem:
        prior = rec.idem[idempotency_key]
        if prior["fingerprint"] != _fingerprint(body):
            raise HTTPException(409, {
                "code": "idempotency_conflict",
                "message": "same Idempotency-Key reused with different parameters",
            })
        return prior["payload"]

    # Client-supplied id makes cancel robust against a lost POST response.
    exec_id = body.execution_id or ("exec_" + uuid.uuid4().hex[:12])
    if exec_id in _EXECS:
        prior_exec = _EXECS[exec_id]
        if prior_exec.env_id != env_id or prior_exec.fingerprint != _fingerprint(body):
            raise HTTPException(409, {
                "code": "execution_id_conflict",
                "message": "execution_id already belongs to a different environment or request",
            })
        # Only an exact retry may reuse an existing execution handle.
        return {"execution_id": exec_id, "state": "running"}

    # Capture the generation at enqueue time. The generation header guards the
    # ENTRANCE, but a restore can still land between here and the moment the task
    # acquires the snapshot lock — at which point rec.env is a different
    # container. Re-check under the lock so a command never runs on a sandbox it
    # was not aimed at.
    enqueue_gen = rec.generation

    async def run() -> Any:
        async with _operation(env_id, str(enqueue_gen)) as current:
            return await current.env.exec_cmd(body.command, timeout=body.timeout)

    task = asyncio.create_task(run())
    _EXECS[exec_id] = _ExecRecord(env_id, task, _fingerprint(body))
    rec.executions.add(exec_id)
    payload = {"execution_id": exec_id, "state": "running"}
    if idempotency_key:
        rec.idem[idempotency_key] = {"payload": payload, "fingerprint": _fingerprint(body)}
    return payload


@app.get("/v1/executions/{exec_id}")
async def wait_execution(exec_id: str) -> dict:
    """Long-poll until the command finishes.

    A recoverable timeout returns returncode = -1 with partial output. If
    cleanup instead revokes the sandbox, the operation returns an explicit
    sandbox_torn_down error; it must not masquerade as a recoverable timeout.
    """
    rec = _exec(exec_id)
    env_rec = _ENVS.get(rec.env_id)
    if env_rec is not None:
        env_rec.touch()
    try:
        result = await asyncio.shield(rec.task)
    except asyncio.CancelledError:
        return {"state": "cancelled", "quiesced": True}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"state": "completed", "result": _dump(result)}


@app.delete("/v1/executions/{exec_id}")
async def cancel_execution(exec_id: str) -> dict:
    """Cancel and PROVE the remote side went quiet before answering.

    This is the correctness point of the whole service. OpenCollab's contract
    is "cancel returned => execution quiesced". If cancelling only dropped the
    HTTP connection, `pytest` would keep running on this side and keep
    producing side effects while the agent believes it stopped. So: cancel the
    task and await its unwinding, which drives DockerEnvironment's guarantee:
    it kills the command's process group inside the container, and if it cannot
    prove that group is gone, it tears the whole container down rather than let
    a process leak. Either way, nothing is still running when we return.

    The second, harsher path leaves the sandbox dead. We detect that here and
    mark the environment so later calls get a clear 409 instead of a 500.
    """
    rec = _exec(exec_id)
    rec.cancelled = True
    rec.task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await rec.task
    env_rec = _ENVS.get(rec.env_id)
    torn_down = False
    if env_rec is not None:
        env_rec.executions.discard(exec_id)
        if not BACKEND.alive(env_rec.sb):
            env_rec.alive = False
            torn_down = True
    _EXECS.pop(exec_id, None)
    return {
        "state": "cancelled",
        "quiesced": True,
        "sandbox": "torn_down" if torn_down else "alive",
    }


# ==========================================================================
# files
# ==========================================================================

class _PathBody(BaseModel):
    path: str


class _RangeBody(BaseModel):
    path: str
    offset: int = 1
    limit: int = 2000
    max_chars: int = 30000


class _WriteBody(BaseModel):
    path: str
    content: str


class _TempBody(BaseModel):
    content: str
    prefix: str
    suffix: str = ".tmp"


@app.post("/v1/environments/{env_id}/files/read")
async def read_file(env_id: str, body: _PathBody,
                    x_sandbox_generation: str | None = Header(default=None)) -> dict:
    try:
        async with _operation(env_id, x_sandbox_generation) as rec:
            return {"content": await rec.env.read_file(body.path)}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@app.post("/v1/environments/{env_id}/files/read_range")
async def read_range(env_id: str, body: _RangeBody,
                     x_sandbox_generation: str | None = Header(default=None)) -> dict:
    # DockerEnvironment overrides this deliberately: it avoids pulling a whole
    # file across. Over a network that reason gets stronger, not weaker.
    try:
        async with _operation(env_id, x_sandbox_generation) as rec:
            got = await rec.env.read_text_range(
                body.path, offset=body.offset, limit=body.limit, max_chars=body.max_chars
            )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return _dump(got)


@app.post("/v1/environments/{env_id}/files/write")
async def write_file(env_id: str, body: _WriteBody,
                     x_sandbox_generation: str | None = Header(default=None)) -> dict:
    try:
        async with _operation(env_id, x_sandbox_generation) as rec:
            await rec.env.write_file(body.path, body.content)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {}


@app.post("/v1/environments/{env_id}/files/write_temp")
async def write_temp(env_id: str, body: _TempBody,
                     x_sandbox_generation: str | None = Header(default=None)) -> dict:
    try:
        async with _operation(env_id, x_sandbox_generation) as rec:
            path = await rec.env.write_temp_file(
                body.content, prefix=body.prefix, suffix=body.suffix
            )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"path": path}


@app.post("/v1/environments/{env_id}/files/remove")
async def remove_file(env_id: str, body: _PathBody,
                      x_sandbox_generation: str | None = Header(default=None)) -> dict:
    # NOT a general delete. DockerEnvironment refuses any path it did not
    # itself create via write_temp_file. Keep that refusal — a general delete
    # hands agents a primitive the rest of OpenCollab assumes they lack.
    try:
        async with _operation(env_id, x_sandbox_generation) as rec:
            await rec.env.remove_file(body.path)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {}


# ==========================================================================
# snapshots
# ==========================================================================

class _CheckpointBody(BaseModel):
    label: str | None = None


@app.post("/v1/environments/{env_id}/checkpoints")
async def checkpoint(env_id: str, body: _CheckpointBody,
                     x_sandbox_generation: str | None = Header(default=None)) -> dict:
    """Snapshot the whole container as an image, plus adapter metadata.

    Filesystem is not the whole story. DockerEnvironment keeps temp-file
    ownership in a Python set (`_temporary_files`); restore rebuilds the object,
    so that set must be captured here and re-seeded, or remove_file() would
    wrongly refuse a file that is physically back.
    """
    ref = "ckpt_" + uuid.uuid4().hex[:12]
    async with _operation(env_id, x_sandbox_generation) as rec:
        await BACKEND.snapshot(rec.sb, ref)
        owned = sorted(getattr(rec.env, "_temporary_files", set()))
        _CHECKPOINTS[ref] = _CheckpointRecord(ref, env_id, rec.env.workspace, owned, body.label)
    return {"checkpoint_id": ref, "label": body.label}


@app.get("/v1/environments/{env_id}/checkpoints")
async def list_checkpoints(env_id: str) -> dict:
    _env(env_id)
    # only the checkpoints this environment owns, not the whole server's
    return {"checkpoints": [
        {"checkpoint_id": c.ref, "label": c.label}
        for c in _CHECKPOINTS.values() if c.env_id == env_id
    ]}


def _seed_temp(env: Any, owned: list[str]) -> None:
    if hasattr(env, "_temporary_files"):
        env._temporary_files = set(owned)  # noqa: SLF001


def _owned_checkpoint(env_id: str, ckpt_id: str) -> _CheckpointRecord:
    """A checkpoint may only be operated on through the environment that owns
    it. Otherwise one environment could restore/fork/discard another's snapshot
    just by knowing its id. A non-owner sees a plain 404 — existence is not
    revealed across environments."""
    ck = _CHECKPOINTS.get(ckpt_id)
    if ck is None or ck.env_id != env_id:
        raise HTTPException(404, {"code": "checkpoint_not_found", "message": ckpt_id})
    return ck


async def _wait_owned(task: asyncio.Task) -> Any:
    """Drain a server-owned transition despite cancellation of its HTTP waiter."""
    interrupted: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            interrupted = exc
    # Lifecycle failure takes precedence and remains observable. Otherwise the
    # caller still receives its cancellation after the transition is settled.
    result = task.result()
    if interrupted is not None:
        raise interrupted
    return result


async def _restore_transition(rec: _EnvRecord, ck: _CheckpointRecord) -> dict:
    try:
        for exec_id in list(rec.executions):
            er = _EXECS.get(exec_id)
            if er and not er.task.done():
                er.task.cancel()
                try:
                    await er.task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    rec.alive = False
                    raise
            _EXECS.pop(exec_id, None)
        rec.executions.clear()
        async with rec.snapshot_lock:
            old = rec.sb
            new_sb = await BACKEND.materialize(ck.ref)
            rec.pending_cleanup.append(new_sb)
            try:
                _seed_temp(new_sb.env, ck.owned_temp)
                await BACKEND.destroy(old)
            except BaseException:
                rec.alive = False
                try:
                    await BACKEND.destroy(new_sb)
                except BaseException:
                    pass  # retained in pending_cleanup for DELETE/reaper retry
                else:
                    rec.pending_cleanup.remove(new_sb)
                raise
            rec.pending_cleanup.remove(new_sb)
            rec.sb = new_sb
            rec.alive = True
            rec.primary_cleanup_done = False
            rec.generation += 1
            rec.idem.clear()
        return {"state": "restored", "checkpoint_id": ck.ref,
                "workspace": rec.env.workspace, "generation": rec.generation}
    finally:
        rec.restoring = False


@app.post("/v1/environments/{env_id}/checkpoints/{ckpt_id}/restore")
async def restore(env_id: str, ckpt_id: str,
                  x_sandbox_generation: str | None = Header(default=None)) -> dict:
    """Run restore as a server-owned transition and fence all new admissions."""
    rec = _env(env_id)
    _guard_generation(rec, x_sandbox_generation)
    ck = _owned_checkpoint(env_id, ckpt_id)
    # No await before this flag and task registration: competing lifecycle
    # requests cannot enter between validation and admission closure.
    rec.restoring = True
    transition = asyncio.create_task(_restore_transition(rec, ck))
    return await _wait_owned(transition)


@app.post("/v1/environments/{env_id}/checkpoints/{ckpt_id}/fork")
async def fork(env_id: str, ckpt_id: str,
               x_sandbox_generation: str | None = Header(default=None)) -> dict:
    """Branch: a new, independent environment starting from this checkpoint.

    The original is untouched. This is the primitive that makes
    branch-and-compare / RL rollout from one prefix possible — the thing
    "delete the worktree and start over" cannot do. A natural derivative of the
    checkpoint representation, not a new subsystem.
    """
    async with _operation(env_id, x_sandbox_generation):
        ck = _owned_checkpoint(env_id, ckpt_id)
        child_sb = await BACKEND.materialize(ck.ref)
    _seed_temp(child_sb.env, ck.owned_temp)
    child_id = "env_" + uuid.uuid4().hex[:12]
    child_rec = _EnvRecord(child_sb)
    _ENVS[child_id] = child_rec
    return {
        "id": child_id,
        "workspace": child_sb.env.workspace,
        "forked_from": ckpt_id,
        "generation": child_rec.generation,
        "capabilities": _capabilities(child_sb.env),
    }


@app.delete("/v1/environments/{env_id}/checkpoints/{ckpt_id}")
async def discard_checkpoint(env_id: str, ckpt_id: str,
                             x_sandbox_generation: str | None = Header(default=None)) -> dict:
    """Free a checkpoint's backing image. Without this, images pile up and the
    disk fills after enough runs."""
    async with _operation(env_id, x_sandbox_generation):
        ck = _owned_checkpoint(env_id, ckpt_id)
        await BACKEND.discard(ck.ref)
        _CHECKPOINTS.pop(ckpt_id, None)
    return {"state": "discarded", "checkpoint_id": ckpt_id}
