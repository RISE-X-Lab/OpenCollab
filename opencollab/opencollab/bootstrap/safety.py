from __future__ import annotations

from typing import Any

from opencollab.application.ports import SafetyPolicyPort
from opencollab.tools.safety import SandboxInterceptor


def build_workspace_safety_policy(env: Any) -> SafetyPolicyPort | None:
    if env is None or not getattr(env, "workspace", None):
        return None
    return SandboxInterceptor(env.workspace)
