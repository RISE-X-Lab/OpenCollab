from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEBENCH_DIR = _REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

import gen_prediction_snapshot as snapshot  # noqa: E402
import gen_prediction_snapshot_config as snapshot_config  # noqa: E402
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


def _repository_with_filter_target(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitattributes").write_text("*.py filter=snapshot-sidecar\n", encoding="utf-8")
    (repo / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "app.py").write_text("VALUE = 'hidden target'\n", encoding="utf-8")
    _commit(repo, "hidden target")
    return repo, base


def _write_git_config(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for key, value in values.items():
        subprocess.run(
            ["git", "config", "--file", str(path), key, value],
            capture_output=True,
            text=True,
            check=True,
        )


def _install_untrusted_extension(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extension: str,
    scope: str,
    sidecar: Path,
) -> None:
    if extension == "filter":
        values = {
            "filter.snapshot-sidecar.clean": f"tee {shlex.quote(str(sidecar))}",
            "filter.snapshot-sidecar.smudge": f"tee {shlex.quote(str(sidecar))}",
            "filter.snapshot-sidecar.required": "true",
        }
    else:
        hooks = tmp_path / f"{scope}-hooks"
        hooks.mkdir()
        hook = hooks / "post-commit"
        hook.write_text(
            "#!/bin/sh\n" f"printf '%s\\n' leaked > {shlex.quote(str(sidecar))}\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        values = {"core.hooksPath": str(hooks)}

    if scope == "local-include":
        included = tmp_path / "local-included-malicious.gitconfig"
        sidecar.write_text("embedded gold answer\n", encoding="utf-8")
        values["core.attributesFile"] = str(sidecar)
        _write_git_config(included, values)
        _git(repo, "config", "include.path", os.path.relpath(included, repo / ".git"))
        return
    if scope == "local":
        for key, value in values.items():
            _git(repo, "config", key, value)
        return
    if scope == "global":
        home = tmp_path / "malicious-home"
        xdg = tmp_path / "malicious-xdg"
        included = tmp_path / "included-malicious.gitconfig"
        _write_git_config(included, values)
        _write_git_config(home / ".gitconfig", {"include.path": str(included)})
        _write_git_config(xdg / "git" / "config", {"include.path": str(included)})
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        return
    system_config = tmp_path / "malicious-system.gitconfig"
    _write_git_config(system_config, values)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))


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
    assert not (repo / ".cache").exists()


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


def test_snapshot_removes_ignored_nested_git_metadata(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    ignored = repo / ".cache" / "nested-repo"
    ignored.mkdir()
    _git(ignored, "init", "-q")

    snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert not ignored.exists()


def test_snapshot_rejects_ignored_symlink_to_external_answer_repository(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    answers = tmp_path / "answers"
    answers.mkdir()
    _git(answers, "init", "-q")
    (answers / "gold.patch").write_text("secret answer\n", encoding="utf-8")
    _commit(answers, "target answer")
    (repo / ".cache" / "answers").symlink_to(answers, target_is_directory=True)

    with pytest.raises(snapshot_container.SnapshotSetupError, match="containment root"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_removes_ignored_symlink_to_external_non_git_executable(tmp_path):
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
    assert not link.exists()
    assert executable.exists()


def test_snapshot_removes_preexisting_ignored_answer_sidecar(tmp_path):
    repo, base, base_tree, _target = _repository_with_hidden_target(tmp_path)
    answer = repo / ".cache" / "gold-answer.patch"
    answer.write_text("hidden fix\n", encoding="utf-8")

    evidence = snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert evidence["base_tree"] == base_tree
    assert not answer.exists()


def test_snapshot_removes_ignored_bare_answer_repository(tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    bare = repo / ".cache" / "answers.git"
    _git(repo, "init", "-q", "--bare", str(bare))

    snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert not bare.exists()


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


@pytest.mark.parametrize("scope", ["local", "local-include", "global", "system"])
@pytest.mark.parametrize("extension", ["filter", "hook"])
def test_snapshot_blocks_untrusted_git_extensions(monkeypatch, tmp_path, scope, extension):
    repo, base = _repository_with_filter_target(tmp_path)
    sidecar = tmp_path / f"{scope}-{extension}-sidecar"
    _install_untrusted_extension(
        repo,
        tmp_path,
        monkeypatch,
        extension=extension,
        scope=scope,
        sidecar=sidecar,
    )

    if scope == "system":
        with pytest.raises(snapshot_container.SnapshotSetupError, match="unsafe Git environment"):
            snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)
    else:
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)
        assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"
        assert _git(repo, "config", "--local", "--get", "core.hooksPath").stdout.strip() == "/dev/null"
        assert (
            _git(
                repo,
                "config",
                "--local",
                "--get",
                "filter.snapshot-sidecar.smudge",
                check=False,
            ).returncode
            != 0
        )
        _git(repo, "checkout", "--", "app.py")
        _git(
            repo,
            "-c",
            "user.name=Snapshot Test",
            "-c",
            "user.email=snapshot@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "default environment probe",
        )
        if scope == "local-include":
            assert not (tmp_path / "local-included-malicious.gitconfig").exists()
            assert not (tmp_path / f"{scope}-hooks").exists()
    assert not sidecar.exists()


@pytest.mark.parametrize("link_kind", ["symlink", "symlink-parent"])
def test_snapshot_removes_local_include_real_target(tmp_path, link_kind):
    repo, base = _repository_with_filter_target(tmp_path)
    gold = tmp_path / "gold.gitconfig"
    hooks = tmp_path / "gold-hooks"
    hooks.mkdir()
    (hooks / "post-commit").write_text("SECRET_GOLD_DIFF\n", encoding="utf-8")
    _write_git_config(gold, {"gold.patch": "SECRET_GOLD_DIFF", "core.hooksPath": str(hooks)})
    if link_kind == "symlink":
        include = repo / ".git" / "included.gitconfig"
        include.symlink_to(gold)
    else:
        include_parent = repo / ".git" / "included"
        include_parent.symlink_to(tmp_path, target_is_directory=True)
        include = include_parent / gold.name
    _git(repo, "config", "include.path", os.path.relpath(include, repo / ".git"))

    snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert not gold.exists()
    assert not hooks.exists()


def test_snapshot_rejects_hardlinked_local_include(tmp_path):
    repo, base = _repository_with_filter_target(tmp_path)
    gold = tmp_path / "gold.gitconfig"
    _write_git_config(gold, {"gold.patch": "SECRET_GOLD_DIFF"})
    include = repo / ".git" / "included.gitconfig"
    os.link(gold, include)
    _git(repo, "config", "include.path", include.name)

    with pytest.raises(snapshot_container.SnapshotSetupError, match="multiple hard links"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_rejects_hardlinked_main_config(tmp_path):
    repo, base = _repository_with_filter_target(tmp_path)
    os.link(repo / ".git" / "config", tmp_path / "external-main.gitconfig")

    with pytest.raises(snapshot_container.SnapshotSetupError, match="multiple hard links"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_removes_comment_only_default_config_candidates(monkeypatch, tmp_path):
    repo, base = _repository_with_filter_target(tmp_path)
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    system = tmp_path / "etc" / "gitconfig"
    candidates = (
        home / ".gitconfig",
        xdg / "git" / "config",
        system,
        repo / ".git" / "config.worktree",
    )
    for path in candidates:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# SECRET_GOLD_DIFF remains invisible to --show-origin\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setattr(snapshot_config, "_discover_system_config_path", lambda _repo: system)

    snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert all(not path.exists() for path in candidates)


def test_snapshot_removes_comment_only_include_target(tmp_path):
    repo, base = _repository_with_filter_target(tmp_path)
    included = tmp_path / "comment-only-gold.gitconfig"
    included.write_text("# SECRET_GOLD_DIFF\n", encoding="utf-8")
    _git(repo, "config", "include.path", os.path.relpath(included, repo / ".git"))

    snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert not included.exists()


def test_snapshot_rejects_hardlinked_worktree_config(tmp_path):
    repo, base = _repository_with_filter_target(tmp_path)
    worktree_config = repo / ".git" / "config.worktree"
    worktree_config.write_text("# SECRET_GOLD_DIFF\n", encoding="utf-8")
    os.link(worktree_config, tmp_path / "external-worktree.gitconfig")

    with pytest.raises(snapshot_container.SnapshotSetupError, match="multiple hard links"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_rejects_hardlinked_referenced_artifact(tmp_path):
    repo, base = _repository_with_filter_target(tmp_path)
    attributes = tmp_path / "gold.attributes"
    attributes.write_text("*.py filter=gold-answer\n", encoding="utf-8")
    os.link(attributes, tmp_path / "external-gold.attributes")
    _git(repo, "config", "core.attributesFile", str(attributes))

    with pytest.raises(snapshot_container.SnapshotSetupError, match="multiple hard links"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX special files")
def test_snapshot_rejects_special_referenced_artifact(tmp_path):
    repo, base = _repository_with_filter_target(tmp_path)
    attributes = tmp_path / "gold.attributes"
    os.mkfifo(attributes)
    _git(repo, "config", "core.attributesFile", str(attributes))

    with pytest.raises(snapshot_container.SnapshotSetupError, match="not safe to remove"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_rejects_an_unexpected_ordinary_sidecar(monkeypatch, tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    original_run_git = snapshot_container._run_git
    injected = False

    def injecting_run_git(repo_path, *args, **kwargs):
        nonlocal injected
        result = original_run_git(repo_path, *args, **kwargs)
        if args and args[0] == "commit-tree" and not injected:
            (repo_path / "unexpected-sidecar.txt").write_text("leaked\n", encoding="utf-8")
            injected = True
        return result

    monkeypatch.setattr(snapshot_container, "_run_git", injecting_run_git)

    with pytest.raises(snapshot_container.SnapshotSetupError, match="unexpected ordinary sidecar"):
        snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)


def test_snapshot_keeps_expected_base_out_of_git_process_arguments(monkeypatch, tmp_path):
    repo, base, _base_tree, _target = _repository_with_hidden_target(tmp_path)
    original_run = subprocess.run
    git_argv: list[tuple[str, ...]] = []

    def recording_run(args, *positional, **kwargs):
        if isinstance(args, list) and args and args[0] == "git":
            git_argv.append(tuple(str(value) for value in args))
        return original_run(args, *positional, **kwargs)

    monkeypatch.setattr(snapshot_container.subprocess, "run", recording_run)

    snapshot_container.create_solver_snapshot(repo, base, filesystem_root=tmp_path)

    assert git_argv
    assert all(base not in argument for argv in git_argv for argument in argv)


def test_host_wrapper_installs_helper_and_validates_evidence(monkeypatch):
    calls = []
    stdin_calls = []
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

    def fake_docker_with_stdin(*args, input_text):
        stdin_calls.append((args, input_text))
        return subprocess.CompletedProcess(args, 0, json.dumps(evidence), "")

    monkeypatch.setattr(snapshot, "_docker", fake_docker)
    monkeypatch.setattr(snapshot, "_docker_with_stdin", fake_docker_with_stdin)

    result = snapshot.prepare_solver_git_snapshot("container", "c" * 40)

    assert calls == [
        (
            "cp",
            str(snapshot._CONTAINER_CONFIG_HELPER_SOURCE),
            f"container:{snapshot._CONTAINER_CONFIG_HELPER}",
        ),
        ("cp", str(snapshot._CONTAINER_HELPER_SOURCE), f"container:{snapshot._CONTAINER_HELPER}"),
    ]
    assert stdin_calls[0][0][:5] == ("exec", "-i", "container", "python3", snapshot._CONTAINER_HELPER)
    assert stdin_calls[0][1] == "c" * 40 + "\n"
    assert all("c" * 40 not in argument for argument in stdin_calls[0][0])
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
        ("GIT_TRACE", "/tmp/sidecar"),
        ("GIT_DIR", ""),
    ],
)
def test_snapshot_rejects_inherited_git_redirection_environment(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(snapshot_container.SnapshotSetupError, match="unsafe Git environment"):
        snapshot_container._clean_git_env(tmp_path / "trusted")


def test_snapshot_clean_git_env_fixes_all_outer_config_locations(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "untrusted-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "untrusted-xdg"))

    env = snapshot_container._clean_git_env(tmp_path / "trusted")

    assert env["HOME"] == str(tmp_path / "trusted" / "home")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "trusted" / "xdg")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_snapshot_git_config_audit_stops_at_its_output_bound(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "snapshot.large", "x" * 4096)
    monkeypatch.setattr(snapshot_config, "_MAX_GIT_OUTPUT_BYTES", 256)

    with pytest.raises(snapshot_container.SnapshotSetupError, match="exceeded its output bound"):
        snapshot_config._default_git_config_records(repo)


def test_snapshot_git_config_audit_supports_git_without_show_scope(monkeypatch, tmp_path):
    calls = []

    def fake_bounded(_repo, *args, env=None):
        calls.append((args, env))
        if "--show-scope" in args:
            return subprocess.CompletedProcess(
                ["git", "config"],
                129,
                b"",
                b"error: unknown option `show-scope'\n",
            )
        return subprocess.CompletedProcess(
            ["git", "config"],
            0,
            b"file:.git/config\0core.filemode\ntrue\0",
            b"",
        )

    monkeypatch.setattr(snapshot_config, "_bounded_git_config", fake_bounded)

    assert snapshot_config._default_git_config_records(tmp_path) == [
        ("unknown", "file:.git/config", "core.filemode", "true")
    ]
    assert len(calls) == 2
    assert "--show-scope" in calls[0][0]
    assert "--show-scope" not in calls[1][0]


def test_snapshot_config_helper_avoids_python39_string_helpers(tmp_path):
    assert snapshot_config._origin_path("file:.git/config", tmp_path) == (
        tmp_path / ".git/config"
    ).absolute()
    source = Path(snapshot_config.__file__).read_text(encoding="utf-8")
    assert ".removeprefix(" not in source


def test_snapshot_uses_legacy_compatible_git_init_for_sha1():
    assert snapshot_container._git_init_args("sha1") == ("init", "-q")
    assert snapshot_container._git_init_args("sha256") == (
        "init",
        "-q",
        "--object-format=sha256",
    )
