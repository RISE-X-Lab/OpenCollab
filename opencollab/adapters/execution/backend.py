"""The backend seam shared by every backend and the server.

Kept free of FastAPI / opencollab-heavy imports so a backend (and its tests)
can be exercised without starting the HTTP server. Exec and file operations do
NOT live here — those already go through OpenCollab's EnvironmentPort and are
backend-agnostic. What differs between Docker, an OS-level sandbox, and a
microVM is only container lifecycle + snapshot, which is exactly this Protocol.
"""

from __future__ import annotations

from typing import Any, Protocol


class Sandbox:
    """One live sandbox: the OpenCollab environment plus its backend handle."""

    def __init__(self, env: Any, handle: str) -> None:
        self.env = env          # exec/files go here (EnvironmentPort)
        self.handle = handle    # backend-specific id (container id / sandbox root)


class SandboxBackend(Protocol):
    async def create(self, image: str) -> Sandbox: ...
    async def destroy(self, sb: Sandbox) -> None: ...
    async def snapshot(self, sb: Sandbox, ref: str) -> None: ...
    async def materialize(self, ref: str) -> Sandbox: ...
    async def discard(self, ref: str) -> None: ...
    def alive(self, sb: Sandbox) -> bool: ...
