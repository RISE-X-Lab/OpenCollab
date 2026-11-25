from __future__ import annotations

import asyncio
import os
import posixpath
import shlex
import shutil
import stat
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from opencollab.adapters._candidate_physical import _initialized_submodule_paths
from opencollab.adapters._env_docker import DockerEnvironment
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_vm import VMEnvironment, VMTransportError
from opencollab.adapters._env_worktree import WorktreeEnvironment
from opencollab.adapters._git_patch_bytes import _complete, _patch_temporary
from opencollab.adapters._team_candidate_environment import _TeamCandidateEnvironment
from opencollab.adapters.safe_anchored_files import open_regular_read_at
from opencollab.application.async_timeout import await_owned_operation


async def _copy_to_host(environment: Any, source: str, target: Path, *, directory: bool = False) -> None:
    delegate = environment._delegate if isinstance(environment, _TeamCandidateEnvironment) else environment
    if getattr(delegate, "local_filesystem", False):
        if directory:
            operation = asyncio.to_thread(shutil.copytree, source, target, symlinks=True, dirs_exist_ok=True)
        else:
            operation = asyncio.to_thread(shutil.copyfile, source, target)
        await await_owned_operation(operation, propagate_cancellation=True)
        return
    if isinstance(delegate, VMEnvironment):
        await delegate.copy_to_host(source, target, directory=directory)
        return
    if not isinstance(delegate, DockerEnvironment):
        raise TypeError("Workspace snapshots require a local, Docker, or VM source environment")
    await delegate.setup()
    source_path = source.rstrip("/") + "/." if directory else source
    result = await delegate._docker("cp", "--", f"{delegate._container_id}:{source_path}", str(target))
    if result.returncode or result.stdout_dropped_bytes or result.stderr_dropped_bytes:
        raise OSError("Docker source workspace copy failed")


async def _copy_artifact_to_host(environment: Any, source: str, target: Path) -> None:
    delegate = environment._delegate if isinstance(environment, _TeamCandidateEnvironment) else environment
    if isinstance(delegate, WorktreeEnvironment):
        delegate._ensure_active()
        if delegate._local_env is None:
            await delegate.setup()
        delegate = delegate._local_env
    if isinstance(delegate, LocalEnvironment):
        def copy() -> None:
            with open_regular_read_at(
                delegate._root_fd(), delegate.workspace, delegate._relative_path(source),
            ) as handle, target.open("wb") as output:
                shutil.copyfileobj(handle, output)

        await delegate._run_file_operation(copy)
        return
    relative = (
        posixpath.relpath(source, environment.workspace) if posixpath.isabs(source) else posixpath.normpath(source)
    )
    if relative == ".." or relative.startswith("../"):
        raise PermissionError("artifact path escapes workspace")
    source = posixpath.join(environment.workspace, source)
    if isinstance(delegate, DockerEnvironment):
        source = await delegate._contained_file_path(source)
    if isinstance(delegate, (DockerEnvironment, VMEnvironment)):
        try:
            await _copy_to_host(delegate, source, target)
        except (OSError, VMTransportError) as exc:
            if await _remote_path_absent(delegate, source):
                target.unlink(missing_ok=True)
                raise FileNotFoundError(source) from exc
            raise
        if not stat.S_ISREG(target.lstat().st_mode):
            raise OSError("artifact transfer requires a regular file")
        return
    target.write_text(await environment.read_file(source), encoding="utf-8")


async def _remote_path_absent(environment: Any, source: str) -> bool:
    """A successful absence probe keeps transport failures visible."""
    try:
        result = await environment.exec_cmd(f"test ! -e {shlex.quote(source)}", timeout=30)
    except Exception:
        return False
    return (
        result.returncode == 0 and not result.timed_out
        and not result.stdout_truncated and not result.stderr_truncated
        and not result.stdout_dropped_bytes and not result.stderr_dropped_bytes
    )


def _remove_marker(path: Path) -> None:
    marker = path / ".git"
    if marker.is_dir() and not marker.is_symlink():
        shutil.rmtree(marker)
    elif os.path.lexists(marker):
        marker.unlink()


@asynccontextmanager
async def _snapshot_contents(source: Any):
    probe = await source.exec_cmd(f"git -C {shlex.quote(source.workspace)} rev-parse --show-toplevel")
    _complete(probe, "Docker source repository probe", allowed=(0, 128))
    repository = probe.returncode == 0
    root = probe.stdout.removesuffix("\n") if repository else source.workspace
    prefix = posixpath.relpath(source.workspace, root) if repository else "."
    modules = await _initialized_submodule_paths(source, root, strict=True) if repository else ()
    directory = Path(tempfile.mkdtemp(prefix="opencollab-docker-import-"))
    try:
        contents = directory / "workspace"
        contents.mkdir()
        await _copy_to_host(source, root, contents, directory=True)
        if repository:
            for relative in (".", *modules):
                source_root = posixpath.normpath(posixpath.join(root, relative))
                local_root = contents.joinpath(*relative.split("/"))
                if not local_root.is_dir() or local_root.is_symlink():
                    raise RuntimeError("Docker source repository must be a physical directory")
                await await_owned_operation(asyncio.to_thread(_remove_marker, local_root), propagate_cancellation=True)
                object_format = _complete(await source.exec_cmd(
                    f"git -C {shlex.quote(source_root)} rev-parse --show-object-format",
                ), "Docker source object format").strip()
                bundle = directory / "repository.bundle"
                async with _patch_temporary(source, root, "", prefix=".docker-import-", suffix=".bundle") as remote:
                    _complete(await source.exec_cmd(
                        f"git -C {shlex.quote(source_root)} bundle create - --tags HEAD > {shlex.quote(remote)}",
                        timeout=900,
                    ), "Docker source Git bundle")
                    await _copy_to_host(source, remote, bundle)
                local = LocalEnvironment(str(local_root))
                try:
                    git = (
                        f"GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null "
                        f"git -c core.hooksPath=/dev/null -C {shlex.quote(str(local_root))}"
                    )
                    _complete(await local.exec_cmd(
                        f"{git} init --quiet --object-format={shlex.quote(object_format)}", timeout=900,
                    ), "Docker private repository initialization")
                    shallow = _complete(await source.exec_cmd(
                        f"git -C {shlex.quote(source_root)} rev-parse --is-shallow-repository",
                    ), "Docker source shallow history").strip()
                    if shallow == "true":
                        shallow_path = _complete(await source.exec_cmd(
                            f"git -C {shlex.quote(source_root)} rev-parse --path-format=absolute --git-path shallow",
                        ), "Docker source shallow boundary").removesuffix("\n")
                        await _copy_to_host(source, shallow_path, local_root / ".git" / "shallow")
                    command = (
                        f"{git} fetch --quiet --tags {shlex.quote(str(bundle))} HEAD && "
                        f"{git} reset --mixed --quiet FETCH_HEAD"
                    )
                    _complete(await local.exec_cmd(command, timeout=900), "Docker private repository metadata")
                finally:
                    await local.cleanup()
                bundle.unlink()
        else:
            await await_owned_operation(asyncio.to_thread(_remove_marker, contents), propagate_cancellation=True)
            local = LocalEnvironment(str(contents))
            try:
                git = (
                    "GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null "
                    f"git -c core.hooksPath=/dev/null -C {shlex.quote(str(contents))}"
                )
                _complete(await local.exec_cmd(
                    f"{git} init --quiet && {git} add --force --all && "
                    f"{git} -c user.name='OpenCollab Agent' -c user.email=agent@opencollab.invalid "
                    "commit --quiet --allow-empty --no-verify -m 'Initial workspace'",
                    timeout=900,
                ), "Docker directory snapshot")
            finally:
                await local.cleanup()
        yield contents, prefix
    finally:
        await await_owned_operation(
            asyncio.to_thread(shutil.rmtree, directory), propagate_cancellation=True,
        )


async def _import_snapshot(source: Any, target: DockerEnvironment | VMEnvironment) -> str:
    async with _snapshot_contents(source) as (contents, prefix):
        if isinstance(target, VMEnvironment):
            await target.copy_from_host(contents, target.workspace, directory=True)
            return posixpath.normpath(posixpath.join(target.workspace, prefix))
        container = await target.setup()
        _complete(await target.exec_cmd(f"mkdir -p -- {shlex.quote(target.workspace)}"), "Docker workspace directory")
        copied = await target._docker("cp", "--", str(contents) + "/.", f"{container}:{target.workspace}", timeout=900)
        if copied.returncode or copied.stdout_dropped_bytes or copied.stderr_dropped_bytes:
            raise OSError("Docker isolated workspace import failed")
        identity = _complete(await target.exec_cmd("id -u; id -g"), "Docker workspace user").splitlines()
        if len(identity) != 2 or any(not item.isdecimal() for item in identity):
            raise RuntimeError("Docker workspace user identity is incomplete")
        # Docker's archive transport can retain host IDs. Normalize the private
        # copy before Git checks ownership, including for images with USER set.
        ownership = await target._docker(
            "exec", "--user", "0", "--", container, "chown", "-Rh", ":".join(identity), "--", target.workspace,
            timeout=900,
        )
        if ownership.returncode or ownership.stdout_dropped_bytes or ownership.stderr_dropped_bytes:
            raise OSError("Docker isolated workspace ownership could not be assigned")
        return posixpath.normpath(posixpath.join(target.workspace, prefix))
