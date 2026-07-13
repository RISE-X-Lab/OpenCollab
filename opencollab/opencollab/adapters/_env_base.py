"""Base environment contract and command result value."""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0


class Environment:
    """Abstract execution environment. All tools operate through this."""

    workspace: str = "."
    host_workspace: str | None = None
    source_workspace: str | None = None
    local_filesystem: bool = False
    process_isolated: bool = False
    _aborted: bool = False

    @property
    def revoked(self) -> bool:
        """Whether future side effects have been synchronously revoked."""
        return bool(self._aborted)

    def revoke(self) -> None:
        """Synchronously and idempotently reject future side effects."""
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
        """Create one call-owned temporary file for harness control data."""
        raise NotImplementedError("environment adapters must implement exclusive temporary-file creation")

    async def remove_file(self, path: str) -> None:
        result = await self.exec_cmd(
            f"rm -f -- {shlex.quote(path)}",
            timeout=10.0,
        )
        if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
            raise OSError(f"failed to remove environment file: {path}")

    async def registered_retirement_paths(self) -> tuple[str, ...]:
        """Return exact, unchanged internal tombstones under the workspace."""
        return ()

    async def cleanup(self) -> None:
        """Release every owned resource; implementations must be idempotent.

        Returning certifies that cleanup finished.  If cancellation interrupts
        an attempt, the harness may call this method again from the resource's
        setup owner, including while an ``asyncio.run`` loop is shutting down.
        """
        pass

    async def abort(self) -> None:
        """Synchronously revoke future effects, then stop owned activity.

        Implementations must be idempotent because the setup owner retries an
        attempt interrupted by cancellation before it considers the resource
        released.
        """
        self.revoke()


__all__ = ["Environment", "ExecResult"]
