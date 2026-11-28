from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any

from opencollab.adapters.docker_isolation import DockerIsolationPool
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.vm_isolation import VMIsolationPool
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.isolation import isolation_name
from opencollab.application.ports import EnvironmentPort, WorktreePoolPort


class SessionEnvironmentPool:
    """Own per-run strategy pools; injected factories create a fresh pool per strategy."""

    inherit_parent_environment = True

    def __init__(
        self, workspace: str, *, isolation: str = "worktree",
        base_environment: EnvironmentPort | None = None,
        providers: Mapping[str, Callable[..., WorktreePoolPort]] | None = None,
        roles: Mapping[str, str | None] | None = None,
        owned_save_dir: str | None = None, enable_adoption: bool = False,
        owned_output_paths: Sequence[str] = (),
    ) -> None:
        self._workspace = workspace
        self._default = isolation_name(isolation)
        self._providers = {
            "docker-group": partial(DockerIsolationPool, scope="group"),
            "docker-session": partial(DockerIsolationPool, scope="session"),
            **dict(providers or {}),
        }
        self._roles = dict(roles or {})
        self._options = dict(owned_save_dir=owned_save_dir, enable_adoption=enable_adoption,
                             owned_output_paths=owned_output_paths)
        self._pools: dict[str, WorktreePoolPort] = {}
        self._owners: dict[int, tuple[EnvironmentPort, WorktreePoolPort, asyncio.Lock, EnvironmentPort]] = {}
        self._references: dict[int, int] = {}
        self._lifecycle = asyncio.Condition()
        self._release_lock = asyncio.Lock()
        self._inflight = 0
        self._closed = False
        for name in (self._default, *self._roles.values()):
            if name is not None:
                self._validate(name)
        self._source = base_environment or LocalEnvironment(workspace)
        self._source_isolation: str | None = None
        self._owned_source = self._source if base_environment is None else None

    @property
    def default_isolation(self) -> str:
        return self._default

    @property
    def strategy_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(("shared", "worktree", *self._providers)))

    def _child_isolation(self, environment: EnvironmentPort, role: str) -> str:
        source_isolation = self._source_isolation if environment is self._source else None
        return self._roles.get(role) or source_isolation or self._default

    def describe(self, environment: EnvironmentPort, *, role: str) -> dict[str, Any]:
        return {"default_isolation": self._child_isolation(environment, role),
                "strategies": self.strategy_names}

    def child_pool(self, environment: EnvironmentPort, *, role: str) -> SessionEnvironmentPool:
        return SessionEnvironmentPool(
            self._workspace, isolation=self._child_isolation(environment, role),
            base_environment=environment, providers=self._providers,
        )

    def _validate(self, name: str) -> None:
        isolation_name(name)
        if name not in {"shared", "worktree"} and name not in self._providers:
            if name in {"vm-group", "vm-session"}:
                raise ValueError(
                    f"isolation strategy {name!r} requires VM resources in config.vm or an environment provider"
                )
            raise ValueError(f"isolation strategy {name!r} needs an environment provider")

    def _pool(self, name: str, source: EnvironmentPort) -> WorktreePoolPort:
        self._validate(name)
        key = name
        if key not in self._pools:
            factory = self._providers.get(name)
            kwargs = dict(workspace=self._workspace, base_environment=source, **self._options)
            pool = (factory(**kwargs) if factory is not None else
                    WorktreePool(use_worktrees=name == "worktree", **kwargs))
            self._pools[key] = pool
        return self._pools[key]

    async def acquire(
        self, role: str, *, source: EnvironmentPort | None = None,
        isolation: str | bool | None = None,
    ) -> Any:
        async with self._lifecycle:
            if self._closed:
                raise RuntimeError("session environment pool is closed")
            self._inflight += 1
        try:
            name = isolation_name(isolation, default=self._roles.get(role) or self._default)
            base = self._source if source is None else source
            pool = self._pool(name, base)
            env = await pool.acquire(role, source=base)
            identity = id(env)
            if env is not base or identity in self._owners:
                if identity not in self._owners:
                    self._owners[identity] = (env, pool, asyncio.Lock(), base)
                self._references[identity] = self._references.get(identity, 0) + 1
            if self._closed:
                await self.release_env(env)
                raise RuntimeError("session environment pool closed during acquisition")
            return env
        finally:
            async with self._lifecycle:
                self._inflight -= 1
                self._lifecycle.notify_all()

    def set_source(self, environment: EnvironmentPort, *, isolation: str | None = None) -> None:
        self._source = environment
        self._source_isolation = isolation_name(isolation)

    async def adopt(self, patch: str) -> None:
        pool = self._delivery_pool()
        await pool.adopt(patch)

    def _delivery_pool(self):
        key = "_delivery"
        if key not in self._pools:
            self._pools[key] = WorktreePool(
                self._workspace, use_worktrees=True, base_environment=self._source, **self._options,
            )
        return self._pools[key]

    async def adopt_captured(self, patch, physical_paths):
        await self._delivery_pool().adopt_captured(patch, physical_paths)

    async def release_env(self, env: EnvironmentPort) -> None:
        await self._release_env(env)

    async def _release_env(self, env: EnvironmentPort, *, all_references: bool = False) -> None:
        identity = id(env)
        owner = self._owners.get(identity)
        if owner is None:
            return
        _, pool, lock, _ = owner
        async with lock:
            if self._owners.get(identity) is not owner:
                return
            if not all_references and self._references[identity] > 1:
                self._references[identity] -= 1
                return
            # A failed final release stays available for scoped retry.
            self._references[identity] = 1
            await pool.release_env(env)
            del self._owners[identity]
            del self._references[identity]

    async def release(self) -> None:
        self._closed = True
        # A VM capacity wait needs the shutdown signal before we drain acquisitions.
        for pool in self._pools.values():
            if isinstance(pool, VMIsolationPool):
                pool.stop_acquiring()
        async with self._release_lock:
            await await_owned_operation(self._release(), propagate_cancellation=True)

    async def _release(self) -> None:
        async with self._lifecycle:
            while self._inflight:
                await self._lifecycle.wait()
        # A failed child can still need its source workspace for a cleanup retry.
        retained_sources: set[int] = set()
        first_failure: Exception | None = None
        for identity, (env, _pool, _lock, source) in reversed(tuple(self._owners.items())):
            if identity not in retained_sources:
                try:
                    await self._release_env(env, all_references=True)
                except Exception as exc:
                    if first_failure is None:
                        first_failure = exc
                else:
                    continue
            retained_sources.add(id(source))
        if first_failure is not None:
            raise first_failure
        failures = []
        for key, pool in reversed(tuple(self._pools.items())):
            try:
                await pool.release()
            except Exception as exc:
                failures.append(exc)
                break
            else:
                del self._pools[key]
        if not failures and self._owned_source is not None:
            await self._owned_source.cleanup()
            self._owned_source = None
        if failures:
            raise OSError("session environment cleanup failed: " + "; ".join(map(str, failures)))
        self._owners.clear()
