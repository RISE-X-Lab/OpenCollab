from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from opencollab.application.ports import EnvironmentPort, PermissionPort, SafetyPolicyPort


@dataclass(frozen=True)
class ToolRuntime:
    environment: EnvironmentPort | None
    safety_policy: SafetyPolicyPort | None
    permission_policy: PermissionPort | None

    def confirm_fn(self):
        if self.permission_policy is None:
            return None
        return self.permission_policy.confirm


@dataclass(frozen=True)
class CallbackPermissionPort:
    confirm_callback: Callable[[str], Awaitable[bool]]

    async def confirm(self, prompt: str) -> bool:
        return await self.confirm_callback(prompt)
