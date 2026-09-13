"""Regression tests for prepared base identity and publication on Linux."""
import errno
import json
import os
import platform
import subprocess
from types import SimpleNamespace

import pytest

from opencollab.adapters.execution import linux as osbackend

pytestmark = [
    pytest.mark.linux_only,
    pytest.mark.skipif(
        platform.system() != "Linux" or os.geteuid() != 0,
        reason="rootfs identity tests need privileged Linux mount support",
    ),
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECSERVER_OS_BASES", str(tmp_path / "bases"))
    monkeypatch.setenv("EXECSERVER_OS_ROOT", str(tmp_path / "sandboxes"))
    monkeypatch.setattr(osbackend, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    backend = osbackend.OSBackend()
    source = tmp_path / "source"
    source.mkdir()
    (source / "marker").write_text("original")
    (source / "marker").chmod(0o644)
    yield backend, source
    for root in backend._bases.glob("*/rootfs"):
        subprocess.run(["umount", str(root)], capture_output=True)


def build(store):
    backend, source = store
    digest = backend.build_base_from_host("testbase", source_root=source)
    return backend, backend._bases / "testbase" / "rootfs", digest


def test_digest_missing_root_rejects(store):
    backend, source = store
    with pytest.raises(FileNotFoundError):
        backend._compute_digest(source / "absent")


def test_digest_stable_across_reads_and_sensitive_to_metadata(store):
    backend, source = store
    first = backend._compute_digest(source)
    (source / "marker").read_bytes()
    os.utime(source / "marker", ns=(123456789, (source / "marker").stat().st_mtime_ns))
    assert backend._compute_digest(source) == first
    (source / "marker").chmod(0o600)
    assert backend._compute_digest(source) != first


def test_digest_includes_extended_attributes(store):
    backend, source = store
    first = backend._compute_digest(source)
    os.setxattr(source / "marker", "user.oc", b"metadata")
    assert backend._compute_digest(source) != first


def test_built_base_read_only_and_permissions_preserved(store):
    backend, root, digest = build(store)
    assert (root / "marker").stat().st_mode & 0o777 == 0o644
    with pytest.raises(OSError) as caught:
        (root / "marker").write_text("changed")
    assert caught.value.errno == errno.EROFS
    assert backend._resolve_base("testbase")[2] == digest


def test_missing_digest_rejects(store):
    backend, root, _ = build(store)
    (root.parent / "DIGEST").unlink()
    with pytest.raises(RuntimeError):
        backend._resolve_base("testbase")


def test_changed_base_rejects_even_when_admin_remounts(store):
    backend, root, _ = build(store)
    subprocess.run(["mount", "-o", "remount,bind,rw", str(root)], check=True)
    (root / "marker").write_text("changed")
    with pytest.raises(RuntimeError):
        backend._resolve_base("testbase")


def test_failed_build_does_not_publish(store):
    backend, source = store
    with pytest.raises(FileNotFoundError):
        backend.build_base_from_host("broken", source_root=source / "absent")
    assert not (backend._bases / "broken").exists()


def test_build_rejects_escaping_name(store):
    backend, source = store
    with pytest.raises(ValueError):
        backend.build_base_from_host("../outside", source_root=source)


def test_failed_tar_producer_does_not_publish_successful_extraction(store, tmp_path, monkeypatch):
    backend, source = store
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "tar"
    wrapper.write_text('#!/bin/sh\n/bin/tar "$@" || exit $?\n'
                       'for arg in "$@"; do [ "$arg" = "-cf" ] && exit 42; done\nexit 0\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    with pytest.raises(RuntimeError, match="build failed"):
        backend.build_base_from_host("broken", source_root=source)
    assert not (backend._bases / "broken").exists()
    assert not list(backend._bases.glob(".building-*"))


async def test_snapshot_pins_digest_and_rejects_same_name_replacement(store, tmp_path):
    backend, root, digest = build(store)
    upper = tmp_path / "upper"
    upper.mkdir()
    sb = SimpleNamespace(env=SimpleNamespace(_sb=SimpleNamespace(
        upper=upper, base_locator="testbase", base_digest=digest)))
    await backend.snapshot(sb, "cp")
    metadata = json.loads(backend._snap_base("cp").read_text())
    assert metadata["digest"] == digest
    subprocess.run(["mount", "-o", "remount,bind,rw", str(root)], check=True)
    (root / "marker").write_text("replacement")
    (root.parent / "DIGEST").write_text(backend._compute_digest(root))
    with pytest.raises(RuntimeError, match="version|digest"):
        await backend.materialize("cp")


async def test_snapshot_missing_metadata_never_falls_back_to_host(store):
    backend, _ = store
    backend._snap_tar("broken").write_bytes(b"not an archive")
    with pytest.raises((RuntimeError, FileNotFoundError)):
        await backend.materialize("broken")
