from __future__ import annotations

import os
import stat

import pytest

from opencollab.adapters._env_base import _is_write_rejected
from opencollab.adapters.safe_anchored_files import (
    create_regular_bytes_atomic_at,
    open_directory_anchor,
    write_regular_bytes_atomic_at,
    write_regular_file_atomic_at,
)


@pytest.fixture
def anchored_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    root_fd = open_directory_anchor(root)
    try:
        yield root_fd, root
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("name_length", [16, 224, 255])
def test_atomic_create_and_replace_preserve_permissions_and_content_observation(anchored_root, name_length):
    root_fd, root = anchored_root
    name = "n" * name_length
    target = root / name

    create_regular_bytes_atomic_at(root_fd, root, name, b"before")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    target.chmod(0o666)
    previous_umask = os.umask(0o077)
    try:
        assert write_regular_bytes_atomic_at(root_fd, root, name, b"after", observe_change=True) is True
        assert write_regular_bytes_atomic_at(root_fd, root, name, b"after", observe_change=True) is False
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(target.stat().st_mode) == 0o666
    with pytest.raises(FileExistsError):
        create_regular_bytes_atomic_at(root_fd, root, name, b"foreign")
    assert target.read_bytes() == b"after"
    assert list(root.iterdir()) == [target]


@pytest.mark.parametrize("name_length", [16, 224, 255])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["writer", "size"])
def test_atomic_write_failure_retains_target_and_removes_temporary(anchored_root, name_length, existing, failure):
    root_fd, root = anchored_root
    name = "n" * name_length
    target = root / name
    if existing:
        target.write_bytes(b"before")

    def writer(handle):
        handle.write(b"partial")
        if failure == "writer":
            raise RuntimeError("writer failed")

    error = RuntimeError if failure == "writer" else ValueError
    with pytest.raises(error, match="writer failed" if failure == "writer" else "exceeds"):
        write_regular_file_atomic_at(root_fd, root, name, writer, max_bytes=32 if failure == "writer" else 3)

    if existing:
        assert target.read_bytes() == b"before"
    assert list(root.iterdir()) == ([target] if existing else [])


@pytest.mark.parametrize("phase", ["staging", "cleanup", "publication"])
def test_failed_atomic_write_marks_only_clean_prepublication_error(anchored_root, monkeypatch, phase):
    root_fd, root = anchored_root
    target = root / "target"
    target.write_bytes(b"before")

    def write(handle):
        handle.write(b"after")
        if phase != "publication":
            raise OSError("staging")

    def fail_cleanup(_name, **_kwargs):
        raise OSError("cleanup")

    def fail_publication(_source, _target, **_kwargs):
        raise OSError("publication")

    with monkeypatch.context() as scoped:
        if phase == "cleanup":
            scoped.setattr(os, "unlink", fail_cleanup)
        elif phase == "publication":
            scoped.setattr(os, "rename", fail_publication)
        with pytest.raises(OSError, match=phase) as error:
            write_regular_file_atomic_at(root_fd, root, target.name, write, max_bytes=32)

    assert _is_write_rejected(error.value) is (phase == "staging")
    assert bool(list(root.glob(".oc-*.tmp"))) is (phase == "cleanup")
    assert target.read_bytes() == b"before"


@pytest.mark.parametrize("name_length", [16, 224])
@pytest.mark.parametrize("linked_component", ["target", "parent"])
def test_atomic_write_rejects_symlink_components(anchored_root, tmp_path, name_length, linked_component):
    root_fd, root = anchored_root
    outside = tmp_path / "outside"
    outside.mkdir()
    name = "n" * name_length
    target = outside / name
    target.write_bytes(b"outside")
    if linked_component == "target":
        link = root / name
        link.symlink_to(target)
        relative = name
    else:
        link = root / "parent"
        link.symlink_to(outside, target_is_directory=True)
        relative = f"parent/{name}"

    with pytest.raises(OSError):
        write_regular_bytes_atomic_at(root_fd, root, relative, b"changed")

    assert link.is_symlink()
    assert target.read_bytes() == b"outside"
    assert list(root.iterdir()) == [link]
    assert list(outside.iterdir()) == [target]
