"""Public environment contract and command result value."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0


class Environment:
    """Abstract execution environment used by tools and workflows."""

    workspace: str = "."
    host_workspace: str | None = None
    source_workspace: str | None = None
    local_filesystem: bool = False
    process_isolated: bool = False

    def __init__(self) -> None:
        self._aborted = False

    @property
    def revoked(self) -> bool:
        return self._aborted

    def revoke(self) -> None:
        self._aborted = True

    def _ensure_active(self) -> None:
        if self._aborted:
            raise RuntimeError("Execution environment has been aborted.")

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        raise NotImplementedError

    async def read_file(self, path: str) -> str:
        raise NotImplementedError

    async def write_file(self, path: str, content: str) -> None:
        raise NotImplementedError

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        raise NotImplementedError

    async def remove_file(self, path: str) -> None:
        raise NotImplementedError

    async def cleanup(self) -> None:
        pass

    async def abort(self) -> None:
        self.revoke()
        await self.cleanup()


__all__ = ["Environment", "ExecResult"]
