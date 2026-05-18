from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

if TYPE_CHECKING:
    from opencollab.application.tool_runtime import ToolRuntime


class EnvironmentPort(Protocol):
    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> Any:
        ...

    async def read_file(self, path: str) -> str:
        ...

    async def write_file(self, path: str, content: str) -> None:
        ...


class SafetyPolicyPort(Protocol):
    def check_path(self, target_path: str) -> str:
        ...

    def check_cmd(self, cmd: str) -> None:
        ...

    def is_risky(self, cmd: str) -> bool:
        ...

    def check_cmd_interactive(
        self,
        cmd: str,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> Awaitable[None]:
        ...


SafetyPolicyFactory = Callable[[Any], SafetyPolicyPort | None]


class PermissionPort(Protocol):
    async def confirm(self, prompt: str) -> bool:
        ...


class EventPublisherPort(Protocol):
    async def emit(self, event: Any) -> None:
        ...


class ToolPort(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai_schema(self) -> dict[str, Any]:
        ...

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: "ToolRuntime",
    ) -> str:
        ...


class TeammateSessionPort(Protocol):
    """Subset of Session the team orchestrator drives a teammate through."""

    used_tokens: int

    async def add_user_message(self, content: str) -> None:
        ...

    async def run_loop(self) -> str:
        ...


class SessionFactoryPort(Protocol):
    """Factory the team layer uses to build a teammate session.

    Bootstrap binds this to the concrete teammate-session builder so the
    team layer does not import ``opencollab.core.session.Session``.
    """

    def build_teammate_session(
        self,
        *,
        role: str,
        env: Any,
        budget: int,
        max_steps: int = 50,
    ) -> TeammateSessionPort:
        ...
