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
