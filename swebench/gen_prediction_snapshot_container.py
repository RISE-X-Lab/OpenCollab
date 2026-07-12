"""Create an anonymous single-commit Git repository inside a solver image."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_OBJECT_ID_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_LOOSE_OBJECT_DIR_RE = re.compile(r"[0-9a-fA-F]{2}")
_LOOSE_OBJECT_FILE_RE = re.compile(r"[0-9a-fA-F]{38}|[0-9a-fA-F]{62}")
_PACK_OBJECT_FILE_RE = re.compile(r"pack-[0-9a-fA-F]{40,64}\.(?:pack|idx)")
_DANGEROUS_GIT_ENV = (
    "GIT_COMMON_DIR",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
)
_DANGEROUS_GIT_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_MAX_AUDIT_ENTRIES = 1_000_000
_MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024


class SnapshotSetupError(RuntimeError):
    """Raised when a trusted snapshot cannot be constructed or proven isolated."""


def _clean_git_env() -> dict[str, str]:
    unsafe = sorted(
        name
        for name, value in os.environ.items()
        if value
        and (
            name in _DANGEROUS_GIT_ENV
            or name.startswith(_DANGEROUS_GIT_ENV_PREFIXES)
        )
    )
    if unsafe:
        raise SnapshotSetupError("unsafe Git environment: " + ", ".join(unsafe))
    env = os.environ.copy()
    for name in tuple(env):
        if name in _DANGEROUS_GIT_ENV or name.startswith(_DANGEROUS_GIT_ENV_PREFIXES):
            env.pop(name, None)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _run_git(
    repo: Path,
    *args: str,
    env: dict[str, str],
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
        env=env,
    )
    if len(result.stdout) > _MAX_GIT_OUTPUT_BYTES or len(result.stderr) > _MAX_GIT_OUTPUT_BYTES:
        raise SnapshotSetupError("Git snapshot command exceeded its output bound")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotSetupError(
            f"Git snapshot command failed ({args[0]}, exit {result.returncode}): {detail[:1000]}"
        )
    return result


def _git_text(repo: Path, *args: str, env: dict[str, str]) -> str:
    return _run_git(repo, *args, env=env).stdout.decode("utf-8", errors="strict").strip()


def _git_init_args(object_format: str) -> tuple[str, ...]:
    if object_format == "sha1":
        return ("init", "-q")
    return ("init", "-q", f"--object-format={object_format}")


def _tracked_index(repo: Path, *, env: dict[str, str]) -> tuple[bytes, list[tuple[str, str, str]]]:
    records = _run_git(repo, "ls-files", "--stage", "-z", env=env).stdout.split(b"\0")
    regular_paths: list[bytes] = []
    gitlinks: list[tuple[str, str, str]] = []
    for record in records:
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise SnapshotSetupError("tracked Git index record is malformed")
        mode = fields[0].decode("ascii", errors="strict")
        object_id = fields[1].decode("ascii", errors="strict")
        stage = fields[2].decode("ascii", errors="strict")
        if stage != "0" or _OBJECT_ID_RE.fullmatch(object_id) is None:
            raise SnapshotSetupError("tracked Git index contains an unsupported entry")
        if mode == "160000":
            gitlinks.append((mode, object_id.lower(), os.fsdecode(raw_path)))
        else:
            regular_paths.append(raw_path)
    pathspec = b"\0".join(regular_paths)
    if pathspec:
        pathspec += b"\0"
    return pathspec, gitlinks


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _remove_gitlink_worktrees(
    repo_root: Path,
    gitlinks: list[tuple[str, str, str]],
) -> int:
    """Remove checked-out submodules while retaining their index gitlinks."""
    removed = 0
    for _mode, _object_id, path in gitlinks:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise SnapshotSetupError("tracked gitlink escaped the repository")
        worktree = repo_root / relative
        resolved = worktree.resolve(strict=False)
        if resolved == repo_root or not _is_within(resolved, repo_root):
            raise SnapshotSetupError("tracked gitlink escaped the repository")
        if worktree.is_symlink():
            raise SnapshotSetupError("tracked gitlink resolves through a symlink")
        if worktree.is_dir():
            shutil.rmtree(worktree)
            removed += 1
        elif worktree.exists():
            worktree.unlink()
            removed += 1
    return removed


def _looks_like_bare_repository(path: Path) -> bool:
    head = path / "HEAD"
    objects = path / "objects"
    refs = path / "refs"
    if not head.is_file() or not objects.is_dir() or not refs.is_dir():
        return False
    try:
        value = head.read_text(encoding="ascii", errors="strict").strip()
    except (OSError, UnicodeError):
        return True
    return value.startswith("ref: ") or _OBJECT_ID_RE.fullmatch(value) is not None


def _looks_like_object_store(path: Path) -> bool:
    """Recognize complete and deliberately stripped Git object directories."""
    try:
        for entry in path.iterdir():
            if entry.is_dir() and _LOOSE_OBJECT_DIR_RE.fullmatch(entry.name):
                if any(
                    child.is_file() and _LOOSE_OBJECT_FILE_RE.fullmatch(child.name)
                    for child in entry.iterdir()
                ):
                    return True
            if entry.name == "pack" and entry.is_dir():
                if any(
                    child.is_file() and _PACK_OBJECT_FILE_RE.fullmatch(child.name)
                    for child in entry.iterdir()
                ):
                    return True
    except OSError as exc:
        raise SnapshotSetupError(f"Git object store audit failed: {type(exc).__name__}") from exc
    return (path / "info").is_dir() and (path / "pack").is_dir()


def _symlink_exposes_git_metadata(target: Path) -> bool:
    if not target.exists():
        return False
    if target.name == ".git":
        return True
    if target.is_dir():
        return (
            (target / ".git").exists()
            or _looks_like_bare_repository(target)
            or _looks_like_object_store(target)
        )
    if target.is_file():
        return bool(
            (
                _LOOSE_OBJECT_DIR_RE.fullmatch(target.parent.name)
                and _LOOSE_OBJECT_FILE_RE.fullmatch(target.name)
            )
            or (
                target.parent.name == "pack"
                and _PACK_OBJECT_FILE_RE.fullmatch(target.name)
            )
        )
    return False


def _git_metadata_audit(repo_root: Path, filesystem_root: Path) -> tuple[list[str], set[Path]]:
    allowed = (repo_root / ".git").resolve(strict=True)
    allowed_objects = allowed / "objects"
    filesystem_root = filesystem_root.resolve(strict=True)
    violations: set[str] = set()
    removable: set[Path] = set()
    visited = 0

    def audit_error(error: OSError) -> None:
        raise SnapshotSetupError(f"Git metadata audit failed: {type(error).__name__}")

    for current, directories, files in os.walk(
        filesystem_root,
        followlinks=False,
        onerror=audit_error,
    ):
        current_path = Path(current)
        if current_path == filesystem_root and filesystem_root == Path("/"):
            directories[:] = [name for name in directories if name not in {"dev", "proc", "sys"}]
        visited += len(directories)
        if visited > _MAX_AUDIT_ENTRIES:
            raise SnapshotSetupError("Git metadata audit exceeded its entry bound")
        if current_path != allowed and _looks_like_bare_repository(current_path):
            if _is_within(current_path, repo_root):
                violations.add(str(current_path))
            else:
                removable.add(current_path)
        if (
            current_path.resolve(strict=True) != allowed_objects
            and _looks_like_object_store(current_path)
        ):
            if _is_within(current_path, repo_root):
                violations.add(str(current_path))
            else:
                removable.add(current_path)
        if _is_within(current_path, repo_root):
            for name in (*directories, *files):
                entry = current_path / name
                if not entry.is_symlink():
                    continue
                target = entry.resolve(strict=False)
                if not _is_within(target, repo_root) and _symlink_exposes_git_metadata(target):
                    violations.add(f"{entry} -> {target}")
        if ".git" in directories:
            entry = current_path / ".git"
            metadata = entry.resolve(strict=True)
            if metadata != allowed:
                if _is_within(entry, repo_root):
                    violations.add(str(metadata))
                else:
                    removable.add(current_path)
            directories.remove(".git")
        if ".git" in files:
            entry = current_path / ".git"
            if entry.resolve(strict=True) != allowed:
                if _is_within(entry, repo_root):
                    violations.add(str(entry))
                else:
                    removable.add(current_path)
    return sorted(violations), removable


def _sanitize_external_git_metadata(repo_root: Path, filesystem_root: Path) -> int:
    violations, removable = _git_metadata_audit(repo_root, filesystem_root)
    if violations:
        raise SnapshotSetupError("additional Git metadata is visible to the solver")
    minimal: list[Path] = []
    for path in sorted(removable, key=lambda item: len(item.parts)):
        if any(_is_within(path, parent) for parent in minimal):
            continue
        minimal.append(path)
    filesystem_root = filesystem_root.resolve(strict=True)
    for path in minimal:
        resolved_parent = path.parent.resolve(strict=True)
        if (
            not _is_within(resolved_parent, filesystem_root)
            or _is_within(path, repo_root)
            or path == filesystem_root
            or path.parent == filesystem_root
        ):
            raise SnapshotSetupError("external Git metadata cleanup escaped its containment root")
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    remaining_violations, remaining_removable = _git_metadata_audit(repo_root, filesystem_root)
    if remaining_violations or remaining_removable:
        raise SnapshotSetupError("additional Git metadata is visible to the solver")
    return len(minimal)


def create_solver_snapshot(
    workspace: Path,
    expected_base_commit: str,
    *,
    filesystem_root: Path = Path("/"),
) -> dict[str, str | int | bool]:
    """Replace the source repository metadata and return machine-verifiable evidence."""
    expected_base_commit = str(expected_base_commit or "").strip().lower()
    if _OBJECT_ID_RE.fullmatch(expected_base_commit) is None:
        raise SnapshotSetupError("expected base commit must be a full hexadecimal object id")
    env = _clean_git_env()
    workspace = workspace.resolve(strict=True)
    repo_root = Path(_git_text(workspace, "rev-parse", "--show-toplevel", env=env)).resolve(strict=True)
    git_dir = Path(_git_text(repo_root, "rev-parse", "--absolute-git-dir", env=env)).resolve(strict=True)
    if git_dir != repo_root / ".git":
        raise SnapshotSetupError("external Git directories are not supported for solver snapshots")

    _run_git(repo_root, "reset", "--hard", expected_base_commit, env=env)
    _run_git(repo_root, "clean", "-ffd", env=env)
    if _git_text(repo_root, "rev-parse", "HEAD", env=env).lower() != expected_base_commit:
        raise SnapshotSetupError("repository did not reach the expected base commit")
    base_tree = _git_text(repo_root, "rev-parse", "HEAD^{tree}", env=env).lower()
    object_format = _git_text(repo_root, "rev-parse", "--show-object-format", env=env)
    if object_format not in {"sha1", "sha256"}:
        raise SnapshotSetupError("repository uses an unsupported Git object format")
    tracked_paths, gitlinks = _tracked_index(repo_root, env=env)
    removed_git_metadata = _remove_gitlink_worktrees(repo_root, gitlinks)

    shutil.rmtree(git_dir)
    _run_git(repo_root, *_git_init_args(object_format), env=env)
    _run_git(repo_root, "config", "user.name", "OpenCollab Solver Snapshot", env=env)
    _run_git(repo_root, "config", "user.email", "solver-snapshot@invalid", env=env)
    _run_git(repo_root, "config", "core.autocrlf", "false", env=env)
    _run_git(repo_root, "config", "diff.ignoreSubmodules", "all", env=env)
    exclude = repo_root / ".git" / "info" / "exclude"
    exclude.write_text("/.opencollab/\n", encoding="utf-8")
    if tracked_paths:
        _run_git(
            repo_root,
            "add",
            "-f",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            env=env,
            input_bytes=tracked_paths,
        )
    for mode, object_id, path in gitlinks:
        _run_git(
            repo_root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{mode},{object_id},{path}",
            env=env,
        )
    snapshot_tree = _git_text(repo_root, "write-tree", env=env).lower()
    if snapshot_tree != base_tree:
        raise SnapshotSetupError("snapshot tree differs from the expected base tree")

    commit_env = env.copy()
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": "OpenCollab Solver Snapshot",
            "GIT_AUTHOR_EMAIL": "solver-snapshot@invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_NAME": "OpenCollab Solver Snapshot",
            "GIT_COMMITTER_EMAIL": "solver-snapshot@invalid",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
    )
    _run_git(repo_root, "commit", "-q", "--no-gpg-sign", "-m", "solver snapshot", env=commit_env)

    anonymous_head = _git_text(repo_root, "rev-parse", "HEAD", env=env).lower()
    commit_count = int(_git_text(repo_root, "rev-list", "--all", "--count", env=env))
    parent_fields = _git_text(repo_root, "rev-list", "--parents", "-1", "HEAD", env=env).split()
    remotes = [line for line in _git_text(repo_root, "remote", env=env).splitlines() if line]
    if commit_count != 1 or len(parent_fields) != 1:
        raise SnapshotSetupError("snapshot repository is not a single root commit")
    if remotes:
        raise SnapshotSetupError("snapshot repository retained a remote")
    forbidden_metadata = (
        repo_root / ".git" / "objects" / "info" / "alternates",
        repo_root / ".git" / "info" / "grafts",
        repo_root / ".git" / "refs" / "replace",
    )
    if any(path.exists() for path in forbidden_metadata):
        raise SnapshotSetupError("snapshot repository retained external object metadata")
    fsck = _run_git(repo_root, "fsck", "--full", "--unreachable", "--no-reflogs", env=env)
    if fsck.stdout.strip() or fsck.stderr.strip():
        raise SnapshotSetupError("snapshot repository contains unreachable objects")
    if _git_text(repo_root, "status", "--porcelain", "--untracked-files=no", env=env):
        raise SnapshotSetupError("snapshot repository has tracked baseline changes")
    removed_git_metadata += _sanitize_external_git_metadata(repo_root, filesystem_root)

    return {
        "enabled": True,
        "anonymous_head": anonymous_head,
        "base_tree": base_tree,
        "commit_count": commit_count,
        "remote_count": 0,
        "extra_git_metadata": 0,
        "removed_git_metadata": removed_git_metadata,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: gen_prediction_snapshot_container.py WORKSPACE BASE_COMMIT", file=sys.stderr)
        return 2
    try:
        evidence = create_solver_snapshot(Path(args[0]), args[1])
    except (OSError, SnapshotSetupError, UnicodeError, ValueError) as exc:
        print(f"solver Git snapshot failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
