# ARC Bench Checkpoint Policy

## Commit Boundaries

Commit after meaningful workflow milestones:

- requirement tree or seed stored
- design artifacts for one node finished
- implementation for one node finished
- test results for one node updated
- manual edit phase completed

Avoid committing after every small SDK call unless the caller explicitly needs that granularity.

## Git Identity

The runtime SDK reads identity from:

- `ARC_GIT_USER_NAME`, `ARC_GIT_USER_EMAIL`
- `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`
- fallback anonymous ARC Bench identity

## Rewind Safety

`reset`, `restore-worktree`, and `clean-untracked` are destructive. Use them only for explicit pause/rewind/resume flows or direct user instruction.
