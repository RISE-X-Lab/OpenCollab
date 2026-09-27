"""Additional command guard selected by the opt-in Single2 profile."""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable

from opencollab.adapters.safety import SandboxInterceptor
from opencollab.application.ports import SafetyPolicyPort

_BLOCKED_CONTAINER_SEARCH_RE = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bfind\s+/(?:\s|$)",
        r"\b(?:grep|egrep|fgrep)\b(?=[^|;&\n]*(?:-[^\s]*[rR][^\s]*|--recursive|--directories=(?:recurse|read)))(?=[^|;&\n]*\s/(?:\s|$))",
        r"\brg\b(?=[^|;&\n]*\s/(?:\s|$))",
    )
)


class Single2SafetyPolicy:
    """Preserve the active safety policy and add the root-search guard."""

    def __init__(self, policy: SafetyPolicyPort, workspace: str):
        self._policy = policy
        self.root = os.path.abspath(workspace)

    def check_path(self, target_path: str) -> str:
        return self._policy.check_path(target_path)

    def check_cmd(self, cmd: str) -> None:
        self._policy.check_cmd(cmd)
        self._check_container_search(cmd)

    def is_risky(self, cmd: str) -> bool:
        return self._policy.is_risky(cmd)

    async def check_cmd_interactive(
        self,
        cmd: str,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        await self._policy.check_cmd_interactive(cmd, confirm_fn)
        self._check_container_search(cmd)

    def _check_container_search(self, cmd: str) -> None:
        if any(pattern.search(cmd) for pattern in _BLOCKED_CONTAINER_SEARCH_RE):
            raise PermissionError(
                "Blocked container-wide recursive search: do not scan '/'. "
                f"Search known paths inside the workspace ({self.root}) or "
                "use the dedicated grep tool."
            )


def wrap_single2_safety(
    policy: SafetyPolicyPort | None,
    workspace: str,
) -> SafetyPolicyPort:
    """Layer Single2's root-search guard over a workspace policy."""
    base = policy if policy is not None else SandboxInterceptor(workspace)
    return Single2SafetyPolicy(base, workspace)


__all__ = ["Single2SafetyPolicy", "wrap_single2_safety"]
