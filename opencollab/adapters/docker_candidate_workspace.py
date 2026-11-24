from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from opencollab.adapters._team_candidate_environment import _TeamCandidateEnvironment
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.session_environments import SessionEnvironmentPool
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.ports import EnvironmentPort, WorktreePoolPort


class _DockerCandidateLease:
    def __init__(
        self, workspace: DockerCandidateWorkspace, pool: SessionEnvironmentPool,
        environment: _TeamCandidateEnvironment, physical_source_revision: str | None,
    ) -> None:
        self._workspace = workspace
        self._pool = pool
        self._physical_source_revision = physical_source_revision
        self._lease = environment._lease
        self.environment = environment

    @property
    def candidate_workspace(self) -> str:
        container = getattr(self.environment, "container_reference", None)
        return f"{container}:{self.environment.workspace}" if container else self.environment.workspace

    @property
    def delivery_diff(self) -> str | None:
        return self._lease.delivery_diff

    @property
    def delivery_paths(self) -> tuple[str, ...]:
        return self._lease.delivery_paths

    async def diff(self, exclude_paths: Sequence[str] = ()) -> str:
        return await self._lease.diff(exclude_paths)

    async def capture_changes(self) -> str:
        return await self._lease.capture_changes()

    async def physical_source_changed(self) -> bool:
        return self._physical_source_revision != await self._workspace.physical_source_revision()

    async def cleanup(self) -> None:
        await self._pool.release()


class DockerCandidateWorkspace(EnvCandidateWorkspace):
    """Capture candidate files from dedicated containers and adopt into the source."""

    def __init__(
        self, environment: EnvironmentPort, *, workspace: str, owned_save_dir: str | None,
        providers: Mapping[str, Callable[..., WorktreePoolPort]],
    ) -> None:
        super().__init__(environment, workspace=workspace, owned_save_dir=owned_save_dir)
        self._providers = providers

    async def acquire(self, label: str) -> _DockerCandidateLease:
        physical_source_revision = await self.physical_source_revision()
        # Each lease owns its pool so a retained container outlives other candidates' cleanup.
        pool = SessionEnvironmentPool(
            self._workspace, isolation="docker-session", base_environment=self._environment,
            providers=self._providers, owned_save_dir=self._owned_save_dir,
        )
        try:
            candidate = await pool.acquire(label)
            if not isinstance(candidate, _TeamCandidateEnvironment) or candidate.process_isolated is not True:
                raise TypeError("docker-session candidate provider must supply an isolated Git candidate environment")
            return _DockerCandidateLease(self, pool, candidate, physical_source_revision)
        except BaseException as failure:
            try:
                await await_owned_operation(pool.release(), propagate_cancellation=False)
            except BaseException as cleanup_error:
                add_exception_note(failure, f"candidate sandbox cleanup failed: {cleanup_error}")
            raise
