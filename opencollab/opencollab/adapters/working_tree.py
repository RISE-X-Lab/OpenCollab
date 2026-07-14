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
from collections.abc import Sequence
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

    async def changed_excluding(self, paths: Sequence[str]) -> bool:
        # Empty excludes -> identical to ``changed()`` for ordinary runs.
        if not paths:
            return await self.changed()
        # Each ``:(exclude)<path>`` magic pathspec is ONE shlex-quoted token
        # (quote the whole magic+path). The positive pathspec ``.`` is required —
        # an exclude-only pathspec list matches nothing. ``--untracked-files=all``
        # is required so a NEW injected test file in an otherwise-untracked dir is
        # listed (and thus excludable) per-file: default porcelain collapses such
        # a dir to ``?? tests/``, which a file-level exclude can't match, leaking
        # the injected file back in. With ``=all`` the output is empty iff only
        # injected files were dirty (drops modified-tracked AND untracked-new).
        excludes = " ".join(shlex.quote(f":(exclude){p}") for p in paths)
        cmd = (
            f"git -C {shlex.quote(self._workspace)} status --porcelain "
            f"--untracked-files=all -- . {excludes}"
        )
        result = await self._env.exec_cmd(cmd, timeout=30)
        return bool(result.stdout.strip())

    async def diff(self) -> str:
        cmd = f"git -C {shlex.quote(self._workspace)} diff"
        result = await self._env.exec_cmd(cmd, timeout=30)
        return result.stdout


__all__ = ["EnvWorkingTreeProbe"]
