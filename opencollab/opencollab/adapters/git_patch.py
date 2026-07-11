"""Build fail-closed Git patch extraction commands."""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import shlex
import sys
from collections.abc import Sequence

from opencollab.adapters.retirement_registry import RetirementSnapshot

_OID_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_RETIREMENT_VALIDATOR = r"""
import base64,json,os,stat,sys
root_path=sys.argv[1]
records=json.loads(base64.b64decode(sys.argv[2],validate=True))
flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NOFOLLOW',0)
root_fd=os.open(root_path,flags)
try:
    for record in records:
        relative=record['relative_path']
        components=relative.split('/')
        if not components or any(component in {'','.','..'} for component in components):
            raise OSError('retirement checkpoint path is invalid')
        parent_fd=os.dup(root_fd)
        try:
            for component in components[:-1]:
                child_fd=os.open(component,flags,dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd=child_fd
            parent=os.fstat(parent_fd)
            current=os.stat(components[-1],dir_fd=parent_fd,follow_symlinks=False)
            actual=(parent.st_dev,parent.st_ino,current.st_dev,current.st_ino,stat.S_IFMT(current.st_mode),current.st_size,current.st_mtime_ns,current.st_ctime_ns,current.st_nlink)
            fields=('parent_dev','parent_ino','file_dev','file_ino','mode','size',
                    'mtime_ns','ctime_ns','nlink')
            expected=tuple(record[field] for field in fields)
            if actual != expected:
                raise OSError('authenticated retirement changed during patch extraction: '+relative)
        finally:
            os.close(parent_fd)
finally:
    os.close(root_fd)
""".strip()


def _retirement_validation_command(
    working_tree: str,
    snapshots: Sequence[RetirementSnapshot],
) -> str:
    if not snapshots:
        return ":"
    payload = json.dumps(
        [snapshot.to_payload() for snapshot in snapshots],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")
    return " ".join(
        (
            shlex.quote(sys.executable),
            "-I",
            "-c",
            shlex.quote(_RETIREMENT_VALIDATOR),
            shlex.quote(working_tree),
            shlex.quote(encoded),
        )
    )


def guarded_staged_diff_command(
    *,
    base_revision: str,
    exclude_paths: Sequence[str] = (),
    registered_retirement_paths: Sequence[str] = (),
    retirement_snapshot: Sequence[RetirementSnapshot] = (),
    object_directory: str | None = None,
    working_tree: str | None = None,
    stage_checkpoint_command: str = ":",
    diff_checkpoint_command: str = ":",
) -> str:
    """Stage in isolated Git state and verify authenticated exclusions twice."""
    if not isinstance(base_revision, str) or _OID_RE.fullmatch(base_revision) is None:
        raise ValueError("patch base revision must be an exact commit object id")
    base_revision = base_revision.lower()
    if (
        not isinstance(object_directory, str)
        or not os.path.isabs(object_directory)
        or any(character in object_directory for character in ("\0", "\n", os.pathsep))
    ):
        raise ValueError("patch object directory must be one absolute trusted path")
    if (
        not isinstance(working_tree, str)
        or not os.path.isabs(working_tree)
        or "\0" in working_tree
        or "\n" in working_tree
    ):
        raise ValueError("patch working tree must be one absolute trusted path")
    if any("\0" in command or "\n" in command for command in (stage_checkpoint_command, diff_checkpoint_command)):
        raise ValueError("patch checkpoint command is invalid")

    normalized_retirements: list[str] = []
    for path in registered_retirement_paths:
        value = str(path)
        normalized = posixpath.normpath(value)
        if (
            not value
            or "\0" in value
            or value.startswith("/")
            or normalized in {".", ".."}
            or normalized.startswith("../")
            or not posixpath.basename(normalized).startswith(".opencollab-retired-")
        ):
            raise ValueError("registered retirement path is outside the reserved namespace")
        normalized_retirements.append(normalized)
    snapshot_paths = [snapshot.relative_path for snapshot in retirement_snapshot]
    if sorted(normalized_retirements) != sorted(snapshot_paths) or len(snapshot_paths) != len(set(snapshot_paths)):
        raise ValueError("registered retirement paths lack one exact authenticated snapshot")

    base = '"$base_oid"'
    resets = ""
    for path in exclude_paths:
        if not str(path).strip():
            continue
        resets += (
            "trusted_git --literal-pathspecs reset -q "
            f"{base} -- {shlex.quote(str(path))} && "
        )
    for path in normalized_retirements:
        resets += (
            "trusted_git --literal-pathspecs reset -q "
            f"{base} -- {shlex.quote(path)} && "
        )
    reserved_guard = (
        "if trusted_git diff --cached --quiet --no-ext-diff --no-textconv "
        f"{base} -- "
        "':(glob,top).opencollab-retired-*' "
        "':(glob,top)**/.opencollab-retired-*' "
        "':(glob,top).opencollab-retired-*/**' "
        "':(glob,top)**/.opencollab-retired-*/**'; then :; "
        "else reserved_rc=$?; "
        'if [ "$reserved_rc" -eq 1 ]; then '
        "echo 'unregistered or modified .opencollab-retired-* path in candidate patch' >&2; exit 125; "
        'else exit "$reserved_rc"; fi; fi; '
    )
    validate = _retirement_validation_command(working_tree, retirement_snapshot)
    object_format = "sha256" if len(base_revision) == 64 else "sha1"
    return (
        'tmpdir=$(mktemp -d) || exit 125; gitdir="$tmpdir/control.git"; idx="$tmpdir/index"; '
        'patch="$tmpdir/candidate.patch"; empty_home="$tmpdir/home"; empty_xdg="$tmpdir/xdg"; '
        'mkdir -p "$empty_home" "$empty_xdg" || exit 125; '
        "trap 'rm -rf -- \"$tmpdir\"' EXIT; "
        f"worktree={shlex.quote(working_tree)}; objects={shlex.quote(object_directory)}; "
        f"base_oid={shlex.quote(base_revision)}; cd -- \"$worktree\" || exit 125; "
        'env -i PATH=/usr/bin:/bin HOME="$empty_home" XDG_CONFIG_HOME="$empty_xdg" '
        'GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null '
        f'GIT_ATTR_NOSYSTEM=1 git init -q --bare --object-format={object_format} "$gitdir" || exit 125; '
        'trusted_git() { env -i PATH=/usr/bin:/bin HOME="$empty_home" XDG_CONFIG_HOME="$empty_xdg" '
        'GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null '
        'GIT_ATTR_NOSYSTEM=1 GIT_DIR="$gitdir" GIT_WORK_TREE="$worktree" GIT_INDEX_FILE="$idx" '
        'GIT_OBJECT_DIRECTORY="$gitdir/objects" GIT_ALTERNATE_OBJECT_DIRECTORIES="$objects" '
        'git "$@"; }; '
        f"trusted_git fsck --strict --no-reflogs --no-progress --no-dangling {base} >/dev/null && "
        f"trusted_git cat-file -e {base}^{{commit}} && "
        f"trusted_git read-tree {base} && "
        "trusted_git add -f -A -- . ':(exclude,top).git' ':(exclude,top).git/**' && "
        f"{stage_checkpoint_command} && {validate} || exit 125; "
        f"{resets}"
        f"{reserved_guard}"
        f"trusted_git diff --cached --binary --no-ext-diff --no-textconv {base} >\"$patch\" && "
        f"{diff_checkpoint_command} && {validate} || exit 125; "
        "trusted_git fsck --strict --no-reflogs --no-progress --no-dangling "
        f"{base} >/dev/null || exit 125; "
        'cat -- "$patch"'
    )


__all__ = ["guarded_staged_diff_command"]
