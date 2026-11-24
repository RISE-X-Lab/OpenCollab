from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from opencollab.adapters import _docker_snapshot as snapshot_module
from opencollab.adapters._docker_snapshot import _import_snapshot
from opencollab.adapters.docker_isolation import DockerIsolationPool
from opencollab.adapters.env import DockerEnvironment, LocalEnvironment
from opencollab.domain.docker import DockerMount, DockerOptions
from tests.support.asyncio_test_support import assert_cancel_reason
from tests.support.docker_env_test_support import CONTAINER_ID, FakeDocker, _patch, _result


def _git(workspace: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(workspace), "-c", "user.name=Tester", "-c", "user.email=test@example.com", *args],
        text=True, stderr=subprocess.PIPE,
    ).strip()


def _repository(workspace: Path, *, object_format: str = "sha1") -> None:
    workspace.mkdir()
    _git(workspace, "init", "--quiet", f"--object-format={object_format}")
    (workspace / "answer.py").write_text("answer = 1\n")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "--quiet", "-m", "initial")


def _shallow_repository(workspace: Path, *, object_format: str = "sha1") -> None:
    origin = workspace.with_name(workspace.name + "-origin")
    _repository(origin, object_format=object_format)
    (origin / "answer.py").write_text("answer = 2\n")
    _git(origin, "commit", "--quiet", "-am", "second")
    _git(origin, "tag", "v1.2")
    _git(origin, "clone", "--quiet", "--depth=1", origin.as_uri(), str(workspace))


class _LocalImportTarget(LocalEnvironment):
    def __init__(self, workspace):
        Path(workspace).mkdir()
        super().__init__(workspace)

    async def setup(self, mount_dir=None):
        return CONTAINER_ID

    async def _docker(self, *args, **kwargs):
        if args[0] == "exec":
            assert args[1:5] == ("--user", "0", "--", CONTAINER_ID)
            assert args[5:7] == ("chown", "-Rh")
            return _result()
        assert args[:2] == ("cp", "--")
        source, destination = args[2:]
        assert destination == f"{CONTAINER_ID}:{self.workspace}"
        shutil.copytree(source, self.workspace, symlinks=True, dirs_exist_ok=True)
        return _result()


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_import_rebuilds_linked_git_metadata_and_keeps_physical_contents(
    tmp_path: Path, monkeypatch, object_format: str,
) -> None:
    source = tmp_path / "source"
    _repository(source, object_format=object_format)
    _git(source, "tag", "v1.0.0")
    _git(source, "tag", "-a", "v1.1.0", "-m", "release")
    linked = tmp_path / "linked"
    _git(source, "worktree", "add", "--quiet", "--detach", str(linked))
    (linked / "answer.py").write_text("answer = 2\r\n")
    (linked / "new.bin").write_bytes(b"\0\xff")
    nested = linked / "subdirectory"
    nested.mkdir()
    (nested / "file.txt").write_text("inside\n")
    target = _LocalImportTarget(str(tmp_path / "target"))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256" if object_format == "sha1" else "sha1")
    target_workspace = await _import_snapshot(LocalEnvironment(str(nested)), target)
    assert target_workspace == str(Path(target.workspace) / "subdirectory")
    assert (Path(target.workspace) / ".git").is_dir()
    assert (Path(target.workspace) / "answer.py").read_bytes() == b"answer = 2\r\n"
    assert (Path(target.workspace) / "new.bin").read_bytes() == b"\0\xff"
    assert _git(Path(target.workspace), "rev-parse", "--show-object-format") == object_format
    assert _git(Path(target.workspace), "describe", "--tags", "--dirty") == "v1.1.0-dirty"
    assert _git(Path(target.workspace), "cat-file", "-t", "refs/tags/v1.0.0") == "commit"
    assert _git(Path(target.workspace), "cat-file", "-t", "refs/tags/v1.1.0") == "tag"
    _git(source, "worktree", "remove", "--force", str(linked))
    assert _git(Path(target.workspace), "show", "HEAD:answer.py") == "answer = 1"
    assert "answer.py" in _git(Path(target.workspace), "diff", "--name-only")
    assert not list(linked.glob(".docker-import-*"))


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("layout", ["repository", "linked", "submodule"])
async def test_import_preserves_shallow_history(tmp_path: Path, object_format: str, layout: str) -> None:
    source = tmp_path / "source"
    _shallow_repository(source, object_format=object_format)
    workspace = source
    relative = "."
    if layout == "linked":
        workspace = tmp_path / "linked"
        _git(source, "worktree", "add", "--quiet", "--detach", str(workspace))
    elif layout == "submodule":
        workspace = tmp_path / "parent"
        _repository(workspace, object_format=object_format)
        _git(workspace, "-c", "protocol.file.allow=always", "submodule", "add", "--quiet", "--depth=1",
             source.as_uri(), "child")
        _git(workspace, "commit", "--quiet", "-am", "submodule")
        relative = "child"
    repository = workspace / relative
    shallow = Path(_git(repository, "rev-parse", "--path-format=absolute", "--git-path", "shallow"))
    boundary = shallow.read_bytes()
    target = _LocalImportTarget(str(tmp_path / "target"))
    await _import_snapshot(LocalEnvironment(str(workspace)), target)
    imported = Path(target.workspace) / relative
    assert _git(imported, "rev-parse", "--show-object-format") == object_format
    assert _git(imported, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(imported, "rev-list", "--count", "HEAD") == "1"
    assert _git(imported, "describe", "--tags") == "v1.2"
    assert _git(imported, "show", "HEAD:answer.py") == "answer = 2"
    assert shallow.read_bytes() == boundary
    shutil.rmtree(workspace)
    assert _git(imported, "rev-list", "--count", "HEAD") == "1"


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_import_preserves_initialized_submodule_without_source_git_directory(
    tmp_path: Path, object_format: str,
) -> None:
    module = tmp_path / "module"
    source = tmp_path / "source"
    _repository(module, object_format=object_format)
    _repository(source, object_format=object_format)
    _git(source, "-c", "protocol.file.allow=always", "submodule", "add", "--quiet", str(module), "child")
    _git(source, "commit", "--quiet", "-am", "submodule")
    (source / "child" / "answer.py").write_text("answer = 3\n")
    target = _LocalImportTarget(str(tmp_path / "target"))
    await _import_snapshot(LocalEnvironment(str(source)), target)
    shutil.rmtree(source)
    child = Path(target.workspace) / "child"
    assert _git(child, "show", "HEAD:answer.py") == "answer = 1"
    assert (child / "answer.py").read_text() == "answer = 3\n"
    assert "answer.py" in _git(child, "diff", "--name-only")


@pytest.mark.parametrize("stage", ["workspace-copy", "bundle-copy", "metadata-removal"])
async def test_cancelled_import_waits_for_host_io_before_removing_its_source(tmp_path, monkeypatch, stage):
    source = tmp_path / "source"
    _repository(source)
    pool = DockerIsolationPool(str(source))
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
    temporary = None

    def hold(operation, directory, *args, **kwargs):
        nonlocal temporary
        temporary = directory
        entered.set()
        try:
            assert resume.wait(5)
            return operation(*args, **kwargs)
        finally:
            finished.set()

    if stage == "workspace-copy":
        original = shutil._copytree

        def copytree(entries, src, dst, *args, **kwargs):
            if "opencollab-docker-import-" in str(dst) and not entered.is_set():
                return hold(original, Path(dst).parent, entries, src, dst, *args, **kwargs)
            return original(entries, src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "_copytree", copytree)
    elif stage == "bundle-copy":
        original = shutil.copyfile

        def copyfile(src, dst, *args, **kwargs):
            if Path(dst).name == "repository.bundle":
                return hold(original, Path(dst).parent, src, dst, *args, **kwargs)
            return original(src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "copyfile", copyfile)
    else:
        original = snapshot_module._remove_marker

        def remove_marker(path):
            return hold(original, path.parent, path)

        monkeypatch.setattr(snapshot_module, "_remove_marker", remove_marker)

    task = asyncio.create_task(pool.acquire("cancelled"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        candidate = Path(pool._owners[0].source_pools[0]._envs[0].workspace)
        task.cancel("first cancellation")
        await asyncio.sleep(0)
        task.cancel("later cancellation")
        await asyncio.sleep(0.05)
        assert not task.done()
        assert candidate.exists()
        assert temporary.exists()
        resume.set()
        with pytest.raises(asyncio.CancelledError) as failure:
            await task
        assert_cancel_reason(failure.value, "first cancellation")
        assert finished.is_set()
        assert not candidate.exists()
        assert not temporary.exists()
        assert not pool._owners
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)
        if entered.is_set():
            assert await asyncio.to_thread(finished.wait, 2)
        await pool.release()
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


async def test_import_tracks_changes_in_directory_without_git(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".gitignore").write_text("cache.txt\n")
    (source / "cache.txt").write_text("before\n")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    post_commit = hooks / "post-commit"
    post_commit.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    post_commit.chmod(0o700)
    configuration = tmp_path / "global-config"
    configuration.write_text(f"[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(configuration))
    _repository(tmp_path / "control")
    assert marker.exists()
    marker.unlink()
    target = _LocalImportTarget(str(tmp_path / "target"))
    await _import_snapshot(LocalEnvironment(str(source)), target)
    assert not marker.exists()
    (Path(target.workspace) / "cache.txt").write_text("after\n")
    assert "-before" in _git(Path(target.workspace), "diff")
    assert "+after" in _git(Path(target.workspace), "diff")
    assert not (source / ".git").exists()


async def test_options_set_owned_resources_and_readonly_mount_without_socket(monkeypatch, tmp_path) -> None:
    fake = FakeDocker(lambda command, kwargs: _result(stdout=f"{CONTAINER_ID}\n".encode()))
    _patch(monkeypatch, fake)
    assets = tmp_path / "assets"
    assets.mkdir()
    options = DockerOptions(
        image="python:3.11", cpus=1.5, memory="256m", pids_limit=64,
        mounts=(DockerMount(str(assets), "/assets"),),
    )
    env = DockerEnvironment(options=options)
    await env.setup()
    argv = fake.calls[0][0]
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert argv[argv.index("--memory") + 1] == "256m"
    assert argv[argv.index("--pids-limit") + 1] == "64"
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--mount") + 1] == f"type=bind,source={assets},target=/assets,readonly"
    assert "docker.sock" not in " ".join(argv)
    assert "--init" not in argv


async def test_owned_docker_init_process_is_opt_in(monkeypatch) -> None:
    fake = FakeDocker(lambda command, kwargs: _result(stdout=f"{CONTAINER_ID}\n".encode()))
    _patch(monkeypatch, fake)
    env = DockerEnvironment(init_process=True)
    await env.setup()
    assert "--init" in fake.calls[0][0]


async def test_options_reject_socket_directory_before_container_launch(monkeypatch, tmp_path) -> None:
    fake = FakeDocker(lambda command, kwargs: AssertionError(command))
    _patch(monkeypatch, fake)
    with tempfile.TemporaryDirectory(prefix="oc-socket-", dir="/tmp") as directory:
        with socket.socket(socket.AF_UNIX) as daemon:
            daemon.bind(str(Path(directory) / "docker.sock"))
            env = DockerEnvironment(options=DockerOptions(mounts=(DockerMount(directory, "/assets"),)))
            with pytest.raises(ValueError, match="sockets"):
                await env.setup()
    assert fake.calls == []


@pytest.mark.parametrize("changes", [
    {"network": "host"}, {"network": "container:other"}, {"cpus": 0}, {"cpus": float("inf")},
    {"memory": "0"}, {"memory": "256m --privileged"}, {"pids_limit": True}, {"pids_limit": -1},
])
def test_options_reject_invalid_or_shared_host_settings(changes) -> None:
    with pytest.raises(ValueError):
        DockerOptions(**changes)


async def test_lost_process_group_stops_owned_container_and_revokes_sibling_tools(monkeypatch) -> None:
    owner = DockerEnvironment()
    first = DockerEnvironment(container_id=CONTAINER_ID)
    sibling = DockerEnvironment(container_id=CONTAINER_ID)
    first._execution_owner = sibling._execution_owner = owner
    stopped = []

    async def unproven(token):
        return False

    async def stop():
        stopped.append(True)

    monkeypatch.setattr(first, "_cancel_inner", unproven)
    monkeypatch.setattr(owner, "_stop_owned_container", stop)
    assert await first._recover_inner("lost") is False
    assert stopped == [True]
    assert owner.revoked and sibling.revoked
    with pytest.raises(RuntimeError, match="aborted"):
        await sibling.exec_cmd("printf forbidden")


@pytest.mark.parametrize("scope", ["group", "session"])
async def test_docker_sessions_preserve_tagged_build_versions(tmp_path: Path, scope: str) -> None:
    image = os.environ.get("OPENCOLLAB_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("OPENCOLLAB_DOCKER_TEST_IMAGE selects a local image with bash and git")
    source = tmp_path / "source"
    _repository(source)
    _git(source, "tag", "v1.0.0")
    (source / "answer.py").write_text("answer = 2\n")
    _git(source, "commit", "--quiet", "-am", "second")
    _git(source, "tag", "-a", "v1.1.0", "-m", "release")
    (source / "answer.py").write_text("answer = 3\n")
    _git(source, "commit", "--quiet", "-am", "third")
    version = _git(source, "describe", "--tags", "--dirty")
    pool = DockerIsolationPool(str(source), scope=scope, options=DockerOptions(image=image))
    try:
        environment = await pool.acquire("builder")
        result = await environment.exec_cmd(
            "git describe --tags --dirty && git describe --dirty && "
            "git show v1.0.0:answer.py && git show v1.1.0:answer.py && "
            "git cat-file -t refs/tags/v1.0.0 && git cat-file -t refs/tags/v1.1.0"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [version, version, "answer = 1", "answer = 2", "commit", "tag"]
    finally:
        await pool.release()


@pytest.mark.parametrize("scope", ["group", "session"])
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_docker_sessions_preserve_shallow_source_history(tmp_path: Path, scope: str, object_format: str) -> None:
    image = os.environ.get("OPENCOLLAB_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("OPENCOLLAB_DOCKER_TEST_IMAGE selects a local image with bash and git")
    source = tmp_path / "source"
    _shallow_repository(source, object_format=object_format)
    pool = DockerIsolationPool(str(source), scope=scope, options=DockerOptions(image=image))
    try:
        first = await pool.acquire("builder")
        reviewer = await pool.acquire("reviewer", source=first)
        for environment in (first, reviewer):
            result = await environment.exec_cmd(
                "git rev-parse --is-shallow-repository && git rev-list --count HEAD && "
                "git describe --tags && git show HEAD:answer.py"
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.splitlines() == ["true", "1", "v1.2", "answer = 2"]
    finally:
        await pool.release()
    assert _git(source, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(source, "rev-list", "--count", "HEAD") == "1"


@pytest.mark.parametrize("scope", ["group", "session"])
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_docker_sessions_execute_and_deliver_before_owned_cleanup(
    tmp_path: Path, scope: str, object_format: str,
) -> None:
    image = os.environ.get("OPENCOLLAB_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("OPENCOLLAB_DOCKER_TEST_IMAGE selects a local image with bash and git")
    source = tmp_path / "source"
    _repository(source, object_format=object_format)
    (source / "untracked.txt").write_text("source input\n")
    base = LocalEnvironment(str(source))
    pool = DockerIsolationPool(
        str(source), base_environment=base, scope=scope,
        options=DockerOptions(image=image, cpus=1, memory="256m", pids_limit=64),
    )
    first = second = review = None
    try:
        first = await pool.acquire("first")
        second = await pool.acquire("second")
        first_id = first._delegate.container_reference
        second_id = second._delegate.container_reference
        inspected = json.loads(subprocess.check_output(["docker", "inspect", first_id]))[0]
        assert inspected["HostConfig"]["Init"] is True
        assert inspected["HostConfig"]["NetworkMode"] == "none"
        assert inspected["HostConfig"]["NanoCpus"] == 1_000_000_000
        assert inspected["HostConfig"]["Memory"] == 256 * 1024 * 1024
        assert inspected["HostConfig"]["PidsLimit"] == 64
        assert inspected["Mounts"] == []
        assert (first_id == second_id) is (scope == "group")
        assert first.workspace != second.workspace or scope == "session"
        assert await first.read_file("untracked.txt") == "source input\n"
        result = await first.exec_cmd("printf 'answer = 2\\n' > answer.py; printf private > /tmp/private-marker")
        assert result.returncode == 0
        assert await second.read_file("answer.py") == "answer = 1\n"
        assert (await second.exec_cmd("test -e /tmp/private-marker")).returncode == (0 if scope == "group" else 1)
        review = await pool.acquire("reviewer", source=first)
        assert await review.read_file("answer.py") == "answer = 2\n"
        assert (review._delegate.container_reference == first_id) is (scope == "group")
        await review.write_file("answer.py", "answer = 3\n")
        assert await first.read_file("answer.py") == "answer = 2\n"
        assert "+answer = 3" in await review.get_diff()
        patch = await first.get_diff()
        assert "+answer = 2" in patch
        assert (source / "answer.py").read_text() == "answer = 1\n"
        first.retain_changes()
        with pytest.raises(OSError, match="retained"):
            await pool.release()
        assert first.recovery_location is not None
        assert "+answer = 2" in await first.get_diff()
    finally:
        for env in (first, second, review):
            if env is not None and env.recovery_location is not None:
                await env.get_diff()
        await pool.release()
    assert not base.revoked
    assert not pool._owners


@pytest.mark.parametrize("scope", ["group", "session"])
async def test_docker_imports_external_review_source_and_cancels_owned_command(tmp_path: Path, scope: str) -> None:
    image = os.environ.get("OPENCOLLAB_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("OPENCOLLAB_DOCKER_TEST_IMAGE selects a local image with bash and git")
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("original\n")
    external = tmp_path / "external"
    _repository(external)
    (external / "answer.py").write_text("answer = 4\n")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "data.txt").write_text("read-only\n")
    pool = DockerIsolationPool(str(source), scope=scope, options=DockerOptions(
        image=image, mounts=(DockerMount(str(assets), "/assets"),),
    ))
    running = None
    container_ids = set()
    try:
        first = await pool.acquire("first")
        review = await pool.acquire("review", source=LocalEnvironment(str(external)))
        container_ids.update((first.container_reference, review.container_reference))
        assert (first.container_reference == review.container_reference) is (scope == "group")
        assert await review.read_file("answer.py") == "answer = 4\n"
        assert await first.read_file("input.txt") == "original\n"
        mounted_file = await first.exec_cmd("cat /assets/data.txt")
        assert mounted_file.returncode == 0
        assert mounted_file.stdout == "read-only\n"
        assert (await first.exec_cmd("printf changed > /assets/data.txt")).returncode != 0
        assert (assets / "data.txt").read_text() == "read-only\n"
        running = asyncio.create_task(first.exec_cmd("printf ready > started; sleep 30; printf late > late"))
        for _ in range(100):
            if (await first.exec_cmd("test -e started")).returncode == 0:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("owned Docker command did not reach its executable probe")
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        control = pool._owners[0].container
        container_id = first.container_reference
        assert not control.revoked
        inspected = json.loads(subprocess.check_output(["docker", "inspect", container_id]))[0]
        assert inspected["State"]["Running"] is True
        assert (await control.exec_cmd(f"test -e {first.workspace}/late")).returncode == 1
        first.retain_changes()
        assert "started" in await first.get_diff()
    finally:
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await pool.release()
    assert not (source / "started").exists()
    assert not (source / ".git").exists()
    for container_id in container_ids:
        assert subprocess.run(["docker", "inspect", container_id], capture_output=True).returncode != 0


async def test_unproven_docker_cancellation_retains_unexported_candidate(tmp_path: Path) -> None:
    image = os.environ.get("OPENCOLLAB_DOCKER_TEST_IMAGE")
    if not image:
        pytest.skip("OPENCOLLAB_DOCKER_TEST_IMAGE selects a local image with bash and git")
    source = tmp_path / "source"
    _repository(source)
    pool = DockerIsolationPool(str(source), scope="session", options=DockerOptions(image=image))
    running = None
    first = None
    try:
        first = await pool.acquire("first")
        container_id = first.container_reference
        running = asyncio.create_task(first.exec_cmd(
            "rm -f /tmp/.opencollab-exec-*.pid; printf 'answer = 5\\n' > answer.py; "
            "printf ready > started; sleep 300; printf late > late",
            timeout=600,
        ))
        for _ in range(100):
            if (await first.exec_cmd("test -e started")).returncode == 0:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("owned Docker command did not reach its executable probe")
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert first.revoked
        inspected = json.loads(subprocess.check_output(["docker", "inspect", container_id]))[0]
        assert inspected["HostConfig"]["Init"] is True
        assert inspected["State"]["Running"] is False
        first.retain_changes()
        with pytest.raises(OSError, match="retained"):
            await pool.release()
        assert pool._owners
        assert first.recovery_location == f"{container_id}:{first.workspace}"
        artifact = tmp_path / "answer.py"
        subprocess.run(["docker", "cp", f"{container_id}:{first.workspace}/answer.py", str(artifact)], check=True)
        assert artifact.read_text() == "answer = 5\n"
    finally:
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if first is not None and first.recovery_location is not None:
            subprocess.run(["docker", "start", first.container_reference], check=True, capture_output=True)
            assert "+answer = 5" in await first.get_diff()
        await pool.release()
    assert (source / "answer.py").read_text() == "answer = 1\n"
    assert subprocess.run(["docker", "inspect", container_id], capture_output=True).returncode != 0
