"""Build fail-closed Git patch extraction commands."""

from __future__ import annotations

import shlex
from collections.abc import Sequence


def guarded_staged_diff_command(
    *,
    base_revision: str = "HEAD",
    exclude_paths: Sequence[str] = (),
) -> str:
    """Stage through a temporary index without repository-local diff hooks."""
    if not isinstance(base_revision, str) or not base_revision.strip() or "\0" in base_revision:
        raise ValueError("patch base revision is invalid")

    git_env = (
        "GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null "
        "GIT_EXTERNAL_DIFF= GIT_NO_REPLACE_OBJECTS=1"
    )
    git_command = (
        f'{git_env} git -c safe.directory="$PWD" -c core.filemode=true'
    )
    git_index_command = f'GIT_INDEX_FILE="$idx" {git_command}'
    resets = ""
    for path in exclude_paths:
        if str(path).strip():
            resets += (
                f'{git_index_command} --literal-pathspecs reset -q '
                f"{shlex.quote(base_revision)} -- {shlex.quote(str(path))} && "
            )
    unsafe_config_guard = (
        "config_scopes=--local; "
        f'if [ "$({git_command} config --local --includes --type=bool '
        '--get extensions.worktreeConfig 2>/dev/null)" = true ]; then '
        'config_scopes="$config_scopes --worktree"; fi; '
        "for config_scope in $config_scopes; do "
        f"unsafe_config=$({git_command} config \"$config_scope\" --includes --name-only --get-regexp "
        "'^(diff\\..*\\.(command|textconv)|filter\\..*|"
        "diff\\.ignoresubmodules|"
        "core\\.(attributesfile|excludesfile|fsmonitor|sparsecheckout|sparsecheckoutcone|worktree))$' "
        "2>/dev/null); "
        'config_rc=$?; if [ "$config_rc" -eq 0 ]; then '
        "echo \"unsafe repository Git configuration: $unsafe_config\" >&2; exit 125; "
        'elif [ "$config_rc" -ne 1 ]; then exit "$config_rc"; fi; done; '
        f'info_attributes=$({git_command} rev-parse --git-path info/attributes) || exit 125; '
        'if [ -L "$info_attributes" ] || { [ -e "$info_attributes" ] && [ ! -f "$info_attributes" ]; }; then '
        "echo 'repository-local info/attributes is not a regular file' >&2; exit 125; fi; "
        'if [ -s "$info_attributes" ]; then '
        "echo 'repository-local info/attributes can alter patch evidence' >&2; exit 125; fi; "
    )
    stage_untracked = (
        f'{git_index_command} ls-files --others --exclude-per-directory=.gitignore '
        '-z > "$untracked" && '
        'if [ -s "$untracked" ]; then '
        f'{git_index_command} add -f --pathspec-from-file="$untracked" '
        '--pathspec-file-nul; fi && '
    )
    reserved_guard = (
        f'if {git_index_command} diff --no-ext-diff --no-textconv --cached --quiet '
        f"{shlex.quote(base_revision)} -- "
        "':(glob,top).opencollab-retired-*' "
        "':(glob,top)**/.opencollab-retired-*' "
        "':(glob,top).opencollab-retired-*/**' "
        "':(glob,top)**/.opencollab-retired-*/**'; then :; "
        "else reserved_rc=$?; "
        'if [ "$reserved_rc" -eq 1 ]; then '
        "echo 'unregistered or modified .opencollab-retired-* path in candidate patch' >&2; exit 125; "
        'else exit "$reserved_rc"; fi; fi; '
    )
    return (
        'idx=$(mktemp) || exit 125; '
        'untracked=$(mktemp) || { rm -f -- "$idx"; exit 125; }; '
        'trap \'rm -f -- "$idx" "$untracked"\' EXIT; '
        f"{unsafe_config_guard}"
        f"{git_index_command} read-tree {shlex.quote(base_revision)} && "
        f'{git_index_command} add -u && '
        f"{stage_untracked}"
        f"{resets}"
        f"{reserved_guard}"
        f'{git_index_command} diff --no-ext-diff --no-textconv --cached --binary '
        f"{shlex.quote(base_revision)}"
    )


__all__ = ["guarded_staged_diff_command"]
