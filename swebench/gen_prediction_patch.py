"""Host-trusted, bounded extraction of a solver container worktree patch."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from container_quiescence import require_container_quiescence
from gen_prediction_config import _docker_timeout_from_env
from gen_prediction_constants import DOCKER_WORKDIR
from gen_prediction_snapshot import SolverGitSnapshot
from opencollab.harness.swe_generation_proof import (
    MAX_TRUSTED_PATCH_BYTES,
    MAX_WORKSPACE_ARCHIVE_BYTES,
    MAX_WORKSPACE_ARCHIVE_ENTRIES,
    MAX_WORKSPACE_EXTRACTED_BYTES,
    MAX_WORKSPACE_FILE_BYTES,
    TRUSTED_PATCH_EXTRACTION_SCHEMA,
)


@dataclass(frozen=True, slots=True)
class TrustedPatchExtraction:
    fixed_anonymous_base: str
    base_tree: str
    baseline_archive_sha256: str
    baseline_archive_bytes: int
    baseline_archive_entries: int
    baseline_extracted_bytes: int
    workspace_archive_sha256: str
    workspace_archive_bytes: int
    workspace_archive_entries: int
    workspace_extracted_bytes: int
    patch_sha256: str
    patch_bytes: int

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "schema": TRUSTED_PATCH_EXTRACTION_SCHEMA,
            "host_trusted": True,
            "fixed_anonymous_base": self.fixed_anonymous_base,
            "base_tree": self.base_tree,
            "archive_bounded": True,
            "baseline_archive_sha256": self.baseline_archive_sha256,
            "baseline_archive_bytes": self.baseline_archive_bytes,
            "baseline_archive_entries": self.baseline_archive_entries,
            "baseline_extracted_bytes": self.baseline_extracted_bytes,
            "workspace_archive_sha256": self.workspace_archive_sha256,
            "workspace_archive_bytes": self.workspace_archive_bytes,
            "workspace_archive_entries": self.workspace_archive_entries,
            "workspace_extracted_bytes": self.workspace_extracted_bytes,
            "archive_byte_limit": MAX_WORKSPACE_ARCHIVE_BYTES,
            "extracted_byte_limit": MAX_WORKSPACE_EXTRACTED_BYTES,
            "file_byte_limit": MAX_WORKSPACE_FILE_BYTES,
            "entry_limit": MAX_WORKSPACE_ARCHIVE_ENTRIES,
            "patch_byte_limit": MAX_TRUSTED_PATCH_BYTES,
            "container_quiesced_before": True,
            "container_quiesced_after": True,
            "patch_sha256": self.patch_sha256,
            "patch_bytes": self.patch_bytes,
        }


@dataclass(slots=True)
class TrustedPatchBaseline:
    snapshot: SolverGitSnapshot
    temporary_directory: tempfile.TemporaryDirectory[str] | None
    git_dir: Path
    archive_sha256: str
    archive_bytes: int
    archive_entries: int
    extracted_bytes: int

    def cleanup(self) -> None:
        if self.temporary_directory is not None:
            self.temporary_directory.cleanup()


class _BoundedHashReader:
    def __init__(self, raw, limit: int) -> None:
        self.raw = raw
        self.limit = limit
        self.count = 0
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        allowed = self.limit - self.count
        if allowed < 0:
            raise RuntimeError("container workspace archive exceeded its byte limit")
        request = allowed + 1 if size < 0 else min(size, allowed + 1)
        data = self.raw.read(request)
        if not data:
            return b""
        self.count += len(data)
        if self.count > self.limit:
            raise RuntimeError("container workspace archive exceeded its byte limit")
        self.digest.update(data)
        return data


def _member_parts(name: str) -> tuple[str, ...]:
    if "\x00" in name:
        raise RuntimeError("container workspace archive contains a NUL path")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise RuntimeError("container workspace archive contains an absolute path")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        return ()
    if any(part == ".." for part in parts):
        raise RuntimeError("container workspace archive escapes the extraction root")
    return parts


def _safe_parent(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in parts[:-1]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir()
            continue
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise RuntimeError("container workspace archive traverses a non-directory parent")
    return root.joinpath(*parts)


def _symlink_stays_inside(parts: tuple[str, ...], target: str) -> bool:
    target_path = PurePosixPath(target)
    if target_path.is_absolute() or "\x00" in target:
        return False
    depth = len(parts) - 1
    for part in target_path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def _extract_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    root: Path,
    *,
    extracted_bytes: int,
) -> int:
    parts = _member_parts(member.name)
    if not parts:
        if member.isdir():
            return extracted_bytes
        raise RuntimeError("container workspace archive has a non-directory root entry")
    destination = _safe_parent(root, parts)
    if member.isdir():
        try:
            mode = destination.lstat().st_mode
        except FileNotFoundError:
            destination.mkdir()
        else:
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise RuntimeError("container workspace archive redefines a path as a directory")
        return extracted_bytes
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("container workspace archive contains a duplicate path")
    if member.issym():
        if not _symlink_stays_inside(parts, member.linkname):
            raise RuntimeError("container workspace archive contains an escaping symlink")
        os.symlink(member.linkname, destination)
        return extracted_bytes
    if not member.isfile() or member.islnk():
        raise RuntimeError("container workspace archive contains a special or hard-linked entry")
    if member.size < 0 or member.size > MAX_WORKSPACE_FILE_BYTES:
        raise RuntimeError("container workspace archive contains an oversized file")
    total = extracted_bytes + member.size
    if total > MAX_WORKSPACE_EXTRACTED_BYTES:
        raise RuntimeError("container workspace extraction exceeded its byte limit")
    source = archive.extractfile(member)
    if source is None:
        raise RuntimeError("container workspace archive file payload is missing")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(destination, flags, member.mode & 0o777 or 0o600)
    written = 0
    try:
        while written < member.size:
            chunk = source.read(min(1024 * 1024, member.size - written))
            if not chunk:
                raise RuntimeError("container workspace archive file payload is truncated")
            view = memoryview(chunk)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            written += len(chunk)
        os.fsync(fd)
    finally:
        os.close(fd)
    return total


def _copy_workspace_archive(container_id: str, root: Path) -> tuple[str, int, int, int]:
    command = ["docker", "cp", "-L", f"{container_id}:{DOCKER_WORKDIR}/.", "-"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if process.stdout is None:
        process.kill()
        raise RuntimeError("docker cp did not expose its archive stream")
    timed_out = threading.Event()

    def kill_on_timeout() -> None:
        timed_out.set()
        process.kill()

    timer = threading.Timer(_docker_timeout_from_env(), kill_on_timeout)
    reader = _BoundedHashReader(process.stdout, MAX_WORKSPACE_ARCHIVE_BYTES)
    entries = 0
    extracted = 0
    timer.start()
    try:
        with tarfile.open(fileobj=reader, mode="r|*") as archive:
            for member in archive:
                entries += 1
                if entries > MAX_WORKSPACE_ARCHIVE_ENTRIES:
                    raise RuntimeError("container workspace archive exceeded its entry limit")
                extracted = _extract_member(
                    archive,
                    member,
                    root,
                    extracted_bytes=extracted,
                )
        while reader.read(1024 * 1024):
            pass
        returncode = process.wait(timeout=5)
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        timer.cancel()
        process.stdout.close()
    if timed_out.is_set():
        raise RuntimeError("container workspace archive copy timed out")
    if returncode != 0:
        raise RuntimeError(f"docker cp workspace archive failed with exit {returncode}")
    return reader.digest.hexdigest(), reader.count, entries, extracted


def _git_environment(home: Path, template: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TEMPLATE_DIR": str(template),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "xdg"),
            "LC_ALL": "C",
        }
    )
    return env


def _run_git(
    git: str,
    args: list[str],
    *,
    env: dict[str, str],
    timeout: float,
) -> None:
    result = subprocess.run(
        [git, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"trusted host git command failed: {args[0]}")


def _copy_object_store(source_git: Path, clean_git: Path, object_id_length: int) -> None:
    source_objects = source_git / "objects"
    if not source_objects.is_dir() or source_objects.is_symlink():
        raise RuntimeError("solver object store is missing or unsafe")
    copied = 0
    suffix_length = object_id_length - 2
    loose_object = re.compile(rf"[0-9a-f]{{2}}/[0-9a-f]{{{suffix_length}}}\Z")
    packed_object = re.compile(
        rf"pack/pack-[0-9a-f]{{{object_id_length}}}\.(?:idx|pack)\Z"
    )
    for source in source_objects.rglob("*"):
        if source.is_dir() and not source.is_symlink():
            continue
        relative = source.relative_to(source_objects).as_posix()
        if not (loose_object.fullmatch(relative) or packed_object.fullmatch(relative)):
            continue
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("solver object store contains an unsafe object file")
        destination = clean_git / "objects" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied += 1
    if copied == 0:
        raise RuntimeError("solver object store did not contain any usable objects")


def _bounded_git_output(
    git: str,
    args: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    max_bytes: int,
    label: str,
) -> bytes:
    process = subprocess.Popen(
        [git, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    if process.stdout is None:
        process.kill()
        raise RuntimeError("trusted host git did not expose patch output")
    timer = threading.Timer(timeout, process.kill)
    chunks: list[bytes] = []
    total = 0
    timer.start()
    try:
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                process.kill()
                raise RuntimeError(f"trusted host {label} exceeded its byte limit")
            chunks.append(chunk)
        returncode = process.wait(timeout=5)
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        timer.cancel()
        process.stdout.close()
    if returncode != 0:
        raise RuntimeError(f"trusted host {label} failed with exit {returncode}")
    return b"".join(chunks)


def _reject_harness_paths(
    git: str,
    common: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    base: str,
) -> None:
    output = _bounded_git_output(
        git,
        [*common, "diff", "--cached", "--name-only", "-z", base, "--"],
        env=env,
        timeout=timeout,
        max_bytes=MAX_TRUSTED_PATCH_BYTES,
        label="changed-path census",
    )
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        parts = PurePosixPath(path).parts
        if ".opencollab" in parts or any(
            part.startswith(".opencollab-retired-") for part in parts
        ):
            raise RuntimeError("trusted host patch contains a harness-owned path")


def _extract_patch_from_copy(root: Path, baseline: TrustedPatchBaseline) -> str:
    snapshot = baseline.snapshot
    source_git = root / ".git"
    if source_git.is_symlink():
        source_git.unlink()
    elif source_git.is_dir():
        shutil.rmtree(source_git)
    elif source_git.exists():
        source_git.unlink()
    git = shutil.which("git")
    if not git:
        raise RuntimeError("trusted host git executable is unavailable")
    timeout = _docker_timeout_from_env()
    with tempfile.TemporaryDirectory(prefix="opencollab-host-git-") as temp:
        temp_root = Path(temp)
        home = temp_root / "home"
        template = temp_root / "template"
        home.mkdir()
        (home / "xdg").mkdir()
        template.mkdir()
        env = _git_environment(home, template)
        common = [f"--git-dir={baseline.git_dir}", f"--work-tree={root}"]
        _run_git(
            git,
            [*common, "read-tree", snapshot.anonymous_head],
            env=env,
            timeout=timeout,
        )
        _run_git(
            git,
            [
                *common,
                "add",
                "-f",
                "-A",
                "--",
                ".",
                ":(exclude).git",
                ":(exclude).git/**",
                ":(exclude).opencollab",
                ":(exclude).opencollab/**",
            ],
            env=env,
            timeout=timeout,
        )
        _reject_harness_paths(
            git,
            common,
            env=env,
            timeout=timeout,
            base=snapshot.anonymous_head,
        )
        patch = _bounded_git_output(
            git,
            [
                *common,
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                snapshot.anonymous_head,
                "--",
            ],
            env=env,
            timeout=timeout,
            max_bytes=MAX_TRUSTED_PATCH_BYTES,
            label="patch extraction",
        )
    try:
        return patch.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("trusted host patch is not UTF-8") from exc


def prepare_trusted_patch_baseline(
    container_id: str,
    snapshot: SolverGitSnapshot,
) -> TrustedPatchBaseline:
    """Capture and verify the anonymous object store before Solver execution."""
    require_container_quiescence(container_id)
    temporary_directory = tempfile.TemporaryDirectory(prefix="opencollab-trusted-base-")
    temp_root = Path(temporary_directory.name)
    try:
        root = temp_root / "workspace"
        root.mkdir()
        archive_sha, archive_bytes, entries, extracted_bytes = _copy_workspace_archive(
            container_id,
            root,
        )
        require_container_quiescence(container_id)
        source_git = root / ".git"
        if not source_git.is_dir() or source_git.is_symlink():
            raise RuntimeError("trusted baseline Git directory is missing or unsafe")
        git = shutil.which("git")
        if not git:
            raise RuntimeError("trusted host git executable is unavailable")
        clean_git = temp_root / "repo.git"
        home = temp_root / "home"
        template = temp_root / "template"
        home.mkdir()
        (home / "xdg").mkdir()
        template.mkdir()
        env = _git_environment(home, template)
        object_format_args = (
            ["--object-format=sha256"] if len(snapshot.anonymous_head) == 64 else []
        )
        timeout = _docker_timeout_from_env()
        _run_git(
            git,
            [
                "init",
                "--bare",
                "--quiet",
                *object_format_args,
                str(clean_git),
            ],
            env=env,
            timeout=timeout,
        )
        _copy_object_store(source_git, clean_git, len(snapshot.anonymous_head))
        common = [f"--git-dir={clean_git}"]
        _run_git(
            git,
            [*common, "cat-file", "-e", f"{snapshot.anonymous_head}^{{commit}}"],
            env=env,
            timeout=timeout,
        )
        tree = subprocess.run(
            [git, *common, "rev-parse", f"{snapshot.anonymous_head}^{{tree}}"],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        if tree.returncode != 0 or tree.stdout.strip().lower() != snapshot.base_tree:
            raise RuntimeError("trusted host base tree does not match snapshot evidence")
        shutil.rmtree(root)
        return TrustedPatchBaseline(
            snapshot=snapshot,
            temporary_directory=temporary_directory,
            git_dir=clean_git,
            archive_sha256=archive_sha,
            archive_bytes=archive_bytes,
            archive_entries=entries,
            extracted_bytes=extracted_bytes,
        )
    except BaseException:
        temporary_directory.cleanup()
        raise


def extract_patch_trusted(
    container_id: str,
    baseline: TrustedPatchBaseline,
) -> tuple[str, TrustedPatchExtraction]:
    """Copy a quiet container and diff it against its pre-Solver anonymous base."""
    require_container_quiescence(container_id)
    with tempfile.TemporaryDirectory(prefix="opencollab-workspace-copy-") as temp:
        root = Path(temp) / "workspace"
        root.mkdir()
        archive_sha, archive_bytes, entries, extracted = _copy_workspace_archive(
            container_id,
            root,
        )
        require_container_quiescence(container_id)
        patch = _extract_patch_from_copy(root, baseline)
    encoded = patch.encode("utf-8", errors="surrogatepass")
    proof = TrustedPatchExtraction(
        fixed_anonymous_base=baseline.snapshot.anonymous_head,
        base_tree=baseline.snapshot.base_tree,
        baseline_archive_sha256=baseline.archive_sha256,
        baseline_archive_bytes=baseline.archive_bytes,
        baseline_archive_entries=baseline.archive_entries,
        baseline_extracted_bytes=baseline.extracted_bytes,
        workspace_archive_sha256=archive_sha,
        workspace_archive_bytes=archive_bytes,
        workspace_archive_entries=entries,
        workspace_extracted_bytes=extracted,
        patch_sha256=hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
    )
    return patch, proof


__all__ = [
    "TrustedPatchBaseline",
    "TrustedPatchExtraction",
    "extract_patch_trusted",
    "prepare_trusted_patch_baseline",
]
