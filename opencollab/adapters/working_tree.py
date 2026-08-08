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

MAX_WORKING_TREE_DIFF_CHARS = 1_000_000


def _require_complete_result(
    result: Any,
    operation: str,
    *,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> None:
    if (
        bool(getattr(result, "stdout_truncated", False))
        or bool(getattr(result, "stderr_truncated", False))
        or int(getattr(result, "stdout_dropped_bytes", 0) or 0) > 0
        or int(getattr(result, "stderr_dropped_bytes", 0) or 0) > 0
    ):
        raise RuntimeError(f"{operation} exceeded capture limit")
    returncode = int(getattr(result, "returncode", 0))
    if returncode not in allowed_returncodes:
        detail = str(getattr(result, "stderr", "") or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"{operation} failed with git status {returncode}{suffix}"
        )


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
        _require_complete_result(result, "working-tree status")
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
        _require_complete_result(result, "working-tree status")
        return bool(result.stdout.strip())

    async def diff(self) -> str:
        workspace = shlex.quote(self._workspace)
        status_result = await self._env.exec_cmd(
            f"git -C {workspace} status --porcelain=v1 --untracked-files=all",
            timeout=30,
        )
        _require_complete_result(status_result, "working-tree status")

        tracked_result = await self._env.exec_cmd(
            f"git -C {workspace} --no-pager diff HEAD --binary --no-ext-diff --",
            timeout=30,
        )
        _require_complete_result(tracked_result, "tracked diff")

        untracked_result = await self._env.exec_cmd(
            f"git -C {workspace} ls-files --others --exclude-standard -z --",
            timeout=30,
        )
        _require_complete_result(untracked_result, "untracked file listing")
        untracked_paths = [
            path
            for path in untracked_result.stdout.split("\0")
            if path
        ]

        parts: list[str] = []
        total_chars = 0

        def append_complete(part: str) -> None:
            nonlocal total_chars
            separator_chars = 2 if parts else 0
            if total_chars + separator_chars + len(part) > MAX_WORKING_TREE_DIFF_CHARS:
                raise RuntimeError(
                    "working-tree diff exceeded aggregate evidence limit"
                )
            parts.append(part)
            total_chars += separator_chars + len(part)

        status = status_result.stdout.rstrip("\n")
        append_complete(f"[Working tree status]\n{status or '(clean)'}")

        tracked = tracked_result.stdout.rstrip("\n")
        if tracked:
            append_complete(f"[Tracked changes vs HEAD]\n{tracked}")

        for path in untracked_paths:
            result = await self._env.exec_cmd(
                "git -C "
                f"{workspace} --no-pager diff --no-index --binary --no-ext-diff "
                f"-- /dev/null {shlex.quote(path)}",
                timeout=30,
            )
            _require_complete_result(
                result,
                f"untracked diff for {path!r}",
                allowed_returncodes=(0, 1),
            )
            patch = result.stdout.rstrip("\n")
            append_complete(
                f"[Untracked file: {path}]\n"
                f"{patch or '(empty file; no content diff)'}"
            )

        return "\n\n".join(parts)


__all__ = ["EnvWorkingTreeProbe"]
