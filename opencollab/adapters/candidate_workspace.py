"""Environment-backed isolated candidate worktrees."""

from __future__ import annotations

import os
import posixpath
import shlex
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.env import DockerWorkspaceEnvironment


def _complete(result: Any, operation: str, allowed: tuple[int, ...] = (0,)) -> str:
    if (
        getattr(result, "returncode", -1) not in allowed
        or getattr(result, "stdout_truncated", False)
        or getattr(result, "stderr_truncated", False)
    ):
        detail = str(getattr(result, "stderr", "") or "").strip()
        raise RuntimeError(f"{operation} failed: {detail or 'incomplete command result'}")
    return str(getattr(result, "stdout", "") or "")


def _safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("candidate preserve path must be non-empty text")
    normalized = posixpath.normpath(value)
    if posixpath.isabs(normalized) or normalized == ".." or normalized.startswith("../"):
        raise ValueError("candidate preserve path escapes the repository")
    return normalized


async def _raw_diff(environment: Any, exclude_paths: Sequence[str] = ()) -> str:
    workspace = getattr(environment, "workspace", None)
    if not isinstance(workspace, str) or not workspace:
        raise RuntimeError("candidate diff workspace is unavailable")
    return await _raw_diff_at(environment, workspace, exclude_paths)


async def _raw_diff_at(
    environment: Any,
    workspace: str,
    exclude_paths: Sequence[str] = (),
) -> str:
    excluded = tuple(_safe_path(path) for path in exclude_paths)
    pathspec = ""
    if excluded:
        pathspec = " -- . " + " ".join(
            shlex.quote(f":(exclude){path}") for path in excluded
        )
    tracked = _complete(
        await environment.exec_cmd(
            "git -C "
            f"{shlex.quote(workspace)} --no-pager diff HEAD --binary --no-ext-diff"
            + pathspec,
            timeout=60,
        ),
        "candidate tracked diff",
    )
    untracked = _complete(
        await environment.exec_cmd(
            "git -C "
            f"{shlex.quote(workspace)} ls-files --others --exclude-standard -z"
            + pathspec,
            timeout=30,
        ),
        "candidate untracked listing",
    )
    parts = [tracked.rstrip("\n")] if tracked.strip() else []
    for path in (item for item in untracked.split("\0") if item):
        patch = _complete(
            await environment.exec_cmd(
                "git -C "
                f"{shlex.quote(workspace)} --no-pager diff --no-index --binary "
                f"--no-ext-diff -- /dev/null {shlex.quote(path)}",
                timeout=60,
            ),
            f"candidate untracked diff for {path}",
            allowed=(0, 1),
        )
        if patch.strip():
            parts.append(patch.rstrip("\n"))
    return "\n".join(parts) + ("\n" if parts else "")


@dataclass(slots=True)
class _CandidateLease:
    base_environment: Any
    environment: Any
    source_workspace: str
    candidate_workspace: str
    cleaned: bool = False

    async def diff(self) -> str:
        return await _raw_diff_at(
            self.base_environment,
            self.candidate_workspace,
        )

    async def cleanup(self) -> None:
        if self.cleaned:
            return
        await self.environment.cleanup()
        result = await self.base_environment.exec_cmd(
            "git -C "
            f"{shlex.quote(self.source_workspace)} worktree remove --force -- "
            f"{shlex.quote(self.candidate_workspace)}",
            timeout=120,
        )
        _complete(result, "candidate worktree cleanup")
        self.cleaned = True


class EnvCandidateWorkspace:
    """Candidate workspace port implemented over one task environment."""

    def __init__(self, environment: Any, *, workspace: str | None = None) -> None:
        self._environment = environment
        self._workspace = workspace or getattr(environment, "workspace", None)
        if not isinstance(self._workspace, str) or not self._workspace:
            raise ValueError("candidate source workspace is unavailable")

    async def _candidate_environment(self, path: str) -> Any:
        if isinstance(self._environment, LocalEnvironment):
            return LocalEnvironment(path)
        if isinstance(self._environment, DockerEnvironment):
            await self._environment.setup()
            container_id = self._environment._container_id
            if not container_id:
                raise RuntimeError("candidate container identity is unavailable")
            return DockerWorkspaceEnvironment(
                container_id=container_id,
                repo_root=path,
                command_prefix=self._environment._command_prefix,
                timeout_returncode=self._environment._timeout_returncode,
            )
        raise RuntimeError("candidate worktrees require a local or Docker environment")

    async def acquire(self, label: str) -> _CandidateLease:
        token = uuid.uuid4().hex
        if isinstance(self._environment, LocalEnvironment):
            path = tempfile.mkdtemp(prefix="opencollab-candidate-")
            os.rmdir(path)
        else:
            path = f"/tmp/opencollab-candidate-{token}"
        result = await self._environment.exec_cmd(
            "git -C "
            f"{shlex.quote(self._workspace)} worktree add --detach -- "
            f"{shlex.quote(path)} HEAD",
            timeout=120,
        )
        _complete(result, f"candidate worktree setup for {label}")
        environment = await self._candidate_environment(path)
        await environment.setup()
        return _CandidateLease(
            base_environment=self._environment,
            environment=environment,
            source_workspace=self._workspace,
            candidate_workspace=path,
        )

    async def _write_patch(self, content: str, prefix: str) -> str:
        return await self._environment.write_temp_file(
            content,
            prefix=prefix,
            suffix=".patch",
        )

    async def source_diff(self, exclude_paths: Sequence[str] = ()) -> str:
        return await _raw_diff_at(
            self._environment,
            self._workspace,
            exclude_paths,
        )

    async def restore_source(self, patch: str) -> None:
        await self._replace_source_diff(patch)

    async def _replace_source_diff(self, patch: str) -> None:
        current = await _raw_diff_at(self._environment, self._workspace)
        current_file = await self._write_patch(current, ".candidate-current-")
        target_file = await self._write_patch(patch, ".candidate-target-")
        reversed_current = False
        try:
            if current.strip():
                await self._apply(
                    current_file,
                    "candidate current-source removal",
                    reverse=True,
                )
                reversed_current = True
            if patch.strip():
                await self._apply(target_file, "candidate source restoration")
        except BaseException as failure:
            if reversed_current:
                try:
                    await self._apply(
                        current_file,
                        "candidate source restoration rollback",
                    )
                except BaseException as rollback:
                    raise RuntimeError(
                        "candidate source restoration and rollback both failed"
                    ) from rollback
            raise failure
        finally:
            for path in (current_file, target_file):
                try:
                    await self._environment.remove_file(path)
                except Exception:
                    pass

    async def _apply(self, path: str, operation: str, *, reverse: bool = False) -> None:
        reverse_flag = " --reverse" if reverse else ""
        result = await self._environment.exec_cmd(
            "git -C "
            f"{shlex.quote(self._workspace)} apply --binary --whitespace=nowarn"
            f"{reverse_flag} -- {shlex.quote(path)}",
            timeout=120,
        )
        _complete(result, operation)

    async def _patch_paths(self, path: str) -> set[str]:
        result = await self._environment.exec_cmd(
            "git -C "
            f"{shlex.quote(self._workspace)} apply --numstat -- {shlex.quote(path)}",
            timeout=30,
        )
        output = _complete(result, "candidate path inspection")
        return {
            line.split("\t", 2)[-1]
            for line in output.splitlines()
            if line.count("\t") >= 2
        }

    async def adopt(
        self,
        patch: str,
        preserve_paths: Sequence[str] = (),
    ) -> None:
        if not isinstance(patch, str) or not patch.strip():
            raise ValueError("candidate patch must be non-empty")
        preserved = tuple(_safe_path(path) for path in preserve_paths)
        original_source = await _raw_diff_at(
            self._environment,
            self._workspace,
            preserved,
        )
        candidate_file = await self._write_patch(patch, ".candidate-adopt-")
        original_file = await self._write_patch(original_source, ".candidate-original-")
        reversed_original = False
        try:
            candidate_paths = await self._patch_paths(candidate_file)
            if candidate_paths.intersection(preserved):
                raise ValueError("candidate patch overlaps a preserved path")
            if original_source.strip():
                await self._apply(
                    original_file,
                    "candidate original-source removal",
                    reverse=True,
                )
                reversed_original = True
            await self._apply(candidate_file, "candidate adoption")
        except BaseException as failure:
            if reversed_original:
                try:
                    await self._apply(original_file, "candidate adoption rollback")
                except BaseException as rollback:
                    raise RuntimeError("candidate adoption and rollback both failed") from rollback
            raise failure
        finally:
            for path in (candidate_file, original_file):
                try:
                    await self._environment.remove_file(path)
                except Exception:
                    pass


__all__ = ["EnvCandidateWorkspace"]
