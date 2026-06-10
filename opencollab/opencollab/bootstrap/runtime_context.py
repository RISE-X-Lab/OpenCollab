"""Runtime context + workspace safety policy — the pre-session wiring inputs.

``RuntimeContext`` bundles what the CLI/eval entry points resolve before any
session exists (workspace, config overrides, tracer, sinks); the safety-policy
factory turns an environment into its sandbox interceptor. Re-exported from
``bootstrap.container`` so existing import paths keep resolving.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.trace import Tracer
from opencollab.application.ports import (
    EventPublisherPort,
    PermissionPort,
    SafetyPolicyPort,
)


@dataclass
class RuntimeContext:
    workspace: str
    config: dict
    tracer: Tracer | None
    event_sink: EventPublisherPort | None
    permission_policy: PermissionPort | None


def build_runtime_context(
    workspace: str,
    cli_overrides: dict,
    *,
    trace: bool,
    event_sink: EventPublisherPort | None = None,
    permission_policy: PermissionPort | None = None,
    run_id_prefix: str = "",
) -> RuntimeContext:
    """Resolve the workspace path and optional tracer into a ``RuntimeContext``."""
    abs_workspace = os.path.abspath(workspace)
    tracer = (
        Tracer(run_id=f"{run_id_prefix}{uuid.uuid4().hex[:8]}") if trace else None
    )

    return RuntimeContext(
        workspace=abs_workspace,
        config=dict(cli_overrides),
        tracer=tracer,
        event_sink=event_sink,
        permission_policy=permission_policy,
    )


def build_workspace_safety_policy(env: Any) -> SafetyPolicyPort | None:
    """A sandbox interceptor scoped to ``env``'s workspace, or None without one."""
    if env is None or not getattr(env, "workspace", None):
        return None
    return SandboxInterceptor(env.workspace)
