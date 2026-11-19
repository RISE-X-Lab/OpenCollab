"""Restore physical working files after a failed candidate publication."""

from __future__ import annotations

import logging
import posixpath
import shlex
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from opencollab.adapters._env_base import ENV_FILE_WRITE_LIMIT_BYTES
from opencollab.adapters._git_patch_bytes import _complete, _patch_temporary
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.workflow_candidates import CandidateAdoptionRecoveryError

logger = logging.getLogger(__name__)

_COMPARE_ENTRIES = r'''
present() { [ -e "$1" ] || [ -L "$1" ]; }
mode() { stat -c %a -- "$1" 2>/dev/null || stat -f %Lp -- "$1"; }

same_entry() {
    if [ -L "$1" ]; then
        [ -L "$2" ] || return 1
        left=$(readlink "$1" && printf .) && right=$(readlink "$2" && printf .) &&
            [ "$left" = "$right" ]
    elif [ -f "$1" ]; then
        [ ! -L "$2" ] && [ -f "$2" ] || return 1
        left=$(mode "$1") && right=$(mode "$2") && [ "$left" = "$right" ] || return 1
        if command -v cmp >/dev/null 2>&1; then
            cmp -s -- "$1" "$2"
        else
            git diff --no-index --quiet --no-ext-diff --no-textconv -- "$1" "$2"
        fi
    else
        ! present "$1" && ! present "$2"
    fi
}

conflict() { printf 'concurrent change at %s\n' "$1" >&2; return 2; }
'''

_CHECK_PARENTS = r'''
directory() { [ -d "$1" ] && [ ! -L "$1" ]; }

check_parents() {
    if [ "$1" = "$workspace" ]; then directory "$workspace" || conflict "$workspace"; return; fi
    parent=${1%/*}
    while [ "$parent" != "$workspace" ] && [ -n "$parent" ]; do
        directory "$parent" || { conflict "$parent"; return; }
        parent=${parent%/*}
    done
    directory "$workspace" || conflict "$workspace"
}
'''

_DEVICE = r'''
device() { stat -c %d -- "$1" 2>/dev/null || stat -f %d -- "$1"; }
'''

_LINK_ORIGINALS = """\
import os
import sys
from pathlib import Path

for record in Path(sys.argv[1]).glob("[0-9]*.path"):
    try:
        os.link(record.read_bytes(), record.with_suffix(""), follow_symlinks=False)
    except OSError:
        sys.exit(1)
"""

_HOLD_ORIGINALS = _COMPARE_ENTRIES + _CHECK_PARENTS + _DEVICE + r'''
hold_original() {
    if [ "$1" -ef "$2" ] || ln -P -- "$1" "$2" 2>/dev/null; then return 0; fi
    source_device=$(device "$1") && recovery_device=$(device "$recovery") || return 1
    if [ "$source_device" != "$recovery_device" ]; then
        printf 'cannot preserve %s across filesystems\n' "$1" >&2
        return 1
    fi
    mkdir -p -m 700 -- "$recovery/replacements" &&
        cp -pP -- "$1" "$recovery/replacements/${2##*/}" || return 1
    needs_activation=1
}

hold_originals() {
    index=0
    needs_activation=0
    mkdir -m 700 -- "$recovery/original-inodes" || return 1
    for path; do
        source=$workspace_prefix$path
        if check_parents "$source" 2>/dev/null && [ -f "$source" ] && [ ! -L "$source" ]; then
            entry=$recovery/original-inodes/$index
            printf '%s' "$source" > "$entry.path" || return 1
            if [ -z "$linker" ]; then hold_original "$source" "$entry" || return 1; fi
            index=$((index + 1))
        fi
    done
    if [ -n "$linker" ] && ! "$linker" -I -S -c "$link_script" "$recovery/original-inodes"; then
        for record in "$recovery"/original-inodes/*.path; do
            [ -f "$record" ] || continue
            source=$(cat -- "$record" && printf .) || return 1
            hold_original "${source%.}" "${record%.path}" || return 1
        done
    fi
    if [ "$needs_activation" -eq 1 ]; then printf 'activate-originals\n'; fi
}
'''

_RESTORE_SAVED = r'''
restore_saved() {
    if ! present "$1"; then return 0; fi
    if present "$2"; then conflict "$2"; return; fi
    mv -n -- "$1" "$2" || return 1
    if present "$1"; then conflict "$2"; fi
}
'''

_ACTIVATE_ORIGINALS = _COMPARE_ENTRIES + _CHECK_PARENTS + _RESTORE_SAVED + r'''
for replacement in "$recovery"/replacements/*; do
    [ -f "$replacement" ] || continue
    entry=$recovery/original-inodes/${replacement##*/}
    live=$(cat -- "$entry.path" && printf .) || exit
    live=${live%.}
    check_parents "$live" && same_entry "$replacement" "$live" || { conflict "$live"; exit; }
    restore_saved "$live" "$entry" || exit
    if ! same_entry "$replacement" "$entry"; then
        restore_saved "$entry" "$live" || exit
        conflict "$live"
        exit
    fi
    restore_saved "$replacement" "$live" || exit
done
'''

_UNDO_INCOMPLETE_ACTIVATION = _COMPARE_ENTRIES + _CHECK_PARENTS + _RESTORE_SAVED + r'''
for replacement in "$recovery"/replacements/*; do
    entry=$recovery/original-inodes/${replacement##*/}
    [ -f "$replacement" ] && [ -f "$entry" ] || continue
    live=$(cat -- "$entry.path" && printf .) || exit
    live=${live%.}
    check_parents "$live" && restore_saved "$entry" "$live" || exit
done
'''

_DETACHED_FILES = r'''
detached=0
for record in "$recovery"/original-inodes/*.path; do
    [ -f "$record" ] || continue
    path=$(cat -- "$record" && printf .) || exit
    path=${path%.}
    entry=${record%.path}
    if ! present "$entry"; then
        rm -- "$record" || exit
    elif check_parents "$path" 2>/dev/null && [ ! -L "$path" ] && [ "$path" -ef "$entry" ]; then
        rm -- "$record" "$entry" || exit
    else
        detached=1
    fi
done
for parked in "$recovery"/observed-*/entry; do
    if [ -f "$parked" ] && [ ! -L "$parked" ]; then printf 'parked\n'; break; fi
done
if [ "$detached" -eq 1 ]; then printf 'original-detached\n'; fi
'''

_RECOVERY_PARENT = _DEVICE + r'''
recovery_parent() {
    worktree_device=$(device .) && storage_device=$(device "$1/") || return 1
    if [ "$worktree_device" = "$storage_device" ]; then
        printf '%s\n' "$1"
        return
    fi
    cd -P -- .. && parent_device=$(device .) || return 1
    if [ "$worktree_device" != "$parent_device" ]; then
        printf 'no recovery directory outside the worktree on its filesystem\n' >&2
        return 1
    fi
    pwd -P
}
'''

_RECOVER_PATHS = _COMPARE_ENTRIES + _CHECK_PARENTS + _DEVICE + _RESTORE_SAVED + r'''

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

park_candidate_entry() {
    if ! present "$1"; then return 0; fi
    if [ -f "$1" ] && [ ! -L "$1" ]; then
        source_device=$(device "$1") && recovery_device=$(device "$recovery") || return 1
        if [ "$source_device" != "$recovery_device" ]; then
            printf 'cannot park %s across filesystems\n' "$1" >&2
            return 2
        fi
    fi
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

_RECHECK_PARKED = _COMPARE_ENTRIES + r'''
for parked in "$recovery"/observed-*; do
    [ -d "$parked" ] || continue
    path=$(cat -- "$parked/path" && printf .) || exit
    path=${path%.}
    same_entry "$parked/entry" "$staged/${path#"$workspace_prefix"}" || { conflict "$path"; exit; }
done
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
    directory_script = f"cd -- {shlex.quote(workspace)} || exit\n" + _RECOVERY_PARENT
    if repository:
        # Git removes private metadata with a linked worktree. A worktree on
        # another volume needs a recovery directory beside it on that volume.
        directory_script += r'''
git rev-parse --absolute-git-dir && printf '\0' || exit
common=$(git rev-parse --git-common-dir && printf .) || exit
recovery_parent "${common%??}"
'''
    else:
        directory_script += 'recovery_parent "${TMPDIR:-/tmp}"'
    recovery_parent = _complete(await environment.exec_cmd(directory_script, timeout=30), "candidate recovery storage")
    if repository:
        git_directory, recovery_parent = recovery_parent.split("\0")
        git_directory = git_directory.removesuffix("\n")
    storage = shlex.quote(posixpath.join(workspace, recovery_parent.removesuffix("\n")))
    backup = None
    keep_backup = False

    async def allocate_backup() -> None:
        nonlocal backup
        backup = _complete(await environment.exec_cmd(
            f'mktemp -d {storage}/candidate-recovery-XXXXXXXX', timeout=30,
        ), "candidate recovery storage").removesuffix("\n")

    async def remove_backup(path: str, *, retain_detached: bool = False) -> None:
        quoted = shlex.quote(path)
        remove_entry = (
            'remove_entry() { rm -rf -- "$1" || '
            '(find "$1" -type d ! -perm -u+w -exec chmod -- u+w {} + && rm -rf -- "$1"); }\n'
        )
        if retain_detached:
            removal = (
                f"recovery={quoted}; staged={shlex.quote(posixpath.join(path, 'replay'))}; "
                f"workspace_prefix={shlex.quote(workspace.rstrip('/') + '/')};\n"
                + _RECHECK_PARKED + remove_entry + r'''
for entry in "$recovery"/*; do
    case "${entry##*/}" in original-inodes|observed-*) continue;; esac
    remove_entry "$entry" || exit
done
'''
            )
        else:
            removal = remove_entry + f"remove_entry {quoted}"
        result = await environment.exec_cmd(removal, timeout=120)
        if retain_detached:
            _complete(result, "candidate recovery cleanup")

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
        # Git can unlink the original before installing the candidate.
        workspace_vars = (
            f"workspace={shlex.quote(workspace)}; recovery={shlex.quote(backup)};\n"
            'workspace_prefix="${workspace%/}/";\n'
        )
        # Batch local links to avoid launching a process for every file.
        linker = sys.executable if len(paths) >= 64 and getattr(environment, "local_filesystem", False) else ""
        snapshot_script = (
            workspace_vars + f"linker={shlex.quote(linker)};\n"
            + (f"link_script={shlex.quote(_LINK_ORIGINALS)};\n" if linker else "")
            + _HOLD_ORIGINALS + f"hold_originals {' '.join(targets)} || exit; "
            + "; ".join(copies)
        )
        target_test = f'case "${{1#"$workspace_prefix"}}" in {"|".join(targets)}) return 0;; esac; ' if targets else ""
        recovery_script = (
            f"failed=0; {workspace_vars}"
            f"absent={shlex.quote(posixpath.join(backup, 'absent'))};\n"
            f"candidate_path() {{ {target_test}return 1; }}\n"
            f"candidate_descendants() {{ check_candidate_descendants \"$1\" {' '.join(targets)}; }}\n"
        ) + _RECOVER_PATHS + "; ".join((
            *recreate_directories, *restores, *reversed(restore_directory_modes),
        )) + ';\nif [ "$failed" -ne 0 ]; then exit "$failed"; fi\n' + _DETACHED_FILES
        async with AsyncExitStack() as commands:
            snapshot_command = await commands.enter_async_context(_adoption_command(
                environment, workspace, snapshot_script, repository=repository,
            ))
            # Store long recovery commands before publishing any source changes.
            recovery_command = await commands.enter_async_context(_adoption_command(
                environment, workspace, recovery_script, repository=repository,
            ))
            snapshot = _complete(
                await environment.exec_cmd(snapshot_command, timeout=900), "candidate recovery snapshot",
            )
            publication_started = False
            try:
                if "activate-originals" in snapshot.splitlines():
                    # Filesystems without hard links keep the original by
                    # moving it aside and giving Git an equivalent working copy.
                    _complete(await environment.exec_cmd(
                        workspace_vars + _ACTIVATE_ORIGINALS, timeout=900,
                    ), "candidate recovery preparation")
                publication_started = True
                yield
            except BaseException as failure:
                async def recover() -> tuple[bool, bool]:
                    command = recovery_command
                    if publication_started:
                        async with _adoption_command(
                            environment, workspace, replay_script, repository=repository,
                        ) as copy_command:
                            _complete(await environment.exec_cmd(copy_command, timeout=900),
                                      "candidate recovery replay copy")
                        await replay(staged, git_directory, saved_attributes_file)
                    else:
                        command = workspace_vars + _UNDO_INCOMPLETE_ACTIVATION + _DETACHED_FILES

                    shell_commands = getattr(environment, "_shell_commands", None)
                    active = shell_commands.active if shell_commands is not None else 0
                    last_started = shell_commands.last_started if shell_commands is not None else None
                    receipt = _complete(await environment.exec_cmd(
                        command, timeout=900,
                    ), "candidate adoption recovery")
                    shell_overlapped = shell_commands is not None and (
                        active > 0 or last_started is not shell_commands.last_started
                    )
                    entries = receipt.splitlines()
                    parked = "parked" in entries
                    return parked or "original-detached" in entries, parked and shell_overlapped

                try:
                    detached, parked_during_shell = await await_owned_operation(recover())
                except BaseException as recovery:
                    keep_backup = True
                    revoke = getattr(environment, "revoke", None)
                    if callable(revoke):
                        revoke()
                    raise CandidateAdoptionRecoveryError(
                        f"candidate adoption failed ({failure}); recovery failed ({recovery}); "
                        f"original files retained in {backup}"
                    ) from recovery
                if detached:
                    keep_backup = True
                    if parked_during_shell:
                        # A command can still own a detached inode after its original path is restored.
                        raise CandidateAdoptionRecoveryError(
                            f"candidate adoption failed ({failure}); a concurrent shell command may have written "
                            f"to parked files retained in {backup}"
                        ) from failure
                    assert backup is not None
                    try:
                        await await_owned_operation(remove_backup(backup, retain_detached=True))
                    except BaseException as cleanup:
                        raise CandidateAdoptionRecoveryError(
                            f"candidate adoption failed ({failure}); cleanup failed ({cleanup}); "
                            f"recovery location {backup}"
                        ) from cleanup
                    detail = f"Detached candidate files retained in {backup}"
                    add_exception_note(failure, detail)
                    logger.warning("%s", detail)
                raise
    finally:
        if backup is not None and not keep_backup:
            try:
                await await_owned_operation(remove_backup(backup))
            except Exception:
                pass
