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
        left=$(mode "$1") || return 1
        right=${3:-}
        if [ -z "$right" ]; then right=$(mode "$2") || return 1; fi
        [ "$left" = "$right" ] || return 1
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

_FILESYSTEM = r'''
filesystem() { stat -c '%d:%m' -- "$1" 2>/dev/null || stat -f %d -- "$1"; }
'''

_RECOVERY_STORES = _FILESYSTEM + r'''
store_for() (
    parent=${1%/*}
    filesystem_id=$(filesystem "$parent") && primary_id=$(filesystem "$recovery") || exit
    if present "$1"; then
        entry_id=$(filesystem "$1") || exit
        if [ "$entry_id" != "$filesystem_id" ]; then
            printf 'cannot hold the mounted entry %s\n' "$1" >&2
            exit 1
        fi
    fi
    if [ "$filesystem_id" = "$primary_id" ]; then printf '%s' "$recovery"; exit; fi

    # Prefer the mount root so Git can remove empty directories below it.
    anchor=
    while directory "$parent"; do
        [ -w "$parent" ] && anchor=$parent
        [ "$parent" != "$workspace" ] && [ "$parent" != / ] || break
        parent=${parent%/*}
        parent_id=$(filesystem "${parent:-/}") || exit
        [ "$parent_id" = "$filesystem_id" ] || break
    done
    if [ -z "$anchor" ]; then
        printf 'no writable recovery directory on the filesystem of %s\n' "$1" >&2
        exit 1
    fi
    for location in "$recovery"/volumes/*; do
        [ -f "$location" ] || continue
        local_store=$(cat -- "$location" && printf .) || exit
        local_store=${local_store%.}
        [ "${local_store%/*}" = "$anchor" ] || continue
        directory "$local_store" && [ "$(filesystem "$local_store")" = "$filesystem_id" ] || exit 1
        printf '%s' "$local_store"
        exit
    done
    mkdir -p -m 700 -- "$recovery/volumes" || exit
    location=$(mktemp "$recovery/volumes/volume-XXXXXXXX") || exit
    local_store=$(mktemp -d "$anchor/.candidate-recovery-XXXXXXXX") || exit
    printf '%s' "$local_store" > "$location" || { rmdir -- "$local_store"; exit 1; }
    printf '%s' "$local_store"
)

each_store() {
    for storage_record in "$recovery"/volumes/*; do
        [ -f "$storage_record" ] || continue
        storage_path=$(cat -- "$storage_record" && printf .) || return 1
        storage_path=${storage_path%.}
        [ -n "$storage_path" ] || continue
        "$@" "$storage_path" || return $?
    done
    "$@" "$recovery"
}
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

_HOLD_ORIGINALS = _COMPARE_ENTRIES + _CHECK_PARENTS + _RECOVERY_STORES + r'''
hold_original() {
    if [ "$1" -ef "$2" ] || ln -P -- "$1" "$2" 2>/dev/null; then return 0; fi
    store=$(store_for "$1") || return 1
    if [ "$store" != "$recovery" ]; then
        mkdir -p -m 700 -- "$store/original-inodes" || return 1
        held="$store/original-inodes/${2##*/}"
        printf '%s' "$1" > "$held.path" && rm -- "$2.path" || return 1
        if ln -P -- "$1" "$held" 2>/dev/null; then return 0; fi
    fi
    mkdir -p -m 700 -- "$store/replacements" &&
        cp -pP -- "$1" "$store/replacements/${2##*/}" || return 1
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

_ACTIVATE_ORIGINALS = _COMPARE_ENTRIES + _CHECK_PARENTS + _RESTORE_SAVED + _RECOVERY_STORES + r'''
activate_store() {
    for replacement in "$1"/replacements/*; do
        [ -f "$replacement" ] || continue
        entry=$1/original-inodes/${replacement##*/}
        live=$(cat -- "$entry.path" && printf .) || return 1
        live=${live%.}
        check_parents "$live" && same_entry "$replacement" "$live" || { conflict "$live"; return; }
        restore_saved "$live" "$entry" || return $?
        if ! same_entry "$replacement" "$entry"; then
            restore_saved "$entry" "$live" || return $?
            conflict "$live"
            return
        fi
        restore_saved "$replacement" "$live" || return $?
    done
}
each_store activate_store
'''

_UNDO_INCOMPLETE_ACTIVATION = _COMPARE_ENTRIES + _CHECK_PARENTS + _RESTORE_SAVED + _RECOVERY_STORES + r'''
undo_store() {
    for replacement in "$1"/replacements/*; do
        entry=$1/original-inodes/${replacement##*/}
        [ -f "$replacement" ] && [ -f "$entry" ] || continue
        live=$(cat -- "$entry.path" && printf .) || return 1
        live=${live%.}
        check_parents "$live" && restore_saved "$entry" "$live" || return $?
    done
}
each_store undo_store || exit
'''

_DETACHED_FILES = r'''
detached=0
parked=0
collect_detached() {
    for record in "$1"/original-inodes/*.path; do
        [ -f "$record" ] || continue
        path=$(cat -- "$record" && printf .) || return 1
        path=${path%.}
        entry=${record%.path}
        if ! present "$entry"; then
            rm -- "$record" || return 1
        elif check_parents "$path" 2>/dev/null && [ ! -L "$path" ] && [ "$path" -ef "$entry" ]; then
            rm -- "$record" "$entry" || return 1
        else
            detached=1
        fi
    done
    for entry in "$1"/observed-*/entry; do
        if [ -f "$entry" ] && [ ! -L "$entry" ]; then parked=1; break; fi
    done
}
each_store collect_detached || exit
if [ "$parked" -eq 1 ]; then printf 'parked\n'; fi
if [ "$detached" -eq 1 ]; then printf 'original-detached\n'; fi
'''

_RECOVERY_PARENT = _FILESYSTEM + r'''
writable_directory() { [ -d "$1" ] && [ -w "$1" ] && [ -x "$1" ]; }

recovery_parent() {
    worktree_device=$(filesystem .) && storage_device=$(filesystem "$1/") || return 1
    if [ "$worktree_device" = "$storage_device" ] && writable_directory "$1"; then
        printf '%s\n' "$1"
        return
    fi
    workspace=$(pwd -P && printf .) || return 1
    workspace=${workspace%??}
    parent=$workspace
    while [ "$parent" != / ]; do
        parent=${parent%/*}
        parent=${parent:-/}
        parent_device=$(filesystem "$parent") || return 1
        [ "$worktree_device" = "$parent_device" ] || break
        if writable_directory "$parent"; then
            printf '%s\n' "$parent"
            return
        fi
    done
    if [ -n "${2:-}" ]; then
        temporary=$(cd -P -- "${TMPDIR:-/tmp}" >/dev/null 2>&1 && pwd -P && printf .) || temporary=
        temporary=${temporary%??}
        case "$temporary/" in
            "${workspace%/}/"*|"${2%/}/"*) ;;
            *)
                if writable_directory "$temporary" && [ "$(filesystem "$temporary")" = "$worktree_device" ]; then
                    printf '%s\n' "$temporary"
                    return
                fi ;;
        esac
    fi
    if writable_directory "$1"; then
        printf '%s\n' "$1"
    else
        printf '%s\n' "${2:-$1}"
    fi
}
'''

_RECOVERY_GIT_DIRECTORIES = r'''
private=$(git rev-parse --absolute-git-dir && printf .) || exit
private=${private%??}
common=$(git rev-parse --git-common-dir && printf .) || exit
common=${common%??}
'''


async def _mounted_recovery_paths(environment: Any, workspace: str) -> tuple[str, ...]:
    # The volume receipts live beside the primary backup, including for linked worktrees.
    command = f"cd -- {shlex.quote(workspace)} || exit\n" + _RECOVERY_PARENT + _RECOVERY_GIT_DIRECTORIES + r'''
workspace=$(pwd -P && printf .) || exit
workspace=${workspace%??}
prefix=${workspace%/}/
primary=$(recovery_parent "$common" "$private" && printf .) || exit
primary=${primary%??}
if [ "$primary" -ef "$common" ] || [ "$primary" -ef "$private" ]; then primary=; fi
if [ "$private" -ef "$common" ]; then private=; fi
for parent in "$common" "$private" "$primary"; do
    [ -n "$parent" ] || continue
    for backup in "$parent"/candidate-recovery-????????; do
        [ -d "$backup" ] && [ ! -L "$backup" ] && [ -O "$backup" ] || continue
        volumes=$backup/volumes
        [ -d "$volumes" ] && [ ! -L "$volumes" ] && [ -O "$volumes" ] || continue
        for receipt in "$volumes"/volume-????????; do
            [ -f "$receipt" ] && [ ! -L "$receipt" ] && [ -O "$receipt" ] || continue
            store=$(cat -- "$receipt" && printf .) || continue
            store=${store%.}
            case "$store" in "$prefix"*) ;; *) continue;; esac
            case "${store##*/}" in .candidate-recovery-????????) ;; *) continue;; esac
            [ -d "$store" ] && [ ! -L "$store" ] && [ -O "$store" ] || continue
            anchor=${store%/*}
            top=$(cd -P -- "${anchor:-/}" && [ "$PWD" = "${anchor:-/}" ] &&
                GIT_DISCOVERY_ACROSS_FILESYSTEM=1 git rev-parse --show-toplevel 2>/dev/null &&
                printf .) || continue
            [ "${top%??}" = "$workspace" ] || continue
            printf '%s\0' "${store#"$prefix"}"
        done
    done
done
'''
    result = _complete(await environment.exec_cmd(command, timeout=30), "mounted candidate recovery locations")
    return tuple(dict.fromkeys(path for path in result.split("\0") if path))


_RECOVER_PATHS = _COMPARE_ENTRIES + _CHECK_PARENTS + _RESTORE_SAVED + _RECOVERY_STORES + r'''

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
    parked=$(mktemp -d "$3/observed-XXXXXXXX") || return 1
    if [ -n "$4" ]; then printf '%s\n' "$4" > "$parked/mode" || return 1; fi
    printf '%s' "$1" > "$parked/path" && mv -- "$1" "$parked/entry" || return 1
    if ! same_entry "$parked/entry" "$2" "$4"; then
        restore_saved "$parked/entry" "$1" || return $?
        conflict "$1"
    fi
}

project_file_mode() (
    source=$(mktemp "$recovery/mode-XXXXXXXX") || exit
    expected_mode=$(mode "$1") && chmod -- "$expected_mode" "$source" || exit
    sample=$(mktemp -d "$2/mode-XXXXXXXX") || exit
    cp -pP -- "$source" "$sample/entry" && actual_mode=$(mode "$sample/entry") || exit
    rm -- "$source" && rm -rf -- "$sample" || exit
    printf '%s' "$actual_mode"
)

restore_preimage() {
    if ! present "$1"; then return 0; fi
    store=${3:-}
    if [ -z "$store" ]; then store=$(store_for "$2") || return 1; fi
    if [ "$store" = "$recovery" ]; then restore_saved "$1" "$2"; return; fi
    install=$(mktemp -d "$store/restore-XXXXXXXX") || return 1
    cp -pPR -- "$1" "$install/entry" && restore_saved "$install/entry" "$2"
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
            restore_preimage "$old" "$live"
        fi
        return
    fi
    if same_entry "$live" "$old"; then return 0; fi
    projected=
    if same_entry "$live" "$expected"; then
        store=$(store_for "$live") || return 1
    else
        if [ ! -f "$live" ] || [ -L "$live" ] || [ ! -f "$expected" ] || [ -L "$expected" ]; then
            conflict "$live"
            return
        fi
        store=$(store_for "$live") || return 1
        if [ "$store" = "$recovery" ]; then conflict "$live"; return; fi
        # A replay on the Git filesystem can represent modes unavailable on the live mount.
        projected=$(project_file_mode "$expected" "$store") || return 1
        same_entry "$live" "$expected" "$projected" || { conflict "$live"; return; }
    fi
    park_candidate_entry "$live" "$expected" "$store" "$projected" || return $?
    restore_preimage "$old" "$live" "$store"
)
'''

_RECHECK_PARKED = _COMPARE_ENTRIES + r'''
verify_parked() {
    for parked in "$1"/observed-*; do
        [ -d "$parked" ] || continue
        path=$(cat -- "$parked/path" && printf .) || return 1
        path=${path%.}
        projected=
        if [ -f "$parked/mode" ]; then read -r projected < "$parked/mode" || return 1; fi
        same_entry "$parked/entry" "$staged/${path#"$workspace_prefix"}" "$projected" || { conflict "$path"; return; }
    done
}
each_store verify_parked || exit
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
        # Git removes private metadata with the linked worktree.
        directory_script += _RECOVERY_GIT_DIRECTORIES + r'''
printf '%s\0' "$private" || exit
recovery_parent "$common" "$private"
'''
    else:
        directory_script += 'recovery_parent "${TMPDIR:-/tmp}"'
    recovery_parent = _complete(await environment.exec_cmd(directory_script, timeout=30), "candidate recovery storage")
    if repository:
        git_directory, recovery_parent = recovery_parent.split("\0")
    storage = shlex.quote(posixpath.join(workspace, recovery_parent.removesuffix("\n")))
    backup = None
    keep_backup = False

    async def allocate_backup() -> None:
        nonlocal backup
        backup = _complete(await environment.exec_cmd(
            f'mktemp -d {storage}/candidate-recovery-XXXXXXXX', timeout=30,
        ), "candidate recovery storage").removesuffix("\n")

    async def remove_backup(path: str, *, retain_detached: bool = False) -> None:
        remove_entry = (
            'remove_entry() { rm -rf -- "$1" || '
            '(find "$1" -type d ! -perm -u+w -exec chmod -- u+w {} + && rm -rf -- "$1"); }\n'
        )
        removal = (
            f"recovery={shlex.quote(path)}; staged={shlex.quote(posixpath.join(path, 'replay'))}; "
            f"workspace_prefix={shlex.quote(workspace.rstrip('/') + '/')};\n"
            + _RECOVERY_STORES
        )
        if retain_detached:
            removal += remove_entry + _RECHECK_PARKED + r'''
prune_store() {
    for entry in "$1"/*; do
        case "${entry##*/}" in original-inodes|observed-*|volumes) continue;; esac
        remove_entry "$entry" || return $?
    done
}
each_store prune_store
'''
        else:
            removal = remove_entry + removal + "each_store remove_entry"
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
            + _HOLD_ORIGINALS + "; ".join((*copies, f"hold_originals {' '.join(targets)} || exit"))
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
                await await_owned_operation(
                    environment.exec_cmd(snapshot_command, timeout=900), propagate_cancellation=True,
                ),
                "candidate recovery snapshot",
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
