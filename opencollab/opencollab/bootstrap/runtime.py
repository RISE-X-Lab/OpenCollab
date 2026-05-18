"""Runtime composition context — shared wiring for chat/team factories.

Holds the resolved configuration plus the long-lived collaborators (tracer,
repo map, UI hooks). Does NOT carry an Environment or safety policy: env and
safety-policy lifetime is per-Session / per-delegation / per-task.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from opencollab.core.context import get_repo_map
from opencollab.core.session import EventSink, PermissionPolicy
from opencollab.core.tracer import Tracer


@dataclass
class RuntimeContext:
    workspace: str
    config: dict
    tracer: Tracer | None
    repo_map: str | None
    event_sink: EventSink | None
    permission_policy: PermissionPolicy | None


def build_runtime_context(
    workspace: str,
    cli_overrides: dict,
    *,
    trace: bool,
    event_sink: EventSink | None = None,
    permission_policy: PermissionPolicy | None = None,
    run_id_prefix: str = "",
) -> RuntimeContext:
    abs_workspace = os.path.abspath(workspace)
    tracer = (
        Tracer(run_id=f"{run_id_prefix}{uuid.uuid4().hex[:8]}") if trace else None
    )
    repo_map = get_repo_map(abs_workspace)

    return RuntimeContext(
        workspace=abs_workspace,
        config=dict(cli_overrides),
        tracer=tracer,
        repo_map=repo_map,
        event_sink=event_sink,
        permission_policy=permission_policy,
    )
