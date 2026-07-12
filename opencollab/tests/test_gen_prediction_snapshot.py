from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEBENCH_DIR = _REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

import gen_prediction_snapshot as snapshot  # noqa: E402
import gen_prediction_snapshot_container as snapshot_container  # noqa: E402


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=Snapshot Test",
        "-c",
        "user.email=snapshot@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _repository_with_hidden_target(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(".cache/\nlegacy.txt\n", encoding="utf-8")
    (repo / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    (repo / "legacy.txt").write_text("tracked despite later ignore rules\n", encoding="utf-8")
    _git(repo, "add", "-f", "legacy.txt")
    base = _commit(repo, "base")
    base_tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    (repo / ".cache").mkdir()
    (repo / ".cache" / "dependency.bin").write_bytes(b"dependency")
    (repo / "app.py").write_text("VALUE = 'gold answer'\n", encoding="utf-8")
    target = _commit(repo, "hidden target fix")
    return repo, base, base_tree, target


def test_snapshot_removes_target_history_and_preserves_base_tree(tmp_path):
    repo, base, base_tree, target = _repository_with_hidden_target(tmp_path)

    evidence = snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert evidence["enabled"] is True
    assert evidence["base_tree"] == base_tree
    assert evidence["commit_count"] == 1
    assert evidence["remote_count"] == 0
    assert evidence["extra_git_metadata"] == 0
    assert evidence["removed_git_metadata"] == 0
    assert _git(repo, "cat-file", "-e", f"{target}^{{commit}}", check=False).returncode != 0
    assert _git(repo, "rev-list", "--all", "--count").stdout.strip() == "1"
    assert len(_git(repo, "rev-list", "--parents", "-1", "HEAD").stdout.split()) == 1
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"
    assert (repo / ".cache" / "dependency.bin").read_bytes() == b"dependency"


def test_snapshot_keeps_patch_extraction_relative_to_anonymous_head(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)
    (repo / "app.py").write_text("VALUE = 'solver fix'\n", encoding="utf-8")
    (repo / "new.py").write_text("NEW = True\n", encoding="utf-8")

    _git(repo, "add", "-A")
    patch = _git(repo, "diff", "--cached", "--binary").stdout

    assert "-VALUE = 'base'" in patch
    assert "+VALUE = 'solver fix'" in patch
    assert "diff --git a/new.py b/new.py" in patch


def test_snapshot_ignores_replace_refs_when_selecting_the_base_tree(tmp_path):
    repo, base, base_tree, target = _repository_with_hidden_target(tmp_path)
    _git(repo, "replace", base, target)

    evidence = snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert evidence["base_tree"] == base_tree
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"
    assert _git(repo, "cat-file", "-e", target, check=False).returncode != 0


def test_snapshot_rejects_additional_visible_git_metadata(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    ignored = repo / ".cache" / "nested-repo"
    ignored.mkdir()
    _git(ignored, "init", "-q")

    with pytest.raises(snapshot_container.SnapshotSetupError, match="additional Git metadata"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_rejects_ignored_symlink_to_external_answer_repository(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    answers = tmp_path / "answers"
    answers.mkdir()
    _git(answers, "init", "-q")
    (answers / "gold.patch").write_text("secret answer\n", encoding="utf-8")
    _commit(answers, "target answer")
    (repo / ".cache" / "answers").symlink_to(answers, target_is_directory=True)

    with pytest.raises(snapshot_container.SnapshotSetupError, match="additional Git metadata"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_allows_symlink_to_external_non_git_executable(tmp_path):
    repo, _base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    base = _commit(repo, "ignore installed dependencies")
    base_tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "node_modules" / "package" / "build").mkdir(parents=True)
    link = repo / "node_modules" / "package" / "build" / "python3"
    link.symlink_to(executable)

    evidence = snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert evidence["base_tree"] == base_tree
    assert link.resolve() == executable


def test_snapshot_rejects_ignored_bare_answer_repository(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    bare = repo / ".cache" / "answers.git"
    _git(repo, "init", "-q", "--bare", str(bare))

    with pytest.raises(snapshot_container.SnapshotSetupError, match="additional Git metadata"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_removes_answer_repository_elsewhere_in_visible_filesystem(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    answers = tmp_path / "unrelated-path" / "answers"
    answers.mkdir(parents=True)
    _git(answers, "init", "-q")
    (answers / "gold.patch").write_text("secret answer\n", encoding="utf-8")
    _commit(answers, "target answer")

    evidence = snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert evidence["removed_git_metadata"] == 1
    assert not answers.exists()


def test_snapshot_removes_loose_only_object_store_elsewhere_in_visible_filesystem(tmp_path):
    repo, base, _base_tree, target = _repository_with_hidden_target(tmp_path)
    source_object = repo / ".git" / "objects" / target[:2] / target[2:]
    object_store = tmp_path / "cache" / "answer-db"
    loose_dir = object_store / target[:2]
    loose_dir.mkdir(parents=True)
    (loose_dir / target[2:]).write_bytes(source_object.read_bytes())
    readable = subprocess.run(
        ["git", "cat-file", "-e", f"{target}^{{commit}}"],
        cwd=repo,
        env={"GIT_DIR": str(repo / ".git"), "GIT_OBJECT_DIRECTORY": str(object_store)},
        check=False,
    )
    assert readable.returncode == 0

    evidence = snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert evidence["removed_git_metadata"] == 1
    assert not object_store.exists()


def test_snapshot_removes_pack_only_object_store_elsewhere_in_visible_filesystem(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    object_store = tmp_path / "cache" / "pack-cache"
    pack_dir = object_store / "pack"
    pack_dir.mkdir(parents=True)
    pack_id = "a" * 40
    (pack_dir / f"pack-{pack_id}.pack").write_bytes(b"pack data")
    (pack_dir / f"pack-{pack_id}.idx").write_bytes(b"index data")

    evidence = snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert evidence["removed_git_metadata"] == 1
    assert not object_store.exists()


def test_snapshot_fails_closed_for_top_level_external_repository(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    answers = tmp_path / "answers"
    answers.mkdir()
    _git(answers, "init", "-q")

    with pytest.raises(snapshot_container.SnapshotSetupError, match="containment root"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_rejects_external_git_directory(tmp_path):
    repo = tmp_path / "repo"
    metadata = tmp_path / "metadata"
    _git(tmp_path, "init", "-q", f"--separate-git-dir={metadata}", str(repo))
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    base = _commit(repo, "base")

    with pytest.raises(snapshot_container.SnapshotSetupError, match="external Git directories"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_preserves_gitlink_without_copying_submodule_objects(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    submodule_path = Path("vendor/infogami")
    (repo / submodule_path).mkdir(parents=True)
    _git(repo, "add", "app.py")
    submodule_commit = "1" * 40
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{submodule_commit},{submodule_path}",
    )
    _git(
        repo,
        "-c",
        "user.name=Snapshot Test",
        "-c",
        "user.email=snapshot@example.invalid",
        "commit",
        "-m",
        "base with gitlink",
    )
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    base_tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    module_git = repo / ".git" / "modules" / submodule_path
    _git(repo, "init", "-q", "--bare", str(module_git))
    (repo / submodule_path / ".git").write_text(
        "gitdir: ../../.git/modules/vendor/infogami\n",
        encoding="utf-8",
    )
    (repo / submodule_path / "answer.txt").write_text(
        "submodule working tree must not remain visible\n",
        encoding="utf-8",
    )

    evidence = snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert evidence["base_tree"] == base_tree
    assert evidence["removed_git_metadata"] == 1
    assert not (repo / submodule_path).exists()
    assert _git(repo, "ls-tree", "HEAD", str(submodule_path)).stdout.startswith(
        f"160000 commit {submodule_commit}"
    )
    assert _git(repo, "diff", "--binary").stdout == ""
    assert _git(repo, "cat-file", "-e", submodule_commit, check=False).returncode != 0


def test_host_wrapper_installs_helper_and_validates_evidence(monkeypatch):
    calls = []
    evidence = {
        "enabled": True,
        "anonymous_head": "a" * 40,
        "base_tree": "b" * 40,
        "commit_count": 1,
        "remote_count": 0,
        "extra_git_metadata": 0,
        "removed_git_metadata": 0,
    }

    def fake_docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(evidence), "")

    monkeypatch.setattr(snapshot, "_docker", fake_docker)

    result = snapshot.prepare_solver_git_snapshot("container", "c" * 40)

    assert calls[0][0] == "cp"
    assert calls[1][:4] == ("exec", "container", "python3", snapshot._CONTAINER_HELPER)
    assert result.as_dict() == evidence


def test_host_wrapper_rejects_unproven_evidence():
    invalid = {
        "enabled": True,
        "anonymous_head": "a" * 40,
        "base_tree": "b" * 40,
        "commit_count": 2,
        "remote_count": 0,
        "extra_git_metadata": 0,
        "removed_git_metadata": 0,
    }

    with pytest.raises(RuntimeError, match="integrity verification failed"):
        snapshot._parse_snapshot_output(json.dumps(invalid))


def test_anonymous_solver_ids_are_unique_and_opaque():
    first = snapshot.anonymous_solver_task_id()
    second = snapshot.anonymous_solver_task_id()

    assert first != second
    assert first.startswith("solver-")
    assert "instance_" not in first
    assert len(first) == len("solver-") + 32


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/hidden/objects"),
        ("GIT_COMMON_DIR", "/hidden/common"),
        ("GIT_INDEX_FILE", "/hidden/index"),
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "core.alternateRefsCommand"),
        ("GIT_CONFIG_VALUE_0", "cat /hidden/refs"),
    ],
)
def test_snapshot_rejects_inherited_git_redirection_environment(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(snapshot_container.SnapshotSetupError, match="unsafe Git environment"):
        snapshot_container._clean_git_env()


def test_snapshot_uses_legacy_compatible_git_init_for_sha1():
    assert snapshot_container._git_init_args("sha1") == ("init", "-q")
    assert snapshot_container._git_init_args("sha256") == (
        "init",
        "-q",
        "--object-format=sha256",
    )
