---
name: arcbench-checkpoint
description: Manage ARC Bench workspace git checkpoints through the runtime SDK. Use when an agent needs to initialize a workspace git repo, configure anonymous or user git identity, commit generated assets and .arc/traceability state, inspect HEAD/status, restore the worktree, clean untracked files, or reset to a selected commit during pause, resume, or rewind workflows.
---

# ARC Bench Checkpoint

Use this skill when workspace state must be saved or restored. It wraps runtime git operations so commit-history frontend refresh signals are emitted consistently.

## Quick Start

Use the script path relative to this skill directory. Run from the target project workspace and keep `--project-dir .`, or prefix `scripts/arc_checkpoint.py` with the relative path from the workspace to this skill directory.

```bash
python scripts/arc_checkpoint.py ensure-repo --project-dir .
python scripts/arc_checkpoint.py commit --project-dir . --message "REQ-1 design: home page interfaces"
python scripts/arc_checkpoint.py head --project-dir .
```

## Actions

- `ensure-repo`: initialize git repo, configure identity, and add ARC gitignore rules.
- `commit --message <message>`: stage all files and create a checkpoint; returns `committed=false` when no changes exist.
- `status`: print `git status --short`.
- `head`: print current HEAD oid.
- `reset --commit <oid>`: hard reset to a selected commit. Use only when explicitly requested by the user or the ARC Bench rewind flow.
- `restore-worktree`: hard reset current worktree to HEAD. Use only when explicitly requested.
- `clean-untracked`: remove untracked files. Use only when explicitly requested.

## Rules

- Commit after a coherent phase, not after every atomic file write.
- Include `.arc/traceability` in checkpoints so traceability rewinds with code.
- Prefer messages containing requirement id and phase, for example `REQ-2 (design): Search form interfaces`.
- Do not call reset/restore/clean as a speculative fix.
