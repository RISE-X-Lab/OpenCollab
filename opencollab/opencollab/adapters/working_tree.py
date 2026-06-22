"""Env-backed ``WorkingTreeProbe`` — answers "did the working tree change?".

The application-layer workflow uses a :class:`~opencollab.application.ports.WorkingTreeProbe`
to verify that an agent actually edited the tree before declaring success. This
concrete implementation backs that probe with the task ``Environment``, running
``git -C <workspace> status --porcelain`` (and ``git diff`` for the optional
diff). It lives in ``adapters`` because it depends on a concrete environment;
the workflow only ever sees the abstract port.
"""

from __future__ import annotations

import shlex
from typing import Any


class EnvWorkingTreeProbe:
    """``WorkingTreeProbe`` backed by an :class:`Environment`.

    ``changed()`` runs ``git status --porcelain`` — non-empty output means the
    working tree has uncommitted changes (modified, added, or untracked files).
    Both methods are best-effort: any exec failure is treated as "no change"
    (``changed`` -> ``False``) / empty diff so a flaky git call never blocks a
    workflow. The workflow swallows even those to ``None`` via ``ctx.tree_changed``.
    """

    def __init__(self, env: Any, *, workspace: str | None = None) -> None:
        self._env = env
        # Pin the repo dir explicitly so the probe is correct regardless of the
        # env's cwd handling. Falls back to the env's own workspace attribute.
        self._workspace = workspace or getattr(env, "workspace", ".") or "."

    async def changed(self) -> bool:
        cmd = f"git -C {shlex.quote(self._workspace)} status --porcelain"
        result = await self._env.exec_cmd(cmd, timeout=30)
        return bool(result.stdout.strip())

    async def diff(self) -> str:
        cmd = f"git -C {shlex.quote(self._workspace)} diff"
        result = await self._env.exec_cmd(cmd, timeout=30)
        return result.stdout


__all__ = ["EnvWorkingTreeProbe"]
