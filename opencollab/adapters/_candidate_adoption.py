"""Restore physical working files after a failed candidate publication."""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from opencollab.adapters._env_base import ENV_FILE_WRITE_LIMIT_BYTES
from opencollab.adapters._git_patch_bytes import _complete, _patch_temporary
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.workflow_candidates import CandidateAdoptionRecoveryError

_RECOVER_PATHS = r'''
present() { [ -e "$1" ] || [ -L "$1" ]; }
directory() { [ -d "$1" ] && [ ! -L "$1" ]; }
mode() { stat -c %a -- "$1" 2>/dev/null || stat -f %Lp -- "$1"; }

same_entry() {
    if [ -L "$1" ]; then
        [ -L "$2" ] || return 1
        left=$(readlink "$1" && printf .) && right=$(readlink "$2" && printf .) &&
            [ "$left" = "$right" ]
    elif [ -f "$1" ]; then
        [ ! -L "$2" ] && [ -f "$2" ] || return 1
        left=$(mode "$1") && right=$(mode "$2") && [ "$left" = "$right" ] && cmp -s -- "$1" "$2"
    else
        ! present "$1" && ! present "$2"
    fi
}

conflict() { printf 'concurrent change at %s\n' "$1" >&2; return 2; }

check_parents() {
    if [ "$1" = "$workspace" ]; then directory "$workspace" || conflict "$workspace"; return; fi
    parent=${1%/*}
    while [ "$parent" != "$workspace" ] && [ -n "$parent" ]; do
        directory "$parent" || { conflict "$parent"; return; }
        parent=${parent%/*}
    done
    directory "$workspace" || conflict "$workspace"
}

check_candidate_descendants() {
    prefix=${1#"$workspace_prefix"}; shift
    for path; do
        case "$path" in
            "$prefix"/*)
                target=$workspace_prefix$path
                if check_parents "$target" 2>/dev/null && present "$target"; then
                    conflict "$target"
                    return
                fi
                ;;
        esac
    done
}

prepare_parent() {
    if [ "$1" != "$workspace" ]; then check_parents "$1" || return $?; fi
    if ! present "$1"; then
        if directory "$5"; then conflict "$1"; return; fi
        mkdir -m 700 -- "$1" && printf '700 %s\n' "$2" > "$3"
    elif ! directory "$1"; then
        conflict "$1"
    else
        observed=$(mode "$1") || return 1
        restore=$observed
        if directory "$5"; then
            projected=$(mode "$5") || return 1
            if [ "$observed" = "$projected" ]; then restore=$2; fi
        fi
        temporary=$observed
        if [ "$4" = write ] && [ ! -w "$1" ] && [ -O "$1" ]; then
            chmod -- u+w "$1" && temporary=$(mode "$1") || return 1
        fi
        if [ "$temporary" != "$restore" ]; then printf '%s %s\n' "$temporary" "$restore" > "$3"; fi
    fi
}

reset_parent_mode() {
    if [ -f "$2" ]; then
        check_parents "$1" && directory "$1" || { conflict "$1"; return; }
        read -r temporary observed < "$2" || return 1
        current=$(mode "$1") || return 1
        if [ "$current" = "$temporary" ] && [ "$current" != "$observed" ]; then
            chmod -- "$observed" "$1"
        fi
    fi
}

restore_saved() {
    if ! present "$1"; then return 0; fi
    if present "$2"; then conflict "$2"; return; fi
    mv -n -- "$1" "$2" || return 1
    if present "$1"; then conflict "$2"; fi
}

park_candidate_entry() {
    if ! present "$1"; then return 0; fi
    parked=$(mktemp -d "$recovery/observed-XXXXXXXX") || return 1
    printf '%s' "$1" > "$parked/path" && mv -- "$1" "$parked/entry" || return 1
    if ! same_entry "$parked/entry" "$2"; then
        restore_saved "$parked/entry" "$1" || return $?
        conflict "$1"
    fi
}

restore_entry() (
    old=$1 expected=$2 live=$3
    if same_entry "$old" "$expected"; then
        # An unchanged replay entry cannot explain a changed patch target.
        if candidate_path "$live" && ! same_entry "$old" "$live"; then conflict "$live"; return; fi
        if directory "$live"; then candidate_descendants "$live"; return; fi
        return 0
    fi
    if directory "$live"; then
        if ! directory "$old" && ! directory "$expected"; then
            conflict "$live"
            return
        fi
        result=0
        if directory "$expected"; then
            for child in "$expected"/* "$expected"/.[!.]* "$expected"/..?*; do
                present "$child" || continue
                name=${child##*/}
                if ! directory "$old" || ! present "$old/$name"; then
                    restore_entry "$absent" "$child" "$live/$name" || result=$?
                fi
            done
        fi
        if directory "$old"; then
            for child in "$old"/* "$old"/.[!.]* "$old"/..?*; do
                present "$child" || continue
                name=${child##*/}
                after=$absent
                if directory "$expected"; then after=$expected/$name; fi
                restore_entry "$child" "$after" "$live/$name" || result=$?
            done
        fi
        [ "$result" -eq 0 ] || return "$result"
        if ! directory "$old"; then
            for child in "$live"/* "$live"/.[!.]* "$live"/..?*; do
                present "$child" || continue
                if present "$old"; then conflict "$live"; fi
                return
            done
            rmdir -- "$live" || return 1
            restore_saved "$old" "$live"
        fi
        return
    fi
    if same_entry "$live" "$old"; then return 0; fi
    if same_entry "$live" "$expected"; then
        park_candidate_entry "$live" "$expected" || return $?
        restore_saved "$old" "$live"
    else
        conflict "$live"
    fi
)
'''


@asynccontextmanager
async def _adoption_command(
    environment: Any, workspace: str, command: str, *, repository: bool,
) -> AsyncIterator[str]:
    size = len(command.encode("utf-8", errors="surrogateescape"))
    if size <= 32_768:
        yield command
        return
    resources = AsyncExitStack()

    async def stage(content: str) -> str:
        return await resources.enter_async_context(_patch_temporary(
            environment, workspace, content, prefix=".candidate-command-", suffix=".sh",
            cleanup_best_effort=True, repository=repository,
        ))

    try:
        content = command + "\n"
        if size < ENV_FILE_WRITE_LIMIT_BYTES:
            script = await stage(content)
        else:
            # Keep each transfer within the file and bridge limits. Assemble
            # the complete script before it can change the workspace.
            chunk_size = ENV_FILE_WRITE_LIMIT_BYTES // 8
            script = await stage("")
            for start in range(0, len(content), chunk_size):
                fragment = await stage(content[start:start + chunk_size])
                _complete(await environment.exec_cmd(
                    f"cat -- {shlex.quote(fragment)} >> {shlex.quote(script)}", timeout=120,
                ), "candidate command staging")
        yield f"sh {shlex.quote(script)}"
    finally:
        try:
            await await_owned_operation(resources.aclose())
        except Exception:
            pass


@asynccontextmanager
async def _adoption_transaction(
    environment: Any, workspace: str, paths: Sequence[str], *, repository: bool,
    replay: Callable[[str, str | None, str | None], Awaitable[None]],
    attributes_file: str | None = None,
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
    git_directory = None
    storage = '"${TMPDIR:-/tmp}"'
    if repository:
        git_directory = _complete(await environment.exec_cmd(
            f"git -C {shlex.quote(workspace)} rev-parse --absolute-git-dir", timeout=30,
        ), "candidate recovery storage").removesuffix("\n")
        storage = shlex.quote(git_directory)
    backup = None
    keep_backup = False

    async def allocate_backup() -> None:
        nonlocal backup
        backup = _complete(await environment.exec_cmd(
            f'mktemp -d {storage}/candidate-recovery-XXXXXXXX', timeout=30,
        ), "candidate recovery storage").removesuffix("\n")

    try:
        await await_owned_operation(allocate_backup(), propagate_cancellation=True)
        assert backup is not None
        staged = posixpath.join(backup, "replay")
        stage_copies = [f"mkdir -m 700 -- {shlex.quote(staged)} || exit"]
        stage_modes = []
        copies = []
        restores = []
        recreate_directories = []
        restore_directory_modes = []
        restore_parents = {posixpath.dirname(path) or "." for path in roots}
        saved_attributes_file = None
        if attributes_file is not None:
            saved_attributes_file = posixpath.join(backup, "configured-attributes")
            source_attributes = shlex.quote(attributes_file)
            copies.append(
                f"if [ -f {source_attributes} ] && [ -r {source_attributes} ]; then "
                f"cat -- {source_attributes} > {shlex.quote(saved_attributes_file)} || exit; fi"
            )
        saved_roots = set(roots)
        # Git can prune empty ancestors. Make them writable until the saved files return.
        for index, (path, mode) in enumerate(directory_modes):
            source = shlex.quote(posixpath.normpath(posixpath.join(workspace, path)))
            stage_path = shlex.quote(posixpath.normpath(posixpath.join(staged, path)))
            stage_copies.append(f"mkdir -p -- {stage_path} || exit")
            stage_modes.append(f"chmod -- {mode} {stage_path} || exit")
            metadata_file = shlex.quote(posixpath.join(backup, f"directory-{index}.mode"))
            temporary_mode = shlex.quote(posixpath.join(backup, f"directory-{index}.temporary"))
            copies.append(f"printf '%s\\0%s\\0' {source} {mode} > {metadata_file} || exit")
            # Attribute files inside affected roots are already in the file snapshot.
            attributes_path = posixpath.normpath(posixpath.join(path, ".gitattributes"))
            if repository and attributes_path not in saved_roots:
                source_attributes = shlex.quote(posixpath.join(workspace, attributes_path))
                saved_attributes = shlex.quote(posixpath.join(backup, f"directory-{index}.attributes"))
                staged_attributes = shlex.quote(posixpath.join(staged, attributes_path))
                copies.append(
                    f"if [ -f {source_attributes} ] && [ ! -L {source_attributes} ] "
                    f"&& [ -r {source_attributes} ]; then "
                    f"cat -- {source_attributes} > {saved_attributes} || exit; fi"
                )
                stage_copies.append(
                    f"if [ -f {saved_attributes} ]; then "
                    f"cat -- {saved_attributes} > {staged_attributes} || exit; fi"
                )
            permission = "write" if path in restore_parents else "read"
            recreate_directories.append(
                f"prepare_parent {source} {mode} {temporary_mode} {permission} {stage_path} || failed=$?"
            )
            restore_directory_modes.append(
                f"reset_parent_mode {source} {temporary_mode} || failed=$?"
            )
        for index, path in enumerate(roots):
            source = shlex.quote(posixpath.join(workspace, path))
            saved = shlex.quote(posixpath.join(backup, str(index)))
            expected = shlex.quote(posixpath.join(staged, path))
            copies.append(
                f"printf '%s' {source} > {saved}.path || exit; "
                f"if [ -e {source} ] || [ -L {source} ]; then "
                f"cp -pPR -- {source} {saved} || exit; fi"
            )
            stage_copies.append(
                f"if present {saved}; then cp -pPR -- {saved} {expected} || exit; fi"
            )
            restores.append(
                f"{{ check_parents {source} && restore_entry {saved} {expected} {source}; }} || failed=$?"
            )
        replay_script = 'present() { [ -e "$1" ] || [ -L "$1" ]; }; ' + "; ".join((
            *stage_copies, *reversed(stage_modes),
        ))
        targets = [shlex.quote(path) for path in paths]
        target_test = f'case "${{1#"$workspace_prefix"}}" in {"|".join(targets)}) return 0;; esac; ' if targets else ""
        recovery_script = (
            f"failed=0; workspace={shlex.quote(workspace)}; "
            f"recovery={shlex.quote(backup)}; "
            f"absent={shlex.quote(posixpath.join(backup, 'absent'))};\n"
            'workspace_prefix="${workspace%/}/";\n'
            f"candidate_path() {{ {target_test}return 1; }}\n"
            f"candidate_descendants() {{ check_candidate_descendants \"$1\" {' '.join(targets)}; }}\n"
        ) + _RECOVER_PATHS + "; ".join((
            *recreate_directories, *restores, *reversed(restore_directory_modes),
        )) + '; exit "$failed"'
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
                async def recover() -> None:
                    async with _adoption_command(
                        environment, workspace, replay_script, repository=repository,
                    ) as command:
                        _complete(await environment.exec_cmd(command, timeout=900), "candidate recovery replay copy")
                    await replay(staged, git_directory, saved_attributes_file)
                    _complete(await environment.exec_cmd(recovery_command, timeout=900), "candidate adoption recovery")

                try:
                    await await_owned_operation(recover())
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
        if backup is not None and not keep_backup:
            try:
                quoted = shlex.quote(backup)
                await await_owned_operation(environment.exec_cmd(
                    f"rm -rf -- {quoted} || "
                    f"(find {quoted} -type d ! -perm -u+w -exec chmod -- u+w {{}} + && rm -rf -- {quoted})",
                    timeout=120,
                ))
            except Exception:
                pass
