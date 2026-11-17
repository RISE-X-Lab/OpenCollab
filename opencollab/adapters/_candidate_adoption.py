"""Restore physical working files after a failed candidate publication."""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from opencollab.adapters._git_patch_bytes import _complete, _patch_temporary
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.workflow_candidates import CandidateAdoptionRecoveryError


@asynccontextmanager
async def _adoption_command(
    environment: Any, workspace: str, command: str, *, repository: bool,
) -> AsyncIterator[str]:
    if len(command.encode("utf-8", errors="surrogateescape")) <= 32_768:
        yield command
        return
    resources = AsyncExitStack()
    try:
        script = await resources.enter_async_context(_patch_temporary(
            environment, workspace, command + "\n", prefix=".candidate-command-", suffix=".sh",
            cleanup_best_effort=True, repository=repository,
        ))
        yield f"sh {shlex.quote(script)}"
    finally:
        try:
            await await_owned_operation(resources.aclose())
        except Exception:
            pass


@asynccontextmanager
async def _adoption_transaction(
    environment: Any, workspace: str, paths: Sequence[str], *, repository: bool,
) -> AsyncIterator[None]:
    for path in paths:
        if (
            not path or "\0" in path or posixpath.normpath(path) != path or posixpath.isabs(path)
            or path in {".", ".."} or path.startswith("../")
            or any(part.casefold() == ".git" for part in path.split("/"))
        ):
            raise RuntimeError(f"invalid path in candidate patch: {path!r}")
    # Own a missing or non-directory ancestor too, so rollback also reverses
    # directory creation and file/directory replacements without following links.
    inspection = (
        f"cd -- {shlex.quote(workspace)} && "
        f"for path in {shlex.join(paths)}; do root=$path; parent=${{path%/*}}; "
        'while [ "$parent" != "$path" ]; do '
        'if [ ! -d "$parent" ] || [ -L "$parent" ]; then root=$parent; fi; '
        'path=$parent; parent=${path%/*}; done; printf "%s\\0" "$root"; done'
    )
    async with _adoption_command(environment, workspace, inspection, repository=repository) as command:
        inspected = _complete(await environment.exec_cmd(command, timeout=120), "candidate adoption path inspection")
    roots: list[str] = []
    for path in sorted(set(inspected.split("\0")) - {""}):
        if not any(path.startswith(root + "/") for root in roots):
            roots.append(path)
    ancestors = {"."}
    for path in roots:
        parent = posixpath.dirname(path)
        while parent:
            ancestors.add(parent)
            parent = posixpath.dirname(parent)
    directory_modes: list[tuple[str, str]] = []
    if ancestors:
        inspection = (
            f"cd -- {shlex.quote(workspace)} && "
            f"for path in {shlex.join(sorted(ancestors, key=lambda path: (path.count('/'), path)))}; do "
            'if [ -d "$path" ] && [ ! -L "$path" ]; then '
            'mode=$(stat -c %a -- "$path" 2>/dev/null) || '
            'mode=$(stat -f %Lp -- "$path") || exit; '
            'printf "%s\\0%s\\0" "$path" "$mode"; fi; done'
        )
        async with _adoption_command(environment, workspace, inspection, repository=repository) as command:
            metadata = _complete(await environment.exec_cmd(
                command, timeout=120,
            ), "candidate ancestor directory inspection").split("\0")
        for path, mode in zip(metadata[::2], metadata[1::2]):
            if not mode or len(mode) > 4 or any(digit not in "01234567" for digit in mode):
                raise RuntimeError("candidate ancestor directory mode is invalid")
            directory_modes.append((path, mode))
    storage = '"${TMPDIR:-/tmp}"'
    if repository:
        storage = shlex.quote(_complete(await environment.exec_cmd(
            f"git -C {shlex.quote(workspace)} rev-parse --absolute-git-dir", timeout=30,
        ), "candidate recovery storage").removesuffix("\n"))
    backup = _complete(await environment.exec_cmd(
        f'mktemp -d {storage}/candidate-recovery-XXXXXXXX', timeout=30,
    ), "candidate recovery storage").removesuffix("\n")
    backup_quoted = shlex.quote(backup)
    keep_backup = False
    try:
        copies = []
        restores = []
        recreate_directories = []
        restore_directory_modes = []
        restore_parents = {posixpath.dirname(path) or "." for path in roots}
        # Git prunes empty ancestors after deleting their last leaf. Recreate
        # those directories with write access until their saved leaves return.
        for index, (path, mode) in enumerate(directory_modes):
            source = shlex.quote(posixpath.join(workspace, path))
            metadata_file = shlex.quote(posixpath.join(backup, f"directory-{index}.mode"))
            copies.append(f"printf '%s\\0%s\\0' {source} {mode} > {metadata_file} || exit")
            recreate_directories.append(
                f"if [ ! -e {source} ] && [ ! -L {source} ]; then "
                f"mkdir -m 700 -- {source} || failed=1; "
                f"elif [ ! -d {source} ] || [ -L {source} ]; then failed=1; fi"
            )
            if path in restore_parents:
                recreate_directories.append(
                    f"if [ -d {source} ] && [ ! -L {source} ] && "
                    f"[ ! -w {source} ] && [ -O {source} ]; then "
                    f"chmod -- u+w {source} || failed=1; fi"
                )
            restore_directory_modes.append(
                f"if [ -d {source} ] && [ ! -L {source} ] && "
                f"{{ current=$(stat -c %a -- {source} 2>/dev/null) || "
                f"current=$(stat -f %Lp -- {source}); }}; then "
                f'[ "$current" = {mode} ] || chmod -- {mode} {source} || failed=1; '
                "else failed=1; fi"
            )
        for index, path in enumerate(roots):
            source = shlex.quote(posixpath.join(workspace, path))
            saved = shlex.quote(posixpath.join(backup, str(index)))
            copies.append(
                f"printf '%s' {source} > {saved}.path || exit; "
                f"if [ -e {source} ] || [ -L {source} ]; then "
                f"cp -pPR -- {source} {saved} || exit; fi"
            )
            restores.append(
                f"if [ -e {saved} ] || [ -L {saved} ]; then "
                f"if rm -rf -- {source} && mv -- {saved} {source} "
                f"&& {{ [ -e {source} ] || [ -L {source} ]; }}; then :; else failed=1; fi; "
                f"elif rm -rf -- {source} && [ ! -e {source} ] && [ ! -L {source} ]; "
                "then :; else failed=1; fi"
            )
        recovery_script = "failed=0; " + "; ".join((
            *recreate_directories, *restores, *reversed(restore_directory_modes),
        )) + '; [ "$failed" -eq 0 ]'
        async with AsyncExitStack() as commands:
            snapshot_command = await commands.enter_async_context(_adoption_command(
                environment, workspace, "; ".join(copies), repository=repository,
            ))
            # Store long recovery commands before publishing any source changes.
            recovery_command = await commands.enter_async_context(_adoption_command(
                environment, workspace, recovery_script, repository=repository,
            ))
            _complete(await environment.exec_cmd(snapshot_command, timeout=900), "candidate recovery snapshot")
            try:
                yield
            except BaseException as failure:
                try:
                    _complete(await await_owned_operation(environment.exec_cmd(
                        recovery_command, timeout=900,
                    )), "candidate adoption recovery")
                except BaseException as recovery:
                    keep_backup = True
                    revoke = getattr(environment, "revoke", None)
                    if callable(revoke):
                        revoke()
                    raise CandidateAdoptionRecoveryError(
                        f"candidate adoption failed ({failure}); recovery failed ({recovery}); "
                        f"original files retained in {backup}"
                    ) from recovery
                raise
    finally:
        if not keep_backup:
            try:
                await await_owned_operation(environment.exec_cmd(
                    f"rm -rf -- {backup_quoted}", timeout=120,
                ))
            except Exception:
                pass
