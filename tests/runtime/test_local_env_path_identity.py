from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from opencollab.adapters import safe_anchored_files
from opencollab.environments import local_environment


async def _operate(environment, operation, path):
    if operation == "read":
        return await environment.read_file(path)
    if operation == "range":
        result = await environment.read_text_range(path, offset=1, limit=1, max_chars=100)
        return "\n".join(result.lines) + "\n"
    await environment.write_file(path, "changed\n")


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("operation", ["read", "range", "write"])
@pytest.mark.parametrize("component", ["directory", "symlink", "missing", "regular-file"])
async def test_local_parent_traversal_checks_each_original_component(
    tmp_path, absolute, operation, component,
):
    (tmp_path / "actual/child").mkdir(parents=True)
    target = tmp_path / "value.txt"
    target.write_text("original\n")
    other = tmp_path / "actual/value.txt"
    other.write_text("other\n")
    parent = tmp_path / "component"
    if component == "directory":
        parent.mkdir()
    elif component == "symlink":
        parent.symlink_to("actual/child", target_is_directory=True)
    elif component == "regular-file":
        parent.write_text("regular\n")
    relative = "component/../value.txt"
    path = os.path.join(str(tmp_path), relative) if absolute else relative
    environment = local_environment(tmp_path)
    try:
        if component == "directory":
            result = await _operate(environment, operation, path)
            if operation != "write":
                assert result == "original\n"
            assert target.read_text() == ("changed\n" if operation == "write" else "original\n")
        else:
            with pytest.raises(OSError):
                await _operate(environment, operation, path)
            assert target.read_text() == "original\n"
        assert other.read_text() == "other\n"
        if component == "missing":
            assert not parent.exists()
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("operation", ["read", "range", "write"])
async def test_local_final_symlink_remains_rejected(tmp_path, operation):
    target = tmp_path / "value.txt"
    target.write_text("original\n")
    (tmp_path / "link").symlink_to(target.name)
    environment = local_environment(tmp_path)
    try:
        with pytest.raises(OSError):
            await _operate(environment, operation, "link")
        assert target.read_text() == "original\n"
        assert (tmp_path / "link").is_symlink()
    finally:
        await environment.cleanup()


async def test_local_parent_traversal_keeps_new_parent_creation_and_owned_removal(tmp_path):
    (tmp_path / "sub").mkdir()
    environment = local_environment(tmp_path)
    try:
        await environment.write_file("sub/../new/value.txt", "created\n")
        assert (tmp_path / "new/value.txt").read_text() == "created\n"
        temporary = await environment.write_temp_file("temporary", prefix="owned-")
        await environment.remove_file("sub/../" + Path(temporary).name)
        assert not Path(temporary).exists()
    finally:
        await environment.cleanup()


async def test_local_workspace_selection_resolves_symlink_before_parent(tmp_path):
    (tmp_path / "actual/child").mkdir(parents=True)
    (tmp_path / "alias").symlink_to("actual/child", target_is_directory=True)
    (tmp_path / "actual/value.txt").write_text("selected\n")
    (tmp_path / "value.txt").write_text("unrelated\n")
    environment = local_environment(os.path.join(str(tmp_path), "alias/.."))
    try:
        assert await environment.read_file("value.txt") == "selected\n"
    finally:
        await environment.cleanup()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system-directory aliases")
@pytest.mark.parametrize("alias_root", ["/tmp", "/var/tmp"])
async def test_local_absolute_system_alias_preserves_parent_checks(alias_root):
    with tempfile.TemporaryDirectory(dir=os.path.realpath(alias_root)) as directory:
        root = Path(directory)
        (root / "sub").mkdir()
        (root / "actual/child").mkdir(parents=True)
        (root / "alias").symlink_to("actual/child", target_is_directory=True)
        target = root / "value.txt"
        target.write_text("original\n")
        public_root = os.path.join(alias_root, root.name)
        environment = local_environment(root)
        try:
            await environment.write_file(os.path.join(public_root, "sub/../value.txt"), "changed\n")
            with pytest.raises(OSError):
                await environment.write_file(os.path.join(public_root, "alias/../value.txt"), "wrong\n")
            assert target.read_text() == "changed\n"
            assert not (root / "actual/value.txt").exists()
        finally:
            await environment.cleanup()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor limits")
@pytest.mark.parametrize("traversal", ["plain", "early-parent", "late-parent"])
def test_deep_ordinary_local_paths_use_bounded_descriptors(tmp_path, traversal):
    relative = "/".join(["sub"] * 64 + ["value.txt"])
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text("original\n")
    if traversal == "early-parent":
        (tmp_path / "cancel").mkdir()
        relative = "cancel/../" + relative
    elif traversal == "late-parent":
        (target.parent / "cancel").mkdir()
        relative = "/".join(["sub"] * 64 + ["cancel", "..", "value.txt"])
    code = (
        "import asyncio, os, resource, sys\n"
        "from opencollab.environments import local_environment\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (32, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))\n"
        "before = len(os.listdir('/dev/fd'))\n"
        "async def run():\n"
        "    environment = local_environment(sys.argv[1])\n"
        "    try:\n"
        "        assert await environment.read_file(sys.argv[2]) == 'original\\n'\n"
        "        await environment.write_file(sys.argv[2], 'changed\\n')\n"
        "    finally:\n"
        "        await environment.cleanup()\n"
        "asyncio.run(run())\n"
        "assert len(os.listdir('/dev/fd')) == before\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), relative], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "changed\n"


async def test_parent_step_keeps_the_opened_ancestor_when_its_path_is_replaced(tmp_path, monkeypatch):
    (tmp_path / "parent/child").mkdir(parents=True)
    (tmp_path / "parent/value.txt").write_text("original\n")
    environment = local_environment(tmp_path)
    started, release = threading.Event(), threading.Event()
    original_open = safe_anchored_files.os.open

    def blocked_open(path, flags, mode=0o777, *, dir_fd=None):
        if os.fspath(path) == "value.txt":
            started.set()
            assert release.wait(2)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_anchored_files.os, "open", blocked_open)
    reading = asyncio.create_task(environment.read_file("parent/child/../value.txt"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        (tmp_path / "parent").rename(tmp_path / "saved-parent")
        (tmp_path / "parent").mkdir()
        (tmp_path / "parent/value.txt").write_text("replacement\n")
        release.set()
        assert await reading == "original\n"
        assert (tmp_path / "parent/value.txt").read_text() == "replacement\n"
    finally:
        release.set()
        await asyncio.gather(reading, return_exceptions=True)
        await environment.cleanup()
