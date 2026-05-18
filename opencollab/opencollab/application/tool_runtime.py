from __future__ import annotations

from dataclasses import dataclass

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
