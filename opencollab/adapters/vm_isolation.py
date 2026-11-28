from __future__ import annotations

import asyncio
import os
import posixpath
import shlex
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from opencollab.adapters._docker_snapshot import _import_snapshot, _snapshot_contents
from opencollab.adapters._env_vm import VMEnvironment, VMOperationError
from opencollab.adapters._git_patch_bytes import _complete
from opencollab.adapters._team_candidate_environment import _TeamCandidateEnvironment
from opencollab.adapters._vm_leases import VMCapacityError as VMCapacityError
from opencollab.adapters._vm_leases import _VMLeases
from opencollab.adapters.env import Environment
from opencollab.adapters.vm_config import VMResource
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note
from opencollab.domain.identity import role_storage_slug, validate_role_identity


@dataclass
class _VMOwner:
    resource: VMResource
    token: str
    control: VMEnvironment
    root: str
    source_pools: list[WorktreePool] = field(default_factory=list)
    views: list[VMEnvironment] = field(default_factory=list)
    pool: WorktreePool | None = None
    reset: VMEnvironment | None = None
    reset_step: int = 0
    reset_operation: str | None = None
    cleaned: bool = False


class VMIsolationPool:
    def __init__(
        self, workspace: str, *, resources: Sequence[VMResource], lease_database: str,
        base_environment: Environment | None = None, scope: str = "group", wait_timeout: float | None = 0,
        owned_save_dir: str | None = None, enable_adoption: bool = False,
        owned_output_paths: Sequence[str] = (),
    ) -> None:
        if scope not in {"group", "session"}:
            raise ValueError("VM isolation scope must be group or session")
        resources = tuple(resources)
        if not resources or any(not isinstance(resource, VMResource) for resource in resources):
            raise ValueError("VM isolation requires at least one VMResource")
        if len({resource.resource_id for resource in resources}) != len(resources):
            raise ValueError("VM resources must have unique stable resource IDs")
        if any(resource.reset_command is None for resource in resources):
            raise ValueError("VM isolation requires an explicit reset_command for every resource before reuse")
        self._registry = _VMLeases(lease_database, resources, wait_timeout)
        self._workspace = workspace
        self._base_environment = base_environment
        self._scope = scope
        self._owned_save_dir = owned_save_dir
        self._owned_output_paths = tuple(owned_output_paths)
        self.artifact_directory = str(Path(owned_save_dir).absolute()) if owned_save_dir else tempfile.mkdtemp(
            prefix="opencollab-vm-results-",
        )
        self._owners: list[_VMOwner] = []
        self._leases: dict[Environment, _VMOwner] = {}
        self._exports: dict[Environment, Path] = {}
        self._exported: set[Environment] = set()
        self._group: _VMOwner | None = None
        self._group_lock = asyncio.Lock()
        self._lifecycle = asyncio.Condition()
        self._inflight = 0
        self._closed = False
        self._closing = asyncio.Event()
        self._release_lock = asyncio.Lock()
        self._delivery = WorktreePool(
            workspace, use_worktrees=False, base_environment=base_environment, enable_adoption=enable_adoption,
        )

    @property
    def retained_resources(self) -> tuple[str, ...]:
        return tuple(owner.resource.resource_id for owner in self._owners)

    async def _create_owner(self, role: str, source: Environment | None) -> _VMOwner:
        token = uuid.uuid4().hex
        resource = await self._registry.acquire(token, self._closing)
        control = VMEnvironment(resource.connection, resource.workspace_root, state_directory=resource.state_directory)
        owner = _VMOwner(resource, token, control, f"{resource.workspace_root}/{token}")
        self._owners.append(owner)
        try:
            await self._prepare_owner(owner, role, source)
        except BaseException as failure:
            try:
                await await_owned_operation(self._cleanup_owner(owner))
            except BaseException as cleanup_error:
                add_exception_note(failure, f"VM lease retained for cleanup retry: {cleanup_error}")
            raise
        return owner

    async def _prepare_owner(self, owner: _VMOwner, role: str, source: Environment | None) -> None:
        control = owner.control
        await control.setup()
        source_pool = WorktreePool(
            self._workspace, use_worktrees=True, base_environment=self._base_environment,
            enable_adoption=True, owned_save_dir=self._owned_save_dir,
            owned_output_paths=self._owned_output_paths,
        )
        owner.source_pools.append(source_pool)
        candidate = await source_pool.acquire(role, source=source)
        view = await self._import(owner, candidate, "source")
        owner.pool = WorktreePool(view.workspace, use_worktrees=True, base_environment=view, enable_adoption=True)
        await source_pool.release()
        owner.source_pools.remove(source_pool)

    async def _import(self, owner: _VMOwner, source: Environment, component: str) -> VMEnvironment:
        path = posixpath.join(owner.root, component)
        _complete(await owner.control.exec_cmd(f"mkdir -p -- {shlex.quote(path)}"), "VM workspace directory")
        target = VMEnvironment(owner.resource.connection, path, state_directory=owner.resource.state_directory)
        owner.views.append(target)
        workspace = await _import_snapshot(source, target)
        if workspace == target.workspace:
            return target
        view = VMEnvironment(owner.resource.connection, workspace, state_directory=owner.resource.state_directory)
        owner.views.append(view)
        await view.setup()
        return view

    async def acquire(self, role: str, *, source: Environment | None = None) -> Environment:
        role = validate_role_identity(role)
        async with self._lifecycle:
            if self._closed:
                raise RuntimeError("VM isolation pool is closed")
            self._inflight += 1
        owner = None
        created = False
        try:
            # Only group creation is serialized. A session waiting for capacity
            # must leave sibling release operations free to return a VM.
            async with (self._group_lock if self._scope == "group" else nullcontext()):
                owner = self._group if self._scope == "group" else None
                if owner is None:
                    owner = await self._create_owner(role, source)
                    created = True
                assert owner.pool is not None
                review_source = None
                if source is not None and not created:
                    if self._leases.get(source) is owner:
                        review_source = source
                    else:
                        review_source = await self._import(owner, source, f"import-{uuid.uuid4().hex}")
                environment = await owner.pool.acquire(role, source=review_source)
                self._leases[environment] = owner
                component = f"{role_storage_slug(role)}-{uuid.uuid4().hex[:8]}"
                self._exports[environment] = (
                    Path(self.artifact_directory) / "vm" / owner.token / component
                )
                environment.resource_id = owner.resource.resource_id
                environment.export_directory = str(self._exports[environment])
                if self._scope == "group":
                    self._group = owner
                if self._closed:
                    raise RuntimeError("VM isolation pool closed during acquisition")
                return environment
        except BaseException as failure:
            if created and owner is not None:
                try:
                    await await_owned_operation(self._cleanup_owner(owner))
                except BaseException as cleanup_error:
                    add_exception_note(failure, f"VM lease retained for cleanup retry: {cleanup_error}")
            raise
        finally:
            async with self._lifecycle:
                self._inflight -= 1
                self._lifecycle.notify_all()

    async def _export(self, env: Environment, owner: _VMOwner) -> None:
        if not isinstance(env, _TeamCandidateEnvironment):
            raise TypeError("VM pool expected a candidate workspace")
        for view in (owner.control, *owner.views, env._lease.base_environment):
            for operation_id in view.pending_operations:
                try:
                    await view.recover_operation(operation_id)
                except BaseException:
                    if operation_id in view.pending_operations:
                        raise
        if env in self._exported:
            return
        env.retain_changes()
        await env._delegate.cleanup()
        await env.capture_changes()
        view = VMEnvironment(owner.resource.connection, env.workspace, state_directory=owner.resource.state_directory)
        owner.views.append(view)
        destination = self._exports[env]
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".vm-export-", dir=destination.parent))
        try:
            async with _snapshot_contents(view) as (contents, _prefix):
                await await_owned_operation(asyncio.to_thread(
                    shutil.copytree, contents, staging, symlinks=True, dirs_exist_ok=True,
                ), propagate_cancellation=True)
                os.replace(staging, destination)
                self._exported.add(env)
        except BaseException:
            env.retain_changes()
            raise
        finally:
            if staging.exists():
                await await_owned_operation(asyncio.to_thread(shutil.rmtree, staging), propagate_cancellation=True)

    async def _cleanup_owner(self, owner: _VMOwner) -> None:
        if not owner.cleaned:
            for env, value in tuple(self._leases.items()):
                if value is owner:
                    await self._export(env, owner)
            if owner.pool is not None:
                await owner.pool.release()
            for source_pool in tuple(owner.source_pools):
                await source_pool.release()
                owner.source_pools.remove(source_pool)
            for view in owner.views:
                await view.cleanup()
            await owner.control.cleanup()
            owner.cleaned = True
        if owner.reset is None:
            owner.reset = VMEnvironment(
                owner.resource.connection, owner.resource.workspace_root,
                state_directory=owner.resource.state_directory,
            )
        assert owner.resource.reset_command is not None
        commands = (owner.resource.reset_command, f"rm -rf -- {shlex.quote(owner.root)}")
        while owner.reset_step < len(commands):
            try:
                if owner.reset_operation is not None:
                    result = await owner.reset.recover_operation(owner.reset_operation)
                else:
                    owner.reset_operation = uuid.uuid4().hex
                    result = await owner.reset.exec_operation(
                        owner.reset_operation, commands[owner.reset_step], timeout=900,
                    )
            except VMOperationError:
                if owner.reset_operation not in owner.reset.pending_operations:
                    owner.reset_operation = None
                raise
            if result.returncode:
                owner.reset_operation = None
            _complete(result, "VM guest reset" if owner.reset_step == 0 else "VM workspace reset")
            owner.reset_step += 1
            owner.reset_operation = None
        await owner.reset.cleanup()
        self._registry.release(owner.resource, owner.token)
        self._owners.remove(owner)
        for env, value in tuple(self._leases.items()):
            if value is owner:
                self._leases.pop(env)
                self._exports.pop(env)
                self._exported.discard(env)
        if self._group is owner:
            self._group = None

    async def adopt_captured(self, patch: str, physical_paths: Sequence[str]) -> None:
        await self._delivery.adopt_captured(patch, physical_paths)

    async def adopt(self, patch: str) -> None:
        await self._delivery.adopt(patch)

    async def release_env(self, env: Environment) -> None:
        async with self._release_lock:
            owner = self._leases.get(env)
            if owner is None:
                return
            await await_owned_operation(self._export(env, owner), propagate_cancellation=True)
            assert owner.pool is not None
            await await_owned_operation(owner.pool.release_env(env), propagate_cancellation=True)
            if owner is not self._group and not any(
                value is owner and environment is not env for environment, value in self._leases.items()
            ):
                await await_owned_operation(self._cleanup_owner(owner), propagate_cancellation=True)
                return
            self._leases.pop(env)
            self._exports.pop(env)
            self._exported.discard(env)

    def stop_acquiring(self) -> None:
        self._closed = True
        self._closing.set()

    async def release(self) -> None:
        self.stop_acquiring()
        await await_owned_operation(self._release_owned(), propagate_cancellation=True)

    async def _release_owned(self) -> None:
        async with self._lifecycle:
            while self._inflight:
                await self._lifecycle.wait()
        async with self._release_lock:
            errors: list[BaseException] = []
            for owner in reversed(tuple(self._owners)):
                try:
                    await self._cleanup_owner(owner)
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise OSError("VM cleanup or reset failed; leases and workspaces retained for retry") from errors[0]
            await self._delivery.release()

    async def cleanup(self) -> None:
        await self.release()


__all__ = ["VMCapacityError", "VMIsolationPool"]
