"""Build fail-closed Git patch extraction commands."""

from __future__ import annotations

import shlex
from collections.abc import Sequence


def guarded_staged_diff_command(
    *,
    base_revision: str = "HEAD",
    exclude_paths: Sequence[str] = (),
) -> str:
    """Stage through a temporary index without repository-local diff hooks.

    Repository-local configuration is kept out of the evidence three ways, and
    which one applies is per setting.

    ``core.attributesFile`` and ``diff.ignoreSubmodules`` are pinned by an
    explicit ``-c``, which outranks any repository-local value, so a repository
    that marks a file undiffable or hides a gitlink change cannot do either
    here. ``core.fsmonitor`` is judged by its value: a repository that turns a
    monitor off is hardening itself and is allowed through, while one that turns
    a monitor on is refused, because a monitor that lies about what changed can
    only be trusted or refused, never overridden. Everything else that could
    bend the reading -- a redirected working tree, a sparse checkout, an
    excludes file, and the clean/smudge filters and external diff drivers a
    tracked ``.gitattributes`` can still reach -- is refused outright.

    A refusal exits 125 rather than reporting a patch this command cannot vouch
    for.
    """
    if not isinstance(base_revision, str) or not base_revision.strip() or "\0" in base_revision:
        raise ValueError("patch base revision is invalid")

    git_env = (
        "GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null "
        "GIT_EXTERNAL_DIFF= GIT_NO_REPLACE_OBJECTS=1"
    )
    # Settings a repository can carry that would change what this diff reports,
    # pinned on the command line where they outrank any repository-local value.
    # Refusing them instead would be equally safe but far less usable: a harness
    # that rebuilds the repository under test may set exactly these to *disable*
    # attribute and monitor influence on its own snapshot, and a guard that reads
    # the key name alone cannot tell that apart from an attempt to bend the
    # evidence. Anything an override cannot make harmless is still refused below.
    neutralized_config = (
        " -c core.attributesFile=/dev/null"
        " -c diff.ignoreSubmodules=none"
    )
    git_command = (
        f'{git_env} git -c safe.directory="$PWD" -c core.filemode=true'
        f"{neutralized_config}"
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
        "core\\.(excludesfile|sparsecheckout|sparsecheckoutcone|worktree))$' "
        "2>/dev/null); "
        'config_rc=$?; if [ "$config_rc" -eq 0 ]; then '
        "echo \"unsafe repository Git configuration: $unsafe_config\" >&2; exit 125; "
        'elif [ "$config_rc" -ne 1 ]; then exit "$config_rc"; fi; '
        f'fsmonitor=$({git_command} config "$config_scope" --includes --type=bool '
        "--get core.fsmonitor 2>/dev/null); fsmonitor_rc=$?; "
        'if [ "$fsmonitor_rc" -eq 0 ] && [ "$fsmonitor" = false ]; then :; '
        'elif [ "$fsmonitor_rc" -ne 1 ]; then '
        "echo 'unsafe repository Git configuration: core.fsmonitor' >&2; exit 125; fi; "
        "done; "
        f'info_attributes=$({git_command} rev-parse --git-path info/attributes) || exit 125; '
        'if [ -L "$info_attributes" ] || { [ -e "$info_attributes" ] && [ ! -f "$info_attributes" ]; }; then '
        "echo 'repository-local info/attributes is not a regular file' >&2; exit 125; fi; "
        # A repository may turn attributes *off*; it may not turn any on. Every
        # form that assigns a value or sets an attribute can change what the
        # diff shows -- a clean/smudge filter, an external diff driver, an
        # encoding conversion -- and so can ``-diff``, whose "off" means "report
        # this path as binary" and hides content rather than revealing it.
        # Unsetting anything else only makes the diff a more faithful reading of
        # the bytes on disk, which is what a harness hardening its own snapshot
        # is doing when it writes such a file.
        'if [ -s "$info_attributes" ]; then '
        "if ! awk '{ sub(/#.*/, \"\") } NF == 0 { next } "
        '/^[[:space:]]*\\[/ { bad = 1 } '
        '{ for (i = 2; i <= NF; i++) '
        'if ($i == \"-diff\" || $i !~ /^[-!][A-Za-z0-9_.-]+$/) bad = 1 } '
        "END { exit bad ? 1 : 0 }' \"$info_attributes\"; then "
        "echo 'repository-local info/attributes can alter patch evidence' >&2; exit 125; fi; fi; "
    )
    stage_untracked = (
        f'{git_index_command} ls-files --others --exclude-per-directory=.gitignore '
        '-z > "$untracked" && '
        'if [ -s "$untracked" ]; then '
        f'{git_index_command} add -f --pathspec-from-file="$untracked" '
        '--pathspec-file-nul; fi && '
    )
    ignored_guard = (
        f'{git_index_command} ls-files --others --ignored '
        '--exclude-per-directory=.gitignore -z > "$ignored" && '
        'if [ -s "$ignored" ]; then '
        "echo 'ignored untracked files cannot be included in patch evidence' >&2; "
        "exit 125; fi; "
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
        'ignored=$(mktemp) || { rm -f -- "$idx" "$untracked"; exit 125; }; '
        'trap \'rm -f -- "$idx" "$untracked" "$ignored"\' EXIT; '
        f"{unsafe_config_guard}"
        f"{git_index_command} read-tree {shlex.quote(base_revision)} && "
        f'{git_index_command} add -u && '
        f"{ignored_guard}"
        f"{stage_untracked}"
        f"{resets}"
        f"{reserved_guard}"
        f'{git_index_command} diff --no-ext-diff --no-textconv --cached --binary '
        f"{shlex.quote(base_revision)}"
    )


__all__ = ["guarded_staged_diff_command"]
