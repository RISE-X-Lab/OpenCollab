"""Discover, remove, and audit Git configuration visible to solver snapshots."""

from __future__ import annotations

import os
import selectors
import stat
import subprocess
import time
from pathlib import Path

_MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_REFERENCED_ENTRIES = 10_000
_GIT_CONFIG_AUDIT_TIMEOUT_SECONDS = 15
_SAFE_INHERITED_GIT_ENV = {
    "GIT_PAGER": {"cat"},
    "GIT_TERMINAL_PROMPT": {"0"},
}
_REFERENCED_CONFIG_PATH_KEYS = {
    "core.attributesfile",
    "core.excludesfile",
    "core.hookspath",
}


class SnapshotSetupError(RuntimeError):
    """Raised when a trusted snapshot cannot be constructed or proven isolated."""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _assert_safe_inherited_git_env() -> None:
    unsafe = sorted(
        name
        for name, value in os.environ.items()
        if name.startswith("GIT_") and value not in _SAFE_INHERITED_GIT_ENV.get(name, set())
    )
    if unsafe:
        raise SnapshotSetupError("unsafe Git environment: " + ", ".join(unsafe))


def _bounded_git_config(
    repo_root: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        process = subprocess.Popen(
            ["git", "-C", str(repo_root), "config", *args],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise SnapshotSetupError(f"default Git config audit failed: {type(exc).__name__}") from exc
    selector = selectors.DefaultSelector()
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + _GIT_CONFIG_AUDIT_TIMEOUT_SECONDS
    try:
        for name, stream in streams.items():
            if stream is None:
                raise SnapshotSetupError("default Git config audit did not expose captured output")
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SnapshotSetupError("default Git config audit timed out")
            for key, _mask in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                output = buffers[key.data]
                if len(output) + len(chunk) > _MAX_GIT_OUTPUT_BYTES:
                    raise SnapshotSetupError("default Git config audit exceeded its output bound")
                output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SnapshotSetupError("default Git config audit timed out")
        returncode = process.wait(timeout=remaining)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotSetupError(f"default Git config audit failed: {type(exc).__name__}") from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in streams.values():
            if stream is not None and not stream.closed:
                stream.close()
    return subprocess.CompletedProcess(
        ["git", "-C", str(repo_root), "config", *args],
        returncode,
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
    )


def _default_git_config_records(repo_root: Path) -> list[tuple[str, str, str, str]]:
    _assert_safe_inherited_git_env()
    args = (
        "--includes",
        "--show-origin",
        "--show-scope",
        "--null",
        "--list",
    )
    result = _bounded_git_config(
        repo_root,
        *args,
    )
    has_scope = True
    if result.returncode == 129:
        detail = result.stderr.decode("utf-8", errors="replace").lower()
        if "unknown option" in detail and "show-scope" in detail:
            has_scope = False
            args = tuple(item for item in args if item != "--show-scope")
            result = _bounded_git_config(repo_root, *args)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotSetupError(f"default Git config audit failed (exit {result.returncode}): {detail[:1000]}")
    fields = result.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    record_width = 3 if has_scope else 2
    if len(fields) % record_width:
        raise SnapshotSetupError("default Git config audit returned malformed records")
    records: list[tuple[str, str, str, str]] = []
    for offset in range(0, len(fields), record_width):
        if has_scope:
            scope = fields[offset].decode("ascii", errors="strict").lower()
            origin = os.fsdecode(fields[offset + 1])
            key_value = fields[offset + 2]
        else:
            scope = "unknown"
            origin = os.fsdecode(fields[offset])
            key_value = fields[offset + 1]
        raw_key, separator, raw_value = key_value.partition(b"\n")
        if not raw_key:
            raise SnapshotSetupError("default Git config audit returned an empty key")
        key = raw_key.decode("utf-8", errors="strict").lower()
        value = raw_value.decode("utf-8", errors="strict") if separator else ""
        records.append((scope, origin, key, value))
    return records


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _origin_path(origin: str, repo_root: Path) -> Path | None:
    if not origin.startswith("file:"):
        return None
    path = Path(origin[len("file:") :])
    if not path.is_absolute():
        path = repo_root / path
    return _absolute_lexical(path)


def _home_directory() -> Path:
    raw = os.environ.get("HOME")
    home = Path(raw) if raw else Path.home()
    if not home.is_absolute():
        raise SnapshotSetupError("default Git HOME is not absolute")
    return _absolute_lexical(home)


def _xdg_config_directory(home: Path) -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    path = Path(raw) if raw else home / ".config"
    if not path.is_absolute():
        raise SnapshotSetupError("default Git XDG_CONFIG_HOME is not absolute")
    return _absolute_lexical(path)


def _discover_system_config_path(repo_root: Path) -> Path:
    env = os.environ.copy()
    env["GIT_EDITOR"] = "echo"
    env["GIT_PAGER"] = "cat"
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = _bounded_git_config(repo_root, "--system", "--edit", env=env)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotSetupError(f"system Git config discovery failed (exit {result.returncode}): {detail[:1000]}")
    lines = [line for line in result.stdout.splitlines() if line]
    if len(lines) != 1:
        raise SnapshotSetupError("system Git config discovery returned malformed output")
    path = Path(os.fsdecode(lines[0]))
    if not path.is_absolute():
        raise SnapshotSetupError("system Git config path is not absolute")
    return _absolute_lexical(path)


def _default_config_candidates(repo_root: Path, git_dir: Path) -> set[Path]:
    home = _home_directory()
    xdg = _xdg_config_directory(home)
    return {
        git_dir / "config",
        git_dir / "config.worktree",
        home / ".gitconfig",
        xdg / "git" / "config",
        _discover_system_config_path(repo_root),
    }


def _expanded_config_paths(origin: str, key: str, value: str, repo_root: Path) -> list[str]:
    source = _origin_path(origin, repo_root)
    if source is None:
        return [value]
    result = _bounded_git_config(
        repo_root,
        "--file",
        str(source),
        "--null",
        "--path",
        "--get-all",
        key,
    )
    if result.returncode not in {0, 1}:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotSetupError(f"Git config path expansion failed (exit {result.returncode}): {detail[:1000]}")
    values = [item.decode("utf-8", errors="strict") for item in result.stdout.split(b"\0") if item]
    return values or [value]


def _referenced_config_paths(origin: str, key: str, value: str, repo_root: Path) -> set[Path]:
    if not value or value == os.devnull:
        return set()
    is_include = key == "include.path" or (key.startswith("includeif.") and key.endswith(".path"))
    if key not in _REFERENCED_CONFIG_PATH_KEYS and not is_include:
        return set()
    paths: set[Path] = set()
    for expanded in _expanded_config_paths(origin, key, value, repo_root):
        if not expanded or expanded == os.devnull:
            continue
        path = Path(expanded)
        if path.is_absolute():
            resolved = path
        elif is_include:
            source = _origin_path(origin, repo_root)
            if source is None:
                raise SnapshotSetupError("relative Git include has no file origin")
            resolved = source.parent / path
        else:
            resolved = repo_root / path
        paths.add(_absolute_lexical(resolved))
    return paths


def _artifact_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_mode, info.st_nlink


def _checked_artifact(
    path: Path,
    *,
    filesystem_root: Path,
    repo_root: Path,
    allow_directory: bool,
    entry_budget: list[int],
    seen: set[tuple[int, int]],
) -> None:
    if not os.path.lexists(path):
        return
    if not path.is_absolute() or not _is_within(path, filesystem_root):
        raise SnapshotSetupError("referenced Git config artifact escaped its containment root")
    try:
        parent = path.parent.resolve(strict=True)
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        raise SnapshotSetupError("referenced Git config artifact has an unknown link") from exc
    if not _is_within(parent, filesystem_root) or not _is_within(resolved, filesystem_root):
        raise SnapshotSetupError("referenced Git config artifact escaped its containment root")
    identity = (info.st_dev, info.st_ino)
    if identity in seen:
        return
    seen.add(identity)
    entry_budget[0] += 1
    if entry_budget[0] > _MAX_REFERENCED_ENTRIES:
        raise SnapshotSetupError("referenced Git config artifact exceeded its entry bound")
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise SnapshotSetupError("referenced Git config artifact has multiple hard links")
        return
    if stat.S_ISLNK(info.st_mode):
        _checked_artifact(
            resolved,
            filesystem_root=filesystem_root,
            repo_root=repo_root,
            allow_directory=allow_directory,
            entry_budget=entry_budget,
            seen=seen,
        )
        return
    if not stat.S_ISDIR(info.st_mode) or not allow_directory:
        raise SnapshotSetupError("referenced Git config artifact is not safe to remove")
    if resolved == filesystem_root or _is_within(repo_root, resolved):
        raise SnapshotSetupError("referenced Git config artifact contains the solver repository")
    try:
        children = sorted(
            path.iterdir(),
            key=lambda child: (not child.is_symlink(), child.name),
        )
    except OSError as exc:
        raise SnapshotSetupError("referenced Git config artifact cannot be enumerated") from exc
    for child in children:
        _checked_artifact(
            child,
            filesystem_root=filesystem_root,
            repo_root=repo_root,
            allow_directory=True,
            entry_budget=entry_budget,
            seen=seen,
        )


def _remove_checked_artifact(
    path: Path,
    *,
    filesystem_root: Path,
    repo_root: Path,
    allow_directory: bool,
    removed: set[tuple[int, int]],
) -> None:
    if not os.path.lexists(path):
        return
    identity = _artifact_identity(path)[:2]
    if identity in removed:
        return
    removed.add(identity)
    info = path.lstat()
    resolved = path.resolve(strict=True)
    if stat.S_ISLNK(info.st_mode):
        path.unlink()
        _remove_checked_artifact(
            resolved,
            filesystem_root=filesystem_root,
            repo_root=repo_root,
            allow_directory=allow_directory,
            removed=removed,
        )
        return
    if stat.S_ISREG(info.st_mode):
        if _artifact_identity(path) != (info.st_dev, info.st_ino, info.st_mode, info.st_nlink):
            raise SnapshotSetupError("referenced Git config artifact changed during cleanup")
        path.unlink()
        return
    if not stat.S_ISDIR(info.st_mode) or not allow_directory:
        raise SnapshotSetupError("referenced Git config artifact is not safe to remove")
    try:
        children = sorted(
            path.iterdir(),
            key=lambda child: (not child.is_symlink(), child.name),
        )
    except OSError as exc:
        raise SnapshotSetupError("referenced Git config artifact cannot be enumerated") from exc
    for child in children:
        _remove_checked_artifact(
            child,
            filesystem_root=filesystem_root,
            repo_root=repo_root,
            allow_directory=True,
            removed=removed,
        )
    path.rmdir()


def sanitize_default_git_configs(repo_root: Path, git_dir: Path, filesystem_root: Path) -> None:
    """Remove every default config candidate and every path it references."""
    _assert_safe_inherited_git_env()
    filesystem_root = filesystem_root.resolve(strict=True)
    repo_root = repo_root.resolve(strict=True)
    git_dir = git_dir.resolve(strict=True)
    records = _default_git_config_records(repo_root)
    artifacts: dict[Path, bool] = {}
    candidates = _default_config_candidates(repo_root, git_dir)
    for candidate in candidates:
        if _is_within(candidate, filesystem_root):
            artifacts[_absolute_lexical(candidate)] = False
    for _scope, origin, key, value in records:
        origin_path = _origin_path(origin, repo_root)
        if origin_path is None:
            raise SnapshotSetupError("default Git config retained a non-file origin")
        if not _is_within(origin_path, filesystem_root):
            continue
        artifacts[origin_path] = False
        for referenced in _referenced_config_paths(origin, key, value, repo_root):
            artifacts[referenced] = artifacts.get(referenced, False) or key == "core.hookspath"

    budget = [0]
    seen: set[tuple[int, int]] = set()
    for path, allow_directory in artifacts.items():
        _checked_artifact(
            path,
            filesystem_root=filesystem_root,
            repo_root=repo_root,
            allow_directory=allow_directory,
            entry_budget=budget,
            seen=seen,
        )
    removed: set[tuple[int, int]] = set()
    for path, allow_directory in sorted(
        artifacts.items(),
        key=lambda item: (
            not (os.path.lexists(item[0]) and item[0].is_symlink()),
            -len(item[0].parts),
        ),
    ):
        _remove_checked_artifact(
            path,
            filesystem_root=filesystem_root,
            repo_root=repo_root,
            allow_directory=allow_directory,
            removed=removed,
        )
    for path in artifacts:
        if os.path.lexists(path):
            raise SnapshotSetupError("default Git config cleanup left a visible artifact")


def clean_git_env(trusted_root: Path) -> dict[str, str]:
    """Return an environment with fixed empty HOME/XDG and disabled outer configs."""
    _assert_safe_inherited_git_env()
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("GIT_"):
            env.pop(name, None)
    home = trusted_root / "home"
    xdg = trusted_root / "xdg"
    hooks = trusted_root / "hooks"
    template = trusted_root / "template"
    for directory in (home, xdg, hooks, template):
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg),
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(hooks),
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TEMPLATE_DIR": str(template),
        }
    )
    return env


def replace_untrusted_repository_config(git_dir: Path, object_format: str) -> None:
    """Create the minimal repository config used while reading the trusted base."""
    objects = git_dir / "objects"
    if objects.is_symlink() or not objects.is_dir() or objects.resolve(strict=True) != objects:
        raise SnapshotSetupError("external Git object directories are not supported for solver snapshots")
    if (objects / "info" / "alternates").exists() or (git_dir / "commondir").exists():
        raise SnapshotSetupError("external Git object metadata is not supported for solver snapshots")
    for name in ("config", "config.worktree"):
        path = git_dir / name
        if os.path.lexists(path):
            raise SnapshotSetupError("repository Git config cleanup was incomplete")
    repository_format = "1" if object_format == "sha256" else "0"
    extension = "\n[extensions]\n\tobjectFormat = sha256" if object_format == "sha256" else ""
    (git_dir / "config").write_text(
        "[core]\n"
        f"\trepositoryFormatVersion = {repository_format}\n"
        "\tfileMode = true\n"
        "\tbare = false\n"
        "\tlogAllRefUpdates = true\n"
        f"{extension}\n",
        encoding="utf-8",
    )
    info = git_dir / "info"
    if os.path.lexists(info):
        if info.is_symlink() or not info.is_dir() or info.resolve(strict=True) != info:
            raise SnapshotSetupError("repository Git info directory is not trusted")
    else:
        info.mkdir(mode=0o755)
    for name in ("attributes", "exclude"):
        path = info / name
        if os.path.lexists(path):
            item = path.lstat()
            if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
                raise SnapshotSetupError("repository Git info file is not a trusted regular file")
            path.unlink()
    (info / "exclude").write_text("", encoding="utf-8")


def audit_default_solver_git_config(repo_root: Path, filesystem_root: Path, object_format: str) -> None:
    """Prove that the ordinary solver environment sees the new local config only."""
    filesystem_root = filesystem_root.resolve(strict=True)
    local_config = (repo_root / ".git" / "config").resolve(strict=True)
    visible: list[tuple[str, str]] = []
    for scope, origin, key, value in _default_git_config_records(repo_root):
        path = _origin_path(origin, repo_root)
        if path is None:
            raise SnapshotSetupError("snapshot repository retained a non-file Git config origin")
        if not _is_within(path, filesystem_root):
            continue
        if path.resolve(strict=True) != local_config:
            raise SnapshotSetupError("snapshot repository retained an external Git config origin")
        if scope not in {"local", "worktree", "unknown"}:
            raise SnapshotSetupError("snapshot repository retained an external Git config scope")
        visible.append((key, value))
    expected = {
        "core.repositoryformatversion": "1" if object_format == "sha256" else "0",
        "core.bare": "false",
        "core.logallrefupdates": "true",
        "core.autocrlf": "false",
        "core.attributesfile": os.devnull,
        "core.fsmonitor": "false",
        "core.hookspath": os.devnull,
        "diff.ignoresubmodules": "all",
    }
    if object_format == "sha256":
        expected["extensions.objectformat"] = "sha256"
    optional_boolean = {
        "core.filemode",
        "core.ignorecase",
        "core.precomposeunicode",
        "core.protecthfs",
        "core.protectntfs",
        "core.symlinks",
    }
    values: dict[str, str] = {}
    for key, value in visible:
        if key in optional_boolean:
            if value not in {"true", "false"} or key in values:
                raise SnapshotSetupError("snapshot repository retained an invalid Git config")
        elif key not in expected or value != expected[key] or key in values:
            raise SnapshotSetupError("snapshot repository retained an invalid Git config")
        values[key] = value
    if not set(expected).issubset(values) or "core.filemode" not in values:
        raise SnapshotSetupError("snapshot repository returned incomplete Git config evidence")
