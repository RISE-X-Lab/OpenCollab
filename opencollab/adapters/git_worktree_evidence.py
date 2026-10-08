"""The rules a worktree's change evidence follows, wherever the worktree lives.

A worktree on the host and a worktree inside a task container answer the same
three questions -- what did this agent start from, where does it stand now, and
which commits did it make itself -- by reading the same Git output. Only the way
Git is run differs, so the reading lives here and each environment supplies its
own transport.

Keeping one copy matters more than the line count saved: the base a diff is
measured against is what makes a ``worktree_changes`` row attributable to one
agent, and two copies of that rule could disagree without anything failing.
"""

from __future__ import annotations

import re

# One HEAD reflog entry as ``git log -g --format=%H%x09%P%x09%gs`` writes it:
# the commit HEAD moved to, its parents, and the message saying how it got there. Object ids
# are 40 hexadecimal characters in a SHA-1 repository and 64 in a SHA-256 one;
# a rule that recognised only the first would read every entry of a SHA-256
# repository as unparseable and silently report the creation base forever.
REFLOG_ENTRY_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})\t([0-9a-f ]*)\t(.*)$")
# Reflog messages for a commit this worktree made itself: ``commit: <subject>``,
# and the parenthesised variants ``commit (initial)``, ``commit (amend)``,
# ``commit (merge)``. Cherry-pick and revert also append work to the current history.
OWN_COMMIT_REFLOG_PREFIX = ("commit", "cherry-pick:", "revert:")
REBASE_REFLOG_RE = re.compile(r"^rebase(?: -i)? \(([^)]+)\):")
# How many of a worktree's own commits are listed to a caller. The true total is
# reported separately, so a capped list never reads as a shorter one.
OWN_COMMIT_LIMIT = 64
# The old value ``git update-ref`` reads as "this ref must not exist yet", which
# is how a worktree branch is claimed without a check-then-write gap. The
# all-zero object id means the same thing but has to be as long as the
# repository's hash, so it silently stops being a valid old value in a SHA-256
# repository; the empty string says it in a way no hash length can invalidate.
ABSENT_REF_OLD_VALUE = ""
BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")


def validate_worktree_branch(name: str) -> str:
    """Return ``name`` if it is one safe Git ref component, else raise."""
    if not isinstance(name, str) or BRANCH_NAME_RE.fullmatch(name) is None:
        raise ValueError("worktree branch name must be one safe Git ref component")
    return name


def select_diff_base(reflog: str, *, fallback: str) -> str:
    """The external history this worktree's current work grew from, or ``fallback``.

    ``reflog`` is ``git log -g --format=%H%x09%P%x09%gs HEAD``, newest first.
    Checkout, reset, and fast-forward merge adopt the commit they move onto.
    Commit, amend, cherry-pick, and revert extend or revise the current work.
    Rebase adopts its start commit, retaining the replayed work in the diff.
    A two-parent merge adopts its incoming parent, retaining the first parent's
    work and any conflict resolution. Aborted rebases leave the previous base
    intact, including when the rebase had already replayed some commits.

    ``fallback`` is the commit the worktree was created on, which is the honest
    answer when there is nothing to read -- a repository with
    ``core.logAllRefUpdates`` off keeps no such record.
    """
    entries = [
        match.groups() for line in reflog.split("\n")
        if (match := REFLOG_ENTRY_RE.match(line)) is not None
    ]
    aborted_rebases = 0
    for index, (commit, parents, message) in enumerate(entries):
        rebase = REBASE_REFLOG_RE.match(message)
        if rebase is not None:
            action = rebase.group(1)
            if action == "abort":
                aborted_rebases += 1
            elif action == "start":
                if aborted_rebases:
                    aborted_rebases -= 1
                elif index + 1 == len(entries) or entries[index + 1][0] != commit:
                    return commit
            continue
        if aborted_rebases:
            continue
        parent_commits = parents.split()
        if len(parent_commits) == 2 and (
            message.startswith("commit (merge):") or ": Merge made by " in message
        ):
            return parent_commits[1]
        if message.startswith(OWN_COMMIT_REFLOG_PREFIX):
            continue
        # Creating a branch at the current HEAD does not adopt a new base.
        if index + 1 < len(entries) and entries[index + 1][0] == commit:
            continue
        return commit
    return fallback


def parse_own_commits(rev_list: str) -> tuple[tuple[str, ...], int]:
    """Split ``git rev-list <base>..HEAD`` output into a capped list and its total.

    The list is exactly the set of shas this agent could hand to a teammate --
    every commit reachable from HEAD but not from the base it started this
    stretch of work on -- so a handoff joins as ``tester.diff_base in
    coder.commits``. The total is returned separately because a capped list must
    not read as a shorter one.
    """
    commits = rev_list.split()
    return tuple(commits[:OWN_COMMIT_LIMIT]), len(commits)
