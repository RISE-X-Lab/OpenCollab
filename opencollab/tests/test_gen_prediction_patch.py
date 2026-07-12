from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEBENCH_DIR = _REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

import gen_prediction_patch as patcher  # noqa: E402
from gen_prediction_snapshot import SolverGitSnapshot  # noqa: E402
from opencollab.harness.swe_generation_proof import (  # noqa: E402
    current_generation_proof_valid,
    current_generation_summary_proof_valid,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _baseline(
    tmp_path: Path,
    *,
    object_format: str = "sha1",
) -> tuple[patcher.TrustedPatchBaseline, Path]:
    repo = tmp_path / "base"
    repo.mkdir()
    init_args = ["init", "--quiet"]
    if object_format == "sha256":
        init_args.append("--object-format=sha256")
    _git(repo, *init_args)
    _git(repo, "config", "user.name", "Harness Test")
    _git(repo, "config", "user.email", "harness@example.invalid")
    (repo / "source.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "--quiet", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    bare = tmp_path / "trusted.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(repo), str(bare))
    snapshot = SolverGitSnapshot(head, tree, 1, 0, 0, 1)
    return (
        patcher.TrustedPatchBaseline(
            snapshot=snapshot,
            temporary_directory=None,
            git_dir=bare,
            archive_sha256="a" * 64,
            archive_bytes=100,
            archive_entries=2,
            extracted_bytes=50,
        ),
        repo,
    )


def _worktree(repo: Path, destination: Path) -> Path:
    shutil.copytree(repo, destination, symlinks=True)
    return destination


def test_host_extractor_ignores_solver_git_config_head_index_and_shell(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "worktree")
    (worktree / "source.txt").write_text("fixed\n", encoding="utf-8")
    (worktree / ".git" / "config").write_text(
        "[diff]\n\texternal = /bin/false\n",
        encoding="utf-8",
    )
    (worktree / ".git" / "HEAD").write_text("ref: refs/heads/forged\n", encoding="utf-8")
    (worktree / ".bash_profile").write_text("exit 99\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "-base" in extracted
    assert "+fixed" in extracted
    assert ".bash_profile" in extracted


def test_host_extractor_captures_solver_commits_against_fixed_base(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "committed")
    (worktree / "source.txt").write_text("committed repair\n", encoding="utf-8")
    _git(worktree, "add", "source.txt")
    _git(worktree, "commit", "--quiet", "-m", "solver commit")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "+committed repair" in extracted


def test_host_extractor_supports_sha256_baselines(tmp_path: Path) -> None:
    try:
        baseline, repo = _baseline(tmp_path, object_format="sha256")
    except subprocess.CalledProcessError:
        pytest.skip("host Git does not support SHA-256 repositories")
    worktree = _worktree(repo, tmp_path / "sha256-worktree")
    (worktree / "source.txt").write_text("sha256 repair\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert len(baseline.snapshot.anonymous_head) == 64
    assert "+sha256 repair" in extracted


def test_host_extractor_forces_new_files_ignored_by_solver(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "ignored")
    (worktree / ".gitignore").write_text("*.py\n", encoding="utf-8")
    (worktree / "repair.py").write_text("fixed = True\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert "repair.py" in extracted
    assert "+fixed = True" in extracted


def test_host_extractor_excludes_opencollab_state(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "state")
    state = worktree / ".opencollab"
    state.mkdir()
    (state / "events.jsonl").write_text("secret\n", encoding="utf-8")

    extracted = patcher._extract_patch_from_copy(worktree, baseline)

    assert ".opencollab" not in extracted


def test_host_extractor_rejects_retirement_tombstones(tmp_path: Path) -> None:
    baseline, repo = _baseline(tmp_path)
    worktree = _worktree(repo, tmp_path / "retired")
    nested = worktree / "src"
    nested.mkdir()
    (nested / ".opencollab-retired-deadbeef").write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError, match="harness-owned"):
        patcher._extract_patch_from_copy(worktree, baseline)


@pytest.mark.parametrize(
    ("name", "kind", "linkname"),
    [
        ("../escape", "file", ""),
        ("/absolute", "file", ""),
        ("hard", "hardlink", "source.txt"),
        ("fifo", "fifo", ""),
        ("link", "symlink", "../../escape"),
    ],
)
def test_archive_member_rejects_unsafe_entries(
    tmp_path: Path,
    name: str,
    kind: str,
    linkname: str,
) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.linkname = linkname
        if kind == "hardlink":
            member.type = tarfile.LNKTYPE
        elif kind == "fifo":
            member.type = tarfile.FIFOTYPE
        elif kind == "symlink":
            member.type = tarfile.SYMTYPE
        else:
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
            member = None
        if member is not None:
            archive.addfile(member)
    payload.seek(0)
    with tarfile.open(fileobj=payload, mode="r:") as archive:
        member = archive.next()
        assert member is not None
        with pytest.raises(RuntimeError):
            patcher._extract_member(archive, member, tmp_path / "out", extracted_bytes=0)


def test_archive_member_rejects_duplicate_path(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    (root / "same").write_text("first", encoding="utf-8")
    member = tarfile.TarInfo("same")
    member.size = 1
    archive = tarfile.open(fileobj=io.BytesIO(b""), mode="w")
    try:
        with pytest.raises(RuntimeError, match="duplicate"):
            patcher._extract_member(archive, member, root, extracted_bytes=0)
    finally:
        archive.close()


def _proof(snapshot: SolverGitSnapshot, patch: str) -> dict:
    encoded = patch.encode("utf-8")
    return patcher.TrustedPatchExtraction(
        fixed_anonymous_base=snapshot.anonymous_head,
        base_tree=snapshot.base_tree,
        baseline_archive_sha256="a" * 64,
        baseline_archive_bytes=100,
        baseline_archive_entries=2,
        baseline_extracted_bytes=50,
        workspace_archive_sha256="b" * 64,
        workspace_archive_bytes=120,
        workspace_archive_entries=3,
        workspace_extracted_bytes=60,
        patch_sha256=hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
    ).as_dict()


def test_current_proof_binds_snapshot_patch_and_summary() -> None:
    snapshot = SolverGitSnapshot("a" * 40, "b" * 40, 1, 0, 0, 1)
    patch = "diff --git a/a b/a\n"
    metric = {
        "solver_git_snapshot": snapshot.as_dict(),
        "trusted_patch_extraction": _proof(snapshot, patch),
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
    }

    assert current_generation_proof_valid(metric, patch)
    assert current_generation_summary_proof_valid(metric)
    assert not current_generation_proof_valid(metric, patch + "x")
    metric["trusted_patch_extraction"]["workspace_archive_entries"] = True
    assert not current_generation_summary_proof_valid(metric)


def test_empty_patch_requires_exact_zero_byte_proof() -> None:
    snapshot = SolverGitSnapshot("a" * 40, "b" * 40, 1, 0, 0, 0)
    metric = {
        "solver_git_snapshot": snapshot.as_dict(),
        "trusted_patch_extraction": _proof(snapshot, ""),
        "patch_sha256": hashlib.sha256(b"").hexdigest(),
    }
    assert current_generation_proof_valid(metric, "")
    metric["trusted_patch_extraction"]["patch_bytes"] = 1
    assert not current_generation_proof_valid(metric, "")


def test_extract_requires_quiescence_before_and_after_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline, _repo = _baseline(tmp_path)
    calls = 0

    def quiesce(_container: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("still running")

    def copy(_container: str, root: Path) -> tuple[str, int, int, int]:
        (root / ".git").mkdir()
        return "c" * 64, 10, 1, 0

    monkeypatch.setattr(patcher, "require_container_quiescence", quiesce)
    monkeypatch.setattr(patcher, "_copy_workspace_archive", copy)

    with pytest.raises(RuntimeError, match="still running"):
        patcher.extract_patch_trusted("cid", baseline)
    assert calls == 2


def test_baseline_cleanup_removes_trusted_temporary_directory(tmp_path: Path) -> None:
    temp = patcher.tempfile.TemporaryDirectory(dir=tmp_path)
    location = Path(temp.name)
    baseline = patcher.TrustedPatchBaseline(
        snapshot=SolverGitSnapshot("a" * 40, "b" * 40, 1, 0, 0, 0),
        temporary_directory=temp,
        git_dir=location / "repo.git",
        archive_sha256="c" * 64,
        archive_bytes=1,
        archive_entries=1,
        extracted_bytes=0,
    )
    baseline.cleanup()
    assert not location.exists()
